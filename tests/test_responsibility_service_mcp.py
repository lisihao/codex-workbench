"""Real MCP-to-Authority coverage for responsibility ledger operations."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from typing import Any

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract, canonical_json
from codex_workbench.responsibility_api import TOOL_NAME
from codex_workbench.store import WorkbenchStore

from tests.test_connection_service_e2e import ROOT, bridge


_DEADLINE = "2099-01-01T12:00:00+00:00"
_NEXT_RECHECK = "2098-01-01T12:00:00+00:00"


class ResponsibilityServiceMCPTests(unittest.TestCase):
    """Exercise a real bridge, CLI MCP child, HTTP Authority, and SQLite ledger."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="responsibility-service-mcp-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self.task_id = "responsibility-service-mcp"
        self.goal_id = "preserve-accountability"
        self.source_owner = "source-session"
        self.recipient_owner = "recipient-session"

        self.config = WorkbenchConfig(self.root / "state", port=0)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        source_context_ref = self._record_session(self.source_owner)
        self._record_session(self.recipient_owner)
        self._create_task(source_context_ref)
        self.store.bind_task_to_session(self.source_owner, self.task_id)
        self.store.bind_task_to_session(self.recipient_owner, self.task_id)
        self.execution_before = self._execution_snapshot()

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
                "state_file": str(self.config.state_root / "responsibility-mcp-bridge-state.json"),
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

    def _record_session(self, source_thread_id: str) -> str:
        context_ref = self.store.artifacts.put_text(
            json.dumps({"kind": "responsibility-service-mcp", "session": source_thread_id}) + "\n",
            "json",
        )
        archive_ref = self.store.artifacts.put_text("fixture archive\n", "txt")
        self.store.record_session_context(
            command_id=f"context-{source_thread_id}",
            request_hash=f"context-hash-{source_thread_id}",
            source_thread_id=source_thread_id,
            context_ref=context_ref,
            archive_ref=archive_ref,
            manifest={"schema_version": 1},
            repository=str(self.repository),
            base_sha="a" * 40,
            allowed_scopes=("src",),
            context_excerpt="fixture context",
        )
        return context_ref

    def _create_task(self, source_context_ref: str) -> None:
        contract = TaskContract(
            task_id=self.task_id,
            repository=str(self.repository),
            base_sha="a" * 40,
            objective="preserve responsibility without starting fixture work",
            allowed_scope=("src",),
            source_thread_id=self.source_owner,
            context_bundle_ref=source_context_ref,
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("work", self.task_id, "work", "fixture", "fixture", "fixture work"),
                NodeSpec(
                    "verify",
                    self.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "fixture verification",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            "create-responsibility-service-mcp",
        )

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)

    def _persist_child_port(self) -> None:
        WorkbenchConfig(self.config.state_root, port=self.server.server_port).initialize()

    def _initialize_bridge(self) -> None:
        initialized = self.connection.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "responsibility-e2e"}},
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
        self.assertIn(TOOL_NAME, {tool["name"] for tool in tools})

    def _call(self, rpc_id: int, arguments: dict[str, object]) -> dict[str, Any]:
        response = self.connection.handle({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": arguments},
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

    def _identity(self) -> dict[str, object]:
        task = self.store.get_task(self.task_id)
        work = next(node for node in task["nodes"] if node["node_id"] == "work")
        return {
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "node_id": "work",
            "attempt": work["attempt"],
            "task_revision": task["state_revision"],
        }

    def _mutation(
        self,
        op: str,
        request_id: str,
        source_thread_id: str,
        **additional: object,
    ) -> dict[str, object]:
        return {
            "op": op,
            **self._identity(),
            "source_thread_id": source_thread_id,
            "next_action": {"action": "record the next accountable step"},
            "deadline": _DEADLINE,
            "request_id": request_id,
            **additional,
        }

    def _authority_rows(self) -> list[dict[str, object]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT request_id, state, tool, task_id FROM authority_requests ORDER BY request_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _responsibility_events(self) -> list[dict[str, object]]:
        return [
            event
            for event in self.store.read_events(task_id=self.task_id)
            if event["event_type"].startswith("responsibility.")
        ]

    def _task_routes(self) -> list[tuple[str, str]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT source_thread_id, task_id FROM session_notification_routes "
                "WHERE task_id = ? ORDER BY source_thread_id",
                (self.task_id,),
            ).fetchall()
        return [(str(row["source_thread_id"]), str(row["task_id"])) for row in rows]

    def _execution_snapshot(self) -> dict[str, object]:
        with self.store.connection() as connection:
            task = connection.execute(
                "SELECT task_id, state, state_revision, priority, contract_hash FROM tasks WHERE task_id = ?",
                (self.task_id,),
            ).fetchone()
            nodes = connection.execute(
                "SELECT * FROM nodes WHERE task_id = ? ORDER BY node_id",
                (self.task_id,),
            ).fetchall()
            allocations = connection.execute(
                "SELECT * FROM worktree_allocations WHERE task_id = ? ORDER BY allocation_id",
                (self.task_id,),
            ).fetchall()
        self.assertIsNotNone(task)
        return {
            "task": dict(task),
            "nodes": [dict(node) for node in nodes],
            "worktree_allocations": [dict(allocation) for allocation in allocations],
        }

    def test_responsibility_lifecycle_is_journaled_without_execution_side_effects(self) -> None:
        routes_before = self._task_routes()
        self.assertEqual(routes_before, [
            (self.recipient_owner, self.task_id),
            (self.source_owner, self.task_id),
        ])
        open_request_id = "responsibility-open"
        open_arguments = self._mutation("open", open_request_id, self.source_owner)
        opened = self._payload(self._call(10, open_arguments))
        replayed_open = self._payload(self._call(11, open_arguments))

        self.assertEqual(canonical_json(replayed_open), canonical_json(opened))
        self.assertEqual((opened["state"], opened["current_owner"]), ("open", self.source_owner))
        self.assertEqual(opened["command_id"], f"responsibility:{self.task_id}:{open_request_id}")
        self.assertEqual([event["event_type"] for event in self._responsibility_events()], [
            "responsibility.opened",
        ])
        self.assertEqual(self._authority_rows(), [{
            "request_id": open_request_id,
            "state": "completed",
            "tool": TOOL_NAME,
            "task_id": self.task_id,
        }])

        journal_before_inspect = self._authority_rows()
        listed = self._payload(self._call(120, {"op": "list", "task_id": self.task_id, "limit": 10}))
        self.assertEqual([item["goal_id"] for item in listed["items"]], [self.goal_id])
        self.assertEqual(self._authority_rows(), journal_before_inspect)
        inspected = self._payload(self._call(12, {
            "op": "inspect",
            "task_id": self.task_id,
            "goal_id": self.goal_id,
        }))
        self.assertEqual((inspected["state"], inspected["current_owner"]), ("open", self.source_owner))
        self.assertEqual(self._authority_rows(), journal_before_inspect)

        proposed = self._payload(self._call(13, self._mutation(
            "propose_handoff",
            "responsibility-propose",
            self.source_owner,
            expected_responsibility_revision=1,
            proposed_owner=self.recipient_owner,
        )))
        self.assertEqual(
            (proposed["state"], proposed["current_owner"], proposed["proposed_owner"]),
            ("handoff_proposed", self.source_owner, self.recipient_owner),
        )
        unclaimed = self._payload(self._call(14, {
            "op": "inspect",
            "task_id": self.task_id,
            "goal_id": self.goal_id,
        }))
        self.assertEqual(
            (unclaimed["current_owner"], unclaimed["proposed_owner"]),
            (self.source_owner, self.recipient_owner),
        )

        claimed = self._payload(self._call(15, self._mutation(
            "claim_handoff",
            "responsibility-claim",
            self.recipient_owner,
            expected_responsibility_revision=2,
        )))
        self.assertEqual(
            (claimed["state"], claimed["current_owner"], claimed["proposed_owner"]),
            ("claimed", self.recipient_owner, None),
        )
        self.assertEqual(self._task_routes(), routes_before)

        deferred = self._payload(self._call(16, self._mutation(
            "defer",
            "responsibility-defer",
            self.recipient_owner,
            expected_responsibility_revision=3,
            wait_reason={
                "wait_kind": "environment",
                "detail": "fixture host is unavailable",
                "release_condition": "fixture host responds",
            },
            next_recheck_at=_NEXT_RECHECK,
        )))
        self.assertEqual((deferred["state"], deferred["current_owner"]), ("deferred", self.recipient_owner))
        self.assertEqual(deferred["wait"]["responsible_owner"], self.recipient_owner)
        self.assertEqual(
            [event["event_type"] for event in self._responsibility_events()],
            [
                "responsibility.opened",
                "responsibility.handoff_proposed",
                "responsibility.handoff_claimed",
                "responsibility.deferred",
            ],
        )
        self.assertEqual(canonical_json(self._execution_snapshot()), canonical_json(self.execution_before))
        self.assertEqual(self._task_routes(), routes_before)

    def test_lost_open_response_resolves_the_same_authority_receipt(self) -> None:
        request_id = "responsibility-lost-open"
        arguments = self._mutation("open", request_id, self.source_owner)
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
                    raise RuntimeError("fixture Authority response gate was not released")
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
            "params": {"name": TOOL_NAME, "arguments": arguments},
        }

        def invoke() -> None:
            try:
                outcome["response"] = self.connection.handle(message)
            except BaseException as error:
                outcome["error"] = error

        caller = threading.Thread(target=invoke, daemon=True)
        caller.start()
        try:
            self.assertTrue(dispatch_completed.wait(timeout=5))
            with self.store.connection() as connection:
                row = connection.execute(
                    "SELECT state FROM authority_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "completed")
            transport = self.connection.transport
            self.assertIsNotNone(transport)
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
        self.assertIsInstance(response, dict)
        opened = self._payload(response)
        self.assertEqual((opened["state"], opened["current_owner"]), ("open", self.source_owner))
        self.assertEqual(dispatch_calls, [request_id])
        self.assertEqual(request_reads, [request_id])
        self.assertEqual([event["event_type"] for event in self._responsibility_events()], [
            "responsibility.opened",
        ])
        self.assertEqual(canonical_json(self._execution_snapshot()), canonical_json(self.execution_before))
