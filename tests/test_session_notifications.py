from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.session_notifications import (
    SCHEMA_SQL,
    ack_session_notification,
    list_session_notifications,
    project_notifications,
    register_task_session,
)
from codex_workbench.store import WorkbenchStore


class SessionNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.store = WorkbenchStore(self.path)
        self.store.initialize()
        self._install_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _install_schema(self) -> None:
        with self.store.connection() as connection:
            connection.executescript(SCHEMA_SQL)

    def _route(self, source_thread_id: str, task_id: str) -> None:
        with self.store.transaction() as connection:
            register_task_session(connection, task_id, source_thread_id)

    def _event(
        self,
        event_type: str,
        task_id: str,
        *,
        node_id: str | None = "worker",
        payload: dict[str, object] | None = None,
        created_at: str = "2026-09-11T12:00:00+00:00",
    ) -> int:
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (event_type, task_id, node_id, json.dumps(payload or {}), created_at),
            ).lastrowid
        assert cursor is not None
        return int(cursor)

    def test_multiple_sessions_and_active_task_changes_keep_routes_isolated(self) -> None:
        self._route("thread-a", "task-a")
        self._route("thread-b", "task-b")
        self._event("node.blocked", "task-a")
        self._event("node.failed", "task-b")

        projected = project_notifications(self.store)
        self.assertEqual(projected["notifications_created"], 2)
        first_a = list_session_notifications(self.store, "thread-a")
        first_b = list_session_notifications(self.store, "thread-b")
        self.assertEqual([item["task_id"] for item in first_a["notifications"]], ["task-a"])
        self.assertEqual([item["task_id"] for item in first_b["notifications"]], ["task-b"])

        # A later active-task change adds a route but never removes the old one.
        self._route("thread-a", "task-b")
        self._event("node.failed", "task-a", node_id="later")
        self._event("node.blocked", "task-b", node_id="later")
        project_notifications(self.store)

        task_a = list_session_notifications(self.store, "thread-a")["notifications"]
        task_b = list_session_notifications(self.store, "thread-b")["notifications"]
        self.assertEqual(
            [item["task_id"] for item in task_a],
            ["task-a", "task-b", "task-a", "task-b"],
        )
        self.assertEqual([item["task_id"] for item in task_b], ["task-b", "task-b"])
        with self.store.connection() as connection:
            routes = connection.execute(
                """
                SELECT source_thread_id, task_id
                FROM session_notification_routes
                ORDER BY source_thread_id, task_id
                """
            ).fetchall()
        self.assertEqual(
            [(row["source_thread_id"], row["task_id"]) for row in routes],
            [("thread-a", "task-a"), ("thread-a", "task-b"), ("thread-b", "task-b")],
        )

    def test_restart_does_not_duplicate_and_ack_is_idempotent_and_session_scoped(self) -> None:
        self._route("thread-a", "task-a")
        self._event(
            "node_recovery.needs_action",
            "task-a",
            payload={"episode_id": "episode-1", "reason_kind": "permission_denied"},
        )
        project_notifications(self.store)
        first = list_session_notifications(self.store, "thread-a")["notifications"]
        self.assertEqual(len(first), 1)
        notification_id = first[0]["notification_id"]

        reopened = WorkbenchStore(self.path)
        reopened.initialize()
        with reopened.connection() as connection:
            connection.executescript(SCHEMA_SQL)
        repeated = project_notifications(reopened)
        self.assertEqual(repeated["notifications_created"], 0)
        self.assertEqual(
            list_session_notifications(reopened, "thread-a")["notifications"], first
        )

        with patch(
            "codex_workbench.session_notifications.now_iso",
            return_value="2026-09-11T12:30:00+00:00",
        ):
            receipt = ack_session_notification(reopened, "thread-a", notification_id)
        self.assertEqual(receipt["state"], "acknowledged")
        self.assertEqual(receipt["acknowledged_at"], "2026-09-11T12:30:00+00:00")
        self.assertNotIn("host_displayed", receipt)
        self.assertEqual(ack_session_notification(reopened, "thread-a", notification_id), receipt)
        self.assertEqual(list_session_notifications(reopened, "thread-a")["notifications"], [])
        with self.assertRaises(PermissionError):
            ack_session_notification(reopened, "thread-b", notification_id)

    def test_late_binding_promotes_unrouted_event_without_copying_payload(self) -> None:
        private_payload = "PRIVATE_EVENT_BODY_" + ("x" * 200_000)
        self._event(
            "node.blocked",
            "task-late",
            payload={"body": private_payload, "paths": ["private/path"]},
        )
        project_notifications(self.store)
        with self.store.connection() as connection:
            unrouted = connection.execute(
                "SELECT source_thread_id FROM session_notifications WHERE task_id = ?",
                ("task-late",),
            ).fetchall()
        self.assertEqual(len(unrouted), 1)
        self.assertIsNone(unrouted[0]["source_thread_id"])

        self._route("thread-late", "task-late")
        listed = list_session_notifications(self.store, "thread-late")["notifications"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source_thread_id"], "thread-late")
        self.assertNotIn("PRIVATE_EVENT_BODY", json.dumps(listed))
        with self.store.connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(session_notifications)")
            }
        self.assertNotIn("payload_json", columns)

    def test_legacy_context_binding_is_consumed_and_metadata_is_bounded(self) -> None:
        self._event(
            "context.task_bound",
            "task-legacy",
            node_id=None,
            payload={"source_thread_id": "thread-legacy"},
        )
        self._event(
            "node_recovery.needs_action",
            "task-legacy",
            payload={
                "episode_id": "episode-" + ("e" * 500),
                "reason_kind": "reason-" + ("r" * 500),
            },
        )
        result = project_notifications(self.store, limit=2)
        self.assertEqual(result["scanned"], 2)
        listed = list_session_notifications(self.store, "thread-legacy")["notifications"]
        self.assertEqual(len(listed), 1)
        self.assertLessEqual(len(listed[0]["reason_kind"]), 128)
        self.assertLessEqual(len(listed[0]["episode_id"]), 128)

    def test_recovery_receipts_and_node_acceptance_are_projected(self) -> None:
        self._route("thread-recovery", "task-recovery")
        for event_type in (
            "node_recovery.action_reconciled",
            "node_recovery.action_settled",
            "node.accepted",
        ):
            self._event(
                event_type,
                "task-recovery",
                payload={"episode_id": "episode-1", "reason_kind": "receipt"},
            )
        project_notifications(self.store)
        listed = list_session_notifications(self.store, "thread-recovery")["notifications"]
        self.assertEqual(
            {item["event_type"] for item in listed},
            {
                "node_recovery.action_reconciled",
                "node_recovery.action_settled",
                "node.accepted",
            },
        )

    def test_late_binding_backfill_is_bounded_and_completes_across_turns(self) -> None:
        with self.store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
                VALUES('node.blocked', 'task-many', 'worker', '{}', '2026-09-11T12:00:00+00:00')
                """,
                [() for _ in range(450)],
            )
        self.assertEqual(project_notifications(self.store)["scanned"], 200)
        self.assertEqual(project_notifications(self.store)["scanned"], 200)
        self.assertEqual(project_notifications(self.store)["scanned"], 50)

        self._route("thread-many", "task-many")
        first_backfill = project_notifications(self.store)
        second_backfill = project_notifications(self.store)
        self.assertEqual(first_backfill["notifications_created"], 200)
        self.assertEqual(second_backfill["notifications_created"], 50)
        self.assertEqual(
            len(list_session_notifications(self.store, "thread-many", limit=50)["notifications"]),
            50,
        )
        with self.store.connection() as connection:
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM session_notifications
                WHERE task_id = ? AND source_thread_id IS NULL
                """,
                ("task-many",),
            ).fetchone()
        self.assertEqual(remaining["count"], 0)

    def test_many_legacy_context_routes_share_one_turn_backfill_budget(self) -> None:
        with self.store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
                VALUES('node.blocked', ?, 'worker', '{}', '2026-09-11T12:00:00+00:00')
                """,
                [(f"task-{index % 3}",) for index in range(450)],
            )
        for _ in range(3):
            project_notifications(self.store)
        with self.store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
                VALUES('context.task_bound', ?, NULL, ?, '2026-09-11T12:00:00+00:00')
                """,
                [
                    (f"task-{index}", json.dumps({"source_thread_id": f"thread-{index}"}))
                    for index in range(3)
                ],
            )

        projected = project_notifications(self.store, limit=3)
        self.assertEqual(projected["notifications_created"], 200)
        with self.store.connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM session_notifications
                WHERE source_thread_id IN ('thread-0', 'thread-1', 'thread-2')
                """
            ).fetchone()
        self.assertEqual(count["count"], 200)

    def test_list_query_uses_session_cursor_index_and_enforces_limit(self) -> None:
        self._route("thread-a", "task-a")
        with self.store.connection() as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT notification_id
                FROM session_notifications
                WHERE source_thread_id = ? AND state = 'pending' AND event_cursor > ?
                ORDER BY event_cursor, notification_id
                LIMIT ?
                """,
                ("thread-a", 0, 51),
            ).fetchall()
        self.assertTrue(
            any("session_notifications_thread_state_cursor_idx" in row["detail"] for row in plan),
            plan,
        )
        with self.assertRaisesRegex(ValueError, "between 1 and 50"):
            list_session_notifications(self.store, "thread-a", limit=51)
        with self.assertRaisesRegex(ValueError, "between 1 and 200"):
            project_notifications(self.store, limit=201)


if __name__ == "__main__":
    unittest.main()
