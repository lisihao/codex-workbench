from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.store import WorkbenchStore
from codex_workbench.task_observation import (
    MATERIAL_EVENT_INDEX_SQL,
    current_task_observations,
)


class TaskObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        with self.store.connection() as connection:
            connection.execute(MATERIAL_EVENT_INDEX_SQL)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert_task(self, task_id: str, state: str, revision: int = 1) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, contract_json, contract_hash, state, state_revision,
                    priority, created_at, updated_at
                ) VALUES(?, '{}', 'hash', ?, ?, 0, '2026-09-11T00:00:00+00:00',
                         '2026-09-11T00:00:00+00:00')
                """,
                (task_id, state, revision),
            )

    def _insert_node(
        self,
        task_id: str,
        node_id: str,
        state: str,
        *,
        verifier: bool = False,
        recovery_json: str | None = None,
        result_json: str | None = None,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO nodes(
                    task_id, node_id, spec_json, state, attempt, result_json,
                    recovery_json, updated_at
                ) VALUES(?, ?, ?, ?, 0, ?, ?, '2026-09-11T00:00:00+00:00')
                """,
                (
                    task_id,
                    node_id,
                    json.dumps({"verifier": verifier}),
                    state,
                    result_json,
                    recovery_json,
                ),
            )

    def _insert_event(
        self,
        task_id: str,
        event_type: str,
        *,
        node_id: str | None = None,
        payload: str = "{}",
    ) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, '2026-09-11T00:00:00+00:00')
                """,
                (event_type, task_id, node_id, payload),
            )

    def test_current_state_phases_and_missing_task_are_explicit(self) -> None:
        self._insert_task("running", "running", revision=7)
        self._insert_node("running", "execution", "running")
        self._insert_node("running", "verification", "running", verifier=True)
        self._insert_node(
            "running", "recovery", "running", recovery_json='{"attempt": 2}'
        )
        self._insert_task("queued", "queued")
        self._insert_node("queued", "pending", "queued")
        self._insert_task("blocked", "blocked")
        self._insert_node("blocked", "blocked-node", "blocked")
        self._insert_task("accepted", "accepted")
        self._insert_node("accepted", "done", "accepted")

        with patch(
            "codex_workbench.task_observation.now_iso",
            return_value="2026-09-11T12:34:56+00:00",
        ), self.store.connection() as connection:
            observations = current_task_observations(
                connection,
                ("running", "queued", "blocked", "accepted", "missing"),
            )

        self.assertEqual(observations["running"]["state"], "running")
        self.assertEqual(observations["running"]["revision"], 7)
        self.assertEqual(
            observations["running"]["active_phases"],
            ["recovery_preparation", "verification", "execution"],
        )
        self.assertEqual(observations["queued"]["active_phases"], [])
        self.assertEqual(observations["blocked"]["active_phases"], [])
        self.assertEqual(observations["accepted"]["active_phases"], [])
        self.assertEqual(observations["missing"]["state"], "observation_unavailable")
        self.assertEqual(observations["missing"]["reason"], "task_not_found")
        self.assertEqual(
            {item["observed_at"] for item in observations.values()},
            {"2026-09-11T12:34:56+00:00"},
        )

    def test_last_material_event_has_only_bounded_metadata(self) -> None:
        task_id = "material"
        self._insert_task(task_id, "running", revision=2)
        private_result = "PRIVATE_RESULT_" + ("r" * 100_000)
        private_payload = "PRIVATE_PAYLOAD_" + ("p" * 100_000)
        self._insert_node(
            task_id,
            "node-" + ("n" * 300),
            "running",
            result_json=json.dumps({"private": private_result}),
        )
        self._insert_event(task_id, "node.started", node_id="first")
        self._insert_event(task_id, "unrelated.payload", node_id="ignored", payload=private_payload)
        self._insert_event(
            task_id,
            "task.state_changed",
            node_id="node-" + ("n" * 300),
            payload=private_payload,
        )

        with self.store.connection() as connection:
            observation = current_task_observations(connection, (task_id,))[task_id]

        event = observation["last_material_event"]
        self.assertEqual(
            set(event),
            {"cursor", "event_type", "node_id", "created_at"},
        )
        self.assertEqual(event["event_type"], "task.state_changed")
        self.assertEqual(len(event["node_id"]), 64)
        self.assertTrue(event["node_id"].endswith("…"))
        self.assertNotIn("PRIVATE_RESULT", json.dumps(observation))
        self.assertNotIn("PRIVATE_PAYLOAD", json.dumps(observation))

    def test_material_event_partial_index_is_used(self) -> None:
        self._insert_task("indexed", "queued")
        with self.store.connection() as connection:
            predicate = MATERIAL_EVENT_INDEX_SQL.split(" WHERE ", 1)[1]
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT cursor, event_type, node_id, created_at
                FROM events
                WHERE task_id = ? AND {predicate}
                ORDER BY cursor DESC LIMIT 1
                """.format(predicate=predicate),
                ("indexed",),
            ).fetchall()

        self.assertTrue(
            any("events_task_material_cursor_idx" in str(row["detail"]) for row in plan),
            plan,
        )

    def test_page_bound_is_fixed(self) -> None:
        with self.store.connection() as connection:
            with self.assertRaisesRegex(ValueError, "at most 100"):
                current_task_observations(connection, tuple(f"task-{i}" for i in range(101)))


if __name__ == "__main__":
    unittest.main()
