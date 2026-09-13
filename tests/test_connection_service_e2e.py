from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.store import StateConflictError, WorkbenchStore


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "workbench-mcp-bridge.py"


def _load_bridge() -> Any:
    spec = importlib.util.spec_from_file_location("connection_service_e2e_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class ConnectionServiceEndToEndTests(unittest.TestCase):
    """Exercise the real local bridge, CLI MCP child, HTTP Authority, and SQLite ledger."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        (self.repository / "src" / "ancestor.py").write_text(
            "accepted = True\n", encoding="utf-8"
        )
        self.config = WorkbenchConfig(self.root, port=0)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self._create_accepted_ancestor()
        self.server: WorkbenchHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self._start_server(0)
        self._persist_client_port()
        self.addCleanup(self._stop_server)
        self.output = io.StringIO()
        self.bridge = bridge.MCPConnectionBridge(
            bridge.validate_config({
                "schema_version": 1,
                "command": [
                    sys.executable,
                    # The module exposes its CLI via ``main()`` rather than a
                    # module-level execution guard.
                    "-c",
                    "import sys; sys.path.insert(0, " + repr(str(ROOT / "src")) + "); from codex_workbench.cli import main; main()",
                    "--home",
                    str(self.root),
                    "mcp",
                ],
                "state_file": str(self.root / "bridge-private-state.json"),
                "connect_timeout_seconds": 2,
                "request_timeout_seconds": 3,
                "max_reconnect_attempts": 3,
                "initial_backoff_seconds": 0.001,
                "max_backoff_seconds": 0.004,
            }),
            self.output,
            sleep=lambda _: None,
        )
        self.addCleanup(self.bridge.close)
        self._initialize_bridge()

    def _start_server(self, port: int) -> None:
        config = WorkbenchConfig(self.root, port=port)
        self.server = WorkbenchHTTPServer(config, self.store)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def _stop_server(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.server_thread is not None:
            self.server_thread.join(timeout=3)
        self.server = None
        self.server_thread = None

    def _persist_client_port(self) -> None:
        assert self.server is not None
        WorkbenchConfig(self.root, port=self.server.server_port).initialize()

    def _restart_server(self) -> None:
        assert self.server is not None
        port = self.server.server_port
        self._stop_server()
        self._start_server(port)
        self._persist_client_port()

    def _create_accepted_ancestor(self) -> None:
        self.task_id = "connection-e2e-task"
        self.code_evidence_ref = self.store.artifacts.put_text(
            "diff --git a/src/ancestor.py b/src/ancestor.py\n+"
            "+accepted = True\n",
            "patch",
        )
        contract = TaskContract(
            self.task_id,
            str(self.repository),
            "fixture-base",
            "preserve accepted code evidence across a client reconnect",
            ("src",),
        )
        ancestor = NodeSpec(
            "ancestor",
            self.task_id,
            "accepted ancestor",
            "fixture",
            "fixture",
            "fixture source result",
            write_scopes=("src",),
        )
        follow_up = NodeSpec(
            "follow-up",
            self.task_id,
            "unstarted dependent work",
            "fixture",
            "fixture",
            "must not run in connection tests",
            depends_on=("ancestor",),
        )
        verifier = NodeSpec(
            "verify",
            self.task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            "must not run in connection tests",
            depends_on=("ancestor", "follow-up"),
            verifier=True,
        )
        self.store.create_task(contract, [ancestor, follow_up, verifier], "connection-e2e-create")
        self.epoch = self.store.activate_coordinator("connection-e2e-fixture", "fixture-machine")
        self.store.queue_task(self.task_id)
        claimed = self.store.claim_ready_node("connection-e2e-fixture", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], "ancestor")
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                "accepted fixture ancestor",
                artifacts={"patch": self.code_evidence_ref},
                changed_paths=("src/ancestor.py",),
                checks=("fixture-code-evidence",),
            ),
        )
        accepted = self._ancestor(self.store.get_task(self.task_id))
        self.assertEqual((accepted["state"], accepted["attempt"]), ("accepted", 1))

    def _create_idle_task(self, task_id: str) -> None:
        contract = TaskContract(
            task_id,
            str(self.repository),
            "fixture-base",
            "session rebind target",
            ("src",),
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            "must not run in connection tests",
            verifier=True,
        )
        self.store.create_task(contract, [verifier], f"{task_id}-create")

    def _bind_session(self, source_thread_id: str, task_id: str) -> None:
        context_ref = self.store.artifacts.put_text(
            "{\"kind\":\"connection-e2e-context\"}\n", "json"
        )
        archive_ref = self.store.artifacts.put_text("connection-e2e-archive", "txt")
        self.store.record_session_context(
            command_id=f"{source_thread_id}-context",
            request_hash=f"{source_thread_id}-context-hash",
            source_thread_id=source_thread_id,
            context_ref=context_ref,
            archive_ref=archive_ref,
            manifest={},
            repository=str(self.repository),
            base_sha="fixture-base",
            allowed_scopes=("src",),
            context_excerpt="private fixture context",
        )
        self.store.bind_task_to_session(source_thread_id, task_id)

    def _initialize_bridge(self) -> None:
        initialized = self.bridge.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "e2e"}},
        })
        assert initialized is not None
        self.assertIn("result", initialized, self.bridge.state.data)
        self.bridge.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        catalog = self.bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert catalog is not None
        self.assertIn("workbench_control_task", {
            tool["name"] for tool in catalog["result"]["tools"]
        })

    def _call_tool(self, rpc_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = self.bridge.handle({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        assert response is not None
        return response

    @staticmethod
    def _mcp_text(response: dict[str, object]) -> object:
        result = response["result"]
        assert isinstance(result, dict)
        content = result["content"]
        assert isinstance(content, list) and content
        text = content[0]["text"]
        assert isinstance(text, str)
        return json.loads(text)

    @staticmethod
    def _ancestor(task: dict[str, object]) -> dict[str, object]:
        nodes = task["nodes"]
        assert isinstance(nodes, list)
        return next(node for node in nodes if node["node_id"] == "ancestor")

    def test_service_recreation_preserves_accepted_ancestor_and_code_evidence(self) -> None:
        before_task = self.store.get_task(self.task_id)
        before_events = self.store.read_events(task_id=self.task_id)
        before_evidence = self.store.artifacts.verify(self.code_evidence_ref).read_bytes()

        self._restart_server()
        response = self._call_tool(
            10,
            "workbench_inspect_task",
            {"task_id": self.task_id},
        )

        inspected = self._mcp_text(response)
        self.assertEqual(inspected["task_id"], self.task_id)
        after_task = self.store.get_task(self.task_id)
        self.assertEqual(canonical_json(after_task), canonical_json(before_task))
        self.assertEqual(self.store.read_events(task_id=self.task_id), before_events)
        self.assertEqual(
            self.store.artifacts.verify(self.code_evidence_ref).read_bytes(),
            before_evidence,
        )
        ancestor = self._ancestor(after_task)
        self.assertEqual((ancestor["state"], ancestor["attempt"]), ("accepted", 1))

    def test_compatible_service_change_keeps_bridge_and_adapter_process(self) -> None:
        from unittest.mock import patch

        def health(label):
            with patch("codex_workbench.mcp.code_as_harness_health", return_value={"fixture_business_revision": label}):
                return self._mcp_text(self._call_tool(60, "workbench_harness_health", {}))

        before_task = self.store.get_task(self.task_id)
        before = health("before-fix")
        child = self.bridge.transport.process
        self._restart_server()
        after = health("after-fix")
        self.assertEqual(after["fixture_business_revision"], "after-fix")
        self.assertIs(self.bridge.transport.process, child)
        self.assertIsNone(child.poll())
        old = before["connection_evidence"]
        new = after["connection_evidence"]
        for layer in ("adapter", "bridge"):
            self.assertEqual(old[layer]["instance_id"], new[layer]["instance_id"])
            self.assertEqual(old[layer]["pid"], new[layer]["pid"])
        self.assertNotEqual(old["authority"]["instance_id"], new["authority"]["instance_id"])
        self.assertEqual(new["host_catalog"]["ui_tool_availability"], "unknown")
        self.assertEqual(self.store.get_task(self.task_id), before_task)

    def test_cut_mcp_child_after_persisted_write_queries_same_receipt_once(self) -> None:
        assert self.server is not None
        request_id = "connection-cut-priority"
        before_task = self.store.get_task(self.task_id)
        before_ancestor = json.loads(canonical_json(self._ancestor(before_task)))
        before_evidence = self.store.artifacts.verify(self.code_evidence_ref).read_bytes()
        dispatch_completed = threading.Event()
        release_response = threading.Event()
        dispatch_calls: list[str] = []
        request_reads: list[str] = []
        original_dispatch = self.server.authority_service.dispatch
        original_get_request = self.server.authority_service.get_request

        def gated_dispatch(envelope, *, authenticated_actor="authenticated"):
            receipt = original_dispatch(envelope, authenticated_actor=authenticated_actor)
            if envelope.get("request_id") == request_id:
                dispatch_calls.append(request_id)
                dispatch_completed.set()
                if not release_response.wait(timeout=5):
                    raise RuntimeError("response gate was not released")
            return receipt

        def observed_get_request(receipt_id):
            request_reads.append(receipt_id)
            return original_get_request(receipt_id)

        self.server.authority_service.dispatch = gated_dispatch
        self.server.authority_service.get_request = observed_get_request
        outcome: dict[str, object] = {}
        message = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "workbench_control_task",
                "arguments": {
                    "request_id": request_id,
                    "task_id": self.task_id,
                    "action": "set_priority",
                    "priority": 4,
                    "expected_revision": before_task["state_revision"],
                },
            },
        }

        def invoke() -> None:
            try:
                response = self.bridge.handle(message)
                outcome["response"] = response
            except BaseException as error:
                outcome["error"] = error

        caller = threading.Thread(target=invoke, daemon=True)
        caller.start()
        try:
            self.assertTrue(dispatch_completed.wait(timeout=5))
            with self.store.connection() as connection:
                row = connection.execute(
                    "SELECT state FROM authority_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            assert row is not None
            self.assertEqual(row["state"], "completed")
            transport = self.bridge.transport
            assert transport is not None
            transport.process.terminate()
            transport.process.wait(timeout=2)
        finally:
            release_response.set()
            caller.join(timeout=8)
            self.server.authority_service.dispatch = original_dispatch
            self.server.authority_service.get_request = original_get_request

        self.assertFalse(caller.is_alive())
        self.assertNotIn("error", outcome)
        response = outcome.get("response")
        assert isinstance(response, dict)
        self.assertEqual(response["id"], 20)
        control = self._mcp_text(response)
        self.assertEqual(control["task_id"], self.task_id)
        self.assertEqual(control["revision"], before_task["state_revision"] + 1)
        self.assertEqual(dispatch_calls, [request_id])
        self.assertEqual(request_reads, [request_id])

        after_task = self.store.get_task(self.task_id)
        self.assertEqual(after_task["priority"], 4)
        self.assertEqual(after_task["state_revision"], before_task["state_revision"] + 1)
        self.assertEqual(self._ancestor(after_task), before_ancestor)
        self.assertEqual(
            self.store.artifacts.verify(self.code_evidence_ref).read_bytes(),
            before_evidence,
        )
        priority_events = [
            event for event in self.store.read_events(task_id=self.task_id)
            if event["event_type"] == "task.priority_changed"
        ]
        self.assertEqual(len(priority_events), 1)
        accepted_events = [
            event for event in self.store.read_events(task_id=self.task_id)
            if event["event_type"] == "node.accepted" and event["node_id"] == "ancestor"
        ]
        self.assertEqual(len(accepted_events), 1)

    def test_session_rebind_cas_rejects_frozen_continuation_without_state_change(self) -> None:
        source_thread_id = "connection-e2e-session"
        rebound_task_id = "connection-e2e-rebound"
        self._create_idle_task(rebound_task_id)
        self._bind_session(source_thread_id, self.task_id)
        binding = self.store.get_session_binding(source_thread_id)
        frozen_task_id = binding["active_task_id"]
        self.assertEqual(frozen_task_id, self.task_id)
        before_original = self.store.get_task(self.task_id)
        before_rebound = self.store.get_task(rebound_task_id)
        before_evidence = self.store.artifacts.verify(self.code_evidence_ref).read_bytes()

        self.store.bind_task_to_session(source_thread_id, rebound_task_id)
        with self.assertRaisesRegex(StateConflictError, "active task changed"):
            self.store.append_active_session_steering(
                source_thread_id,
                "continue only the task frozen before this rebind",
                expected_task_id=frozen_task_id,
            )

        self.assertEqual(
            self.store.get_session_binding(source_thread_id)["active_task_id"],
            rebound_task_id,
        )
        after_original = self.store.get_task(self.task_id)
        after_rebound = self.store.get_task(rebound_task_id)
        self.assertEqual(canonical_json(after_original), canonical_json(before_original))
        self.assertEqual(canonical_json(after_rebound), canonical_json(before_rebound))
        self.assertEqual(self._ancestor(after_original)["state"], "accepted")
        self.assertEqual(
            self.store.artifacts.verify(self.code_evidence_ref).read_bytes(),
            before_evidence,
        )
        continuation_events = [
            event for event in self.store.read_events()
            if event["event_type"] == "session.active_task_message_appended"
        ]
        self.assertEqual(continuation_events, [])


if __name__ == "__main__":
    unittest.main()
