"""MCP-to-Authority HTTP coverage for the governed lockfile handoff."""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import unittest
from typing import Any
from unittest.mock import patch

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import canonical_json

from tests.test_connection_service_e2e import ROOT, bridge
from tests import test_lockfile_handoff as lockfile_handoff_tests


class HandoffServiceMCPTests(unittest.TestCase):
    """Use the real bridge, CLI MCP child, HTTP client, and Authority fixture."""

    def setUp(self) -> None:
        self.handoff_fixture = lockfile_handoff_tests.LockfileHandoffTests(methodName="runTest")
        self.handoff_fixture.setUp()
        self.addCleanup(self.handoff_fixture.doCleanups)
        self.addCleanup(self.handoff_fixture.tearDown)

        self.config = WorkbenchConfig(
            self.handoff_fixture.config.state_root,
            port=0,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        self.config.initialize()
        self.store = self.handoff_fixture.store
        self.server = WorkbenchHTTPServer(self.config, self.store)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self._persist_child_port()

        self.output = io.StringIO()
        self.connection = bridge.MCPConnectionBridge(
            bridge.validate_config({
                "schema_version": 1,
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, " + repr(str(ROOT / "src"))
                    + "); from codex_workbench.cli import main; main()",
                    "--home",
                    str(self.config.state_root),
                    "mcp",
                ],
                "state_file": str(self.config.state_root / "handoff-mcp-bridge-state.json"),
                "connect_timeout_seconds": 2,
                "request_timeout_seconds": 3,
                "max_reconnect_attempts": 3,
                "initial_backoff_seconds": 0.001,
                "max_backoff_seconds": 0.004,
            }),
            self.output,
            sleep=lambda _: None,
        )
        self.addCleanup(self.connection.close)
        self._initialize_bridge()

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)

    def _persist_child_port(self) -> None:
        WorkbenchConfig(
            self.config.state_root,
            port=self.server.server_port,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        ).initialize()

    def _initialize_bridge(self) -> None:
        initialized = self.connection.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "handoff-e2e"}},
        })
        self.assertIsInstance(initialized, dict)
        self.assertIn("result", initialized)
        self.connection.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        catalog = self.connection.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIsInstance(catalog, dict)
        tools = catalog["result"]["tools"]
        self.assertIn("workbench_handoff_lockfile", {tool["name"] for tool in tools})

    def _call(self, rpc_id: int, arguments: dict[str, object]) -> dict[str, Any]:
        response = self.connection.handle({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": "workbench_handoff_lockfile", "arguments": arguments},
        })
        self.assertIsInstance(response, dict)
        self.assertNotIn("error", response)
        return response

    def _payload(self, response: dict[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        self.assertIsInstance(result, dict)
        self.assertIsNot(result.get("isError"), True)
        content = result.get("content")
        self.assertIsInstance(content, list)
        self.assertTrue(content)
        text = content[0].get("text")
        self.assertIsInstance(text, str)
        payload = json.loads(text)
        self.assertIsInstance(payload, dict)
        return payload

    def _journal_rows(self) -> list[dict[str, object]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT request_id, state, tool, task_id FROM authority_requests ORDER BY request_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def test_handoff_uses_real_mcp_http_journal_and_operation_identities(self) -> None:
        contract, before, _source = self.handoff_fixture._blocked_fixture("handoff-service-mcp")
        handoff_request_id = "handoff-mcp-apply"
        preview_arguments = self.handoff_fixture._arguments(
            contract,
            before,
            handoff_request_id,
        )

        preview = self._payload(self._call(10, preview_arguments))
        self.assertEqual(preview["state"], "preview")
        self.assertFalse(self.connection.state.has_write(handoff_request_id))
        self.assertEqual(self._journal_rows(), [])

        apply_arguments = {
            **preview_arguments,
            "op": "apply",
            "expected_fingerprint": preview["fingerprint"],
        }
        materializer = lockfile_handoff_tests._FixtureMaterializer()
        with patch("codex_workbench.lockfile_handoff._pnpm_materializer", return_value=materializer):
            applied = self._payload(self._call(11, apply_arguments))
            replayed = self._payload(self._call(12, apply_arguments))
        self.assertEqual(applied["state"], "ready")
        self.assertEqual(canonical_json(replayed), canonical_json(applied))
        self.assertEqual(materializer.calls, 1)
        self.assertTrue(self.connection.state.has_write(handoff_request_id))
        self.assertEqual(self._journal_rows(), [{
            "request_id": handoff_request_id,
            "state": "completed",
            "tool": "workbench_handoff_lockfile",
            "task_id": contract.task_id,
        }])

        status = self._payload(self._call(13, {
            "op": "status",
            "task_id": contract.task_id,
            "request_id": handoff_request_id,
        }))
        self.assertEqual((status["state"], status["request_id"]), ("ready", handoff_request_id))
        self.assertEqual(len(self._journal_rows()), 1)

        for rpc_id, operation in ((14, "cancel"), (16, "reconcile")):
            operation_id = "handoff-mcp-" + operation
            arguments = {
                "op": operation,
                "task_id": contract.task_id,
                "request_id": handoff_request_id,
                "operation_id": operation_id,
            }
            result = self._payload(self._call(rpc_id, arguments))
            replay = self._payload(self._call(rpc_id + 1, arguments))
            self.assertEqual((result["state"], result["request_id"]), ("ready", handoff_request_id))
            self.assertEqual(canonical_json(replay), canonical_json(result))
            self.assertTrue(self.connection.state.has_write(operation_id))

        self.assertEqual(self._journal_rows(), [
            {
                "request_id": handoff_request_id,
                "state": "completed",
                "tool": "workbench_handoff_lockfile",
                "task_id": contract.task_id,
            },
            {
                "request_id": "handoff-mcp-cancel",
                "state": "completed",
                "tool": "workbench_handoff_lockfile",
                "task_id": contract.task_id,
            },
            {
                "request_id": "handoff-mcp-reconcile",
                "state": "completed",
                "tool": "workbench_handoff_lockfile",
                "task_id": contract.task_id,
            },
        ])
