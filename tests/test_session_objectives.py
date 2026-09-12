from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.session_notifications import (
    ack_session_notification,
    list_session_notifications,
    project_notifications,
)
from codex_workbench.store import WorkbenchStore


class SessionObjectiveInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="session-objectives-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = WorkbenchConfig(self.root)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("session-objectives", "fixture")
        self.server = WorkbenchMCPServer(self.config, self.store)

    def _record_context(self, source_thread_id: str, *, excerpt: str = "") -> None:
        self.store.record_session_context(
            command_id=f"context-{source_thread_id}",
            request_hash=f"context-hash-{source_thread_id}",
            source_thread_id=source_thread_id,
            context_ref=f"context-ref-{source_thread_id}",
            archive_ref=f"archive-ref-{source_thread_id}",
            manifest={"schema_version": 1},
            repository=str(self.root),
            base_sha="a" * 40,
            allowed_scopes=("src",),
            context_excerpt=excerpt,
        )

    def _create_task(
        self,
        task_id: str,
        *,
        objective: str = "fixture objective",
        prompt: str = "fixture prompt",
    ) -> None:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.root),
            base_sha="a" * 40,
            objective=objective,
            allowed_scope=("src",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("work", task_id, "work", "fixture", "fixture", prompt),
                NodeSpec(
                    "verify",
                    task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "fixture verification",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            f"create-{task_id}",
        )

    def _accept_task(self, task_id: str) -> None:
        self.store.queue_task(task_id)
        while self.store.get_task(task_id)["state"] != "accepted":
            claimed = self.store.claim_ready_node("accept-worker", self.epoch)
            self.assertIsNotNone(claimed)
            self.store.settle_claimed(claimed, NodeResult("succeeded", "fixture complete"))

    def _session(self, source_thread_id: str, **arguments: object) -> dict:
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "workbench_get_session",
                    "arguments": {"source_thread_id": source_thread_id, **arguments},
                },
            }
        )
        assert response is not None
        result = response["result"]
        self.assertFalse(result.get("isError", False), result)
        return json.loads(result["content"][0]["text"])

    def test_switching_active_task_preserves_earlier_unfinished_objective_and_legacy_binding(self) -> None:
        self._record_context("thread-a", excerpt="PRIVATE_CONTEXT_EXCERPT")
        self._create_task("first-task")
        self._create_task("second-task")
        self.store.bind_task_to_session("thread-a", "first-task")
        self.store.bind_task_to_session("thread-a", "second-task")

        before_task = self.store.get_task("first-task")
        with self.store.connection() as connection:
            before_routes = [
                tuple(row)
                for row in connection.execute(
                    "SELECT source_thread_id, task_id FROM session_notification_routes ORDER BY rowid"
                )
            ]

        session = self._session("thread-a")

        self.assertEqual(session["active_task_id"], "second-task")
        self.assertNotIn("context_excerpt", session)
        self.assertEqual(
            [(entry["task_id"], entry["active"]) for entry in session["objectives"]],
            [("first-task", False), ("second-task", True)],
        )
        self.assertTrue(
            {
                "command_id",
                "source_thread_id",
                "context_ref",
                "archive_ref",
                "manifest",
                "repository",
                "base_sha",
                "allowed_scopes",
                "created_at",
                "updated_at",
                "active_task_id",
            }.issubset(session)
        )
        self.assertEqual(self.store.get_task("first-task"), before_task)
        with self.store.connection() as connection:
            after_routes = [
                tuple(row)
                for row in connection.execute(
                    "SELECT source_thread_id, task_id FROM session_notification_routes ORDER BY rowid"
                )
            ]
        self.assertEqual(after_routes, before_routes)

    def test_inventory_is_session_scoped_and_omits_private_session_data(self) -> None:
        self._record_context("thread-a", excerpt="PRIVATE_CONTEXT_A")
        self._record_context("thread-b", excerpt="PRIVATE_CONTEXT_B")
        self._create_task(
            "thread-a-task",
            objective="PRIVATE_TASK_OBJECTIVE_A",
            prompt="PRIVATE_NODE_PROMPT_A",
        )
        self._create_task("thread-b-task")
        self.store.bind_task_to_session("thread-a", "thread-a-task")
        self.store.bind_task_to_session("thread-b", "thread-b-task")

        session_a = self._session("thread-a")
        serialized = json.dumps(session_a)

        self.assertEqual([entry["task_id"] for entry in session_a["objectives"]], ["thread-a-task"])
        self.assertNotIn("thread-b-task", serialized)
        for private_value in (
            "PRIVATE_CONTEXT_A",
            "PRIVATE_CONTEXT_B",
            "PRIVATE_TASK_OBJECTIVE_A",
            "PRIVATE_NODE_PROMPT_A",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(
            set(session_a["objectives"][0]),
            {"task_id", "state", "state_revision", "active", "delivery_objective"},
        )

    def test_accepted_task_with_unfinished_delivery_remains_visible(self) -> None:
        self._record_context("thread-delivery")
        self._create_task("accepted-delivery-task")
        self.store.bind_task_to_session("thread-delivery", "accepted-delivery-task")
        self._accept_task("accepted-delivery-task")

        self.assertEqual(self._session("thread-delivery")["objectives"], [])
        objective = self.store.create_delivery_objective(
            "accepted-delivery-task",
            "create-accepted-delivery-objective",
            {
                "requested_endpoints": {"github": {"base_branch": "main", "merge": False}},
                "scope": {"paths": ["src"]},
                "authority": {"reason": "fixture request, not a grant"},
            },
        )

        session = self._session("thread-delivery")
        self.assertEqual(len(session["objectives"]), 1)
        entry = session["objectives"][0]
        self.assertEqual((entry["task_id"], entry["state"]), ("accepted-delivery-task", "accepted"))
        self.assertEqual(
            entry["delivery_objective"],
            {
                "objective_id": objective["objective_id"],
                "stage": "plan",
                "state": "active",
            },
        )

    def test_cancelled_task_is_not_visible_without_an_unfinished_delivery(self) -> None:
        self._record_context("thread-cancelled")
        self._create_task("cancelled-task")
        self.store.bind_task_to_session("thread-cancelled", "cancelled-task")
        self.store.transition_task("cancelled-task", "cancelled")

        self.assertEqual(self._session("thread-cancelled")["objectives"], [])

    def test_acknowledging_a_notification_does_not_remove_an_unfinished_objective(self) -> None:
        self._record_context("thread-notification")
        self._create_task("notification-task")
        self.store.bind_task_to_session("thread-notification", "notification-task")
        with self.store.transaction() as connection:
            self.store._event(
                connection,
                "node.blocked",
                "notification-task",
                "work",
                {"reason_kind": "fixture"},
            )
        project_notifications(self.store)
        notification = list_session_notifications(self.store, "thread-notification")["notifications"][0]

        ack_session_notification(
            self.store,
            "thread-notification",
            notification["notification_id"],
        )

        self.assertEqual(
            list_session_notifications(self.store, "thread-notification")["notifications"],
            [],
        )
        self.assertEqual(
            [entry["task_id"] for entry in self._session("thread-notification")["objectives"]],
            ["notification-task"],
        )

    def test_pagination_keeps_route_order_when_active_task_changes(self) -> None:
        self._record_context("thread-page")
        for task_id in ("page-first", "page-second", "page-third"):
            self._create_task(task_id)
            self.store.bind_task_to_session("thread-page", task_id)

        first = self._session("thread-page", objective_limit=2)
        self.assertEqual(
            [entry["task_id"] for entry in first["objectives"]],
            ["page-first", "page-second"],
        )
        self.assertIsInstance(first["next_objective_cursor"], str)

        self.store.bind_task_to_session("thread-page", "page-first")

        second = self._session(
            "thread-page",
            objective_limit=2,
            objective_cursor=first["next_objective_cursor"],
        )
        self.assertEqual([entry["task_id"] for entry in second["objectives"]], ["page-third"])
        self.assertIsNone(second["next_objective_cursor"])
        self.assertEqual(
            [entry["task_id"] for entry in first["objectives"] + second["objectives"]],
            ["page-first", "page-second", "page-third"],
        )
        refreshed = self._session("thread-page", objective_limit=3)
        self.assertEqual(
            [(entry["task_id"], entry["active"]) for entry in refreshed["objectives"]],
            [("page-first", True), ("page-second", False), ("page-third", False)],
        )


if __name__ == "__main__":
    unittest.main()
