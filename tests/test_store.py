from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_workbench.model import CODEX_ASTRA_MODEL, NodeResult, NodeSpec, TaskContract
from codex_workbench.planner import archify_internal_directive
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


def verified(nodes: list[NodeSpec], task_id: str) -> list[NodeSpec]:
    if any(node.verifier for node in nodes):
        return nodes
    return [*nodes, NodeSpec(
        "verify", task_id, "verify", "fixture", "fixture", "accepted",
        depends_on=tuple(node.node_id for node in nodes), verifier=True,
    )]


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("test-store", "test-machine")
        self.contract = TaskContract(
            task_id="task-1",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="test persistent DAG",
            allowed_scope=("src",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _set_node_execution_metadata(
        self,
        task_id: str,
        node_id: str,
        **metadata: object,
    ) -> None:
        """Persist forward-compatible lane fields without changing NodeSpec tests.

        The scheduler must also correctly classify already-persisted plans
        produced before the NodeSpec schema grows these optional fields.
        """

        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT spec_json FROM nodes WHERE task_id = ? AND node_id = ?",
                (task_id, node_id),
            ).fetchone()
            assert row is not None
            spec = json.loads(row["spec_json"])
            spec.update(metadata)
            connection.execute(
                "UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = ?",
                (json.dumps(spec), task_id, node_id),
            )

    def test_idempotent_submit_and_command_conflict(self) -> None:
        nodes = verified([NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok")], "task-1")
        self.assertEqual(self.store.create_task(self.contract, nodes, "cmd-1"), "task-1")
        self.assertEqual(self.store.create_task(self.contract, nodes, "cmd-1"), "task-1")
        changed = verified([NodeSpec("a", "task-1", "changed", "fixture", "fixture", "ok")], "task-1")
        with self.assertRaises(CommandConflictError):
            self.store.create_task(self.contract, changed, "cmd-1")

    def test_schema_one_migrates_to_current_schema(self) -> None:
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
            connection.execute("DROP TABLE delivery_receipts")
        self.store.initialize()
        self.assertEqual(self.store.health()["schema_version"], 11)
        with self.store.connection() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
        self.assertIn("effective_executor", columns)
        self.assertIn("effective_model", columns)
        with self.store.connection() as connection:
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
        self.assertIn("priority", task_columns)
        with self.store.connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertTrue(
            {"worktree_allocations", "worktree_archives", "home_presence_leases"}.issubset(tables)
        )

    def test_schema_three_adds_effective_route_columns(self) -> None:
        path = Path(self.temp.name) / "schema-three.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES('schema_version', '3');
                CREATE TABLE nodes (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    worktree TEXT,
                    started_at TEXT,
                    settled_at TEXT,
                    result_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, node_id)
                );
                """
            )
        migrated = WorkbenchStore(path)
        migrated.initialize()
        self.assertEqual(migrated.health()["schema_version"], 11)
        with migrated.connection() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
        self.assertIn("effective_executor", columns)
        self.assertIn("effective_model", columns)

    def test_schema_nine_migrates_steering_sequence_by_legacy_timestamp_and_id(self) -> None:
        path = Path(self.temp.name) / "schema-nine.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES('schema_version', '9');
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    contract_json TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    state_revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    blocker TEXT,
                    verdict TEXT
                );
                INSERT INTO tasks(
                    task_id, contract_json, contract_hash, state, created_at, updated_at
                ) VALUES('old-task', '{}', 'hash', 'queued', 'one', 'one');
                CREATE TABLE task_steering (
                    steering_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO task_steering(steering_id, task_id, instruction, created_at)
                VALUES
                    ('later', 'old-task', 'later instruction', '2026-09-02T12:00:01+00:00'),
                    ('second', 'old-task', 'same timestamp, second id', '2026-09-02T12:00:00+00:00'),
                    ('first', 'old-task', 'same timestamp, first id', '2026-09-02T12:00:00+00:00');
                """
            )

        migrated = WorkbenchStore(path)
        migrated.initialize()
        with migrated.connection() as connection:
            rows = connection.execute(
                "SELECT steering_id, sequence FROM task_steering "
                "WHERE task_id = 'old-task' ORDER BY sequence"
            ).fetchall()
        self.assertEqual(
            [(row["steering_id"], row["sequence"]) for row in rows],
            [("first", 1), ("second", 2), ("later", 3)],
        )

    def test_context_receipt_binds_latest_context_and_task(self) -> None:
        receipt = self.store.record_session_context(
            command_id="context-1",
            request_hash="request-hash",
            source_thread_id="thread-1",
            context_ref="sha256:" + "a" * 64 + ":tar.gz",
            archive_ref="sha256:" + "a" * 64 + ":tar.gz",
            manifest={"schema_version": 1},
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            allowed_scopes=("src",),
            context_excerpt="history",
        )
        self.assertEqual(receipt["source_thread_id"], "thread-1")
        self.store.create_task(
            self.contract,
            verified([NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok")], "task-1"),
            "context-task",
        )
        self.store.bind_task_to_session("thread-1", "task-1")
        binding = self.store.get_session_binding("thread-1")
        self.assertEqual(binding["active_task_id"], "task-1")
        self.assertEqual(binding["context_excerpt"], "history")

    def test_reimported_context_invalidates_stale_active_task_binding(self) -> None:
        first_ref = "sha256:" + "a" * 64 + ":tar.gz"
        second_ref = "sha256:" + "b" * 64 + ":tar.gz"
        self.store.record_session_context(
            command_id="context-reimport-first",
            request_hash="context-reimport-first-request",
            source_thread_id="thread-reimport",
            context_ref=first_ref,
            archive_ref=first_ref,
            manifest={"schema_version": 1},
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            allowed_scopes=("src",),
            context_excerpt="old history",
        )
        self.store.create_task(
            self.contract,
            verified([NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok")], "task-1"),
            "context-reimport-task",
        )
        self.store.bind_task_to_session("thread-reimport", "task-1")

        self.store.record_session_context(
            command_id="context-reimport-second",
            request_hash="context-reimport-second-request",
            source_thread_id="thread-reimport",
            context_ref=second_ref,
            archive_ref=second_ref,
            manifest={"schema_version": 1},
            repository=str(Path(self.temp.name).resolve()),
            base_sha="def456",
            allowed_scopes=("tests",),
            context_excerpt="new history",
        )

        binding = self.store.get_session_binding("thread-reimport")
        self.assertEqual(binding["context_ref"], second_ref)
        self.assertIsNone(binding["active_task_id"])
        self.assertEqual(binding["context_excerpt"], "new history")
        invalidated = [
            event
            for event in self.store.read_events()
            if event["event_type"] == "context.active_task_invalidated"
        ]
        self.assertEqual(invalidated[-1]["task_id"], "task-1")
        self.assertEqual(invalidated[-1]["payload"]["previous_context_ref"], first_ref)
        with self.assertRaisesRegex(StateConflictError, "no active task"):
            self.store.append_active_session_steering("thread-reimport", "不应继续旧任务")

    def test_active_session_steering_appends_without_terminating_task(self) -> None:
        context_ref = "sha256:" + "c" * 64 + ":tar.gz"
        self.store.record_session_context(
            command_id="active-steering-context",
            request_hash="active-steering-request",
            source_thread_id="thread-active-steering",
            context_ref=context_ref,
            archive_ref=context_ref,
            manifest={"schema_version": 1},
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            allowed_scopes=("src",),
            context_excerpt="original objective",
        )
        self.store.create_task(
            self.contract,
            verified([NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")], "task-1"),
            "active-steering-task",
        )
        self.store.bind_task_to_session("thread-active-steering", "task-1")
        self.store.queue_task("task-1")
        self.store.claim_ready_node("fixture-worker", self.epoch)
        before = self.store.get_task("task-1")

        result = self.store.append_active_session_steering(
            "thread-active-steering",
            "继续当前目标，并补充验证边界。",
        )

        self.assertTrue({"task_id", "revision", "state", "steering_id"} <= set(result))
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["revision"], before["state_revision"] + 1)
        self.assertEqual(result["state"], "running")
        after = self.store.get_task("task-1")
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["contract"], before["contract"])
        work_node = next(node for node in after["nodes"] if node["node_id"] == "work")
        self.assertEqual(work_node["state"], "running")
        self.assertEqual(after["steering"][-1]["steering_id"], result["steering_id"])
        self.assertEqual(after["steering"][-1]["instruction"], "继续当前目标，并补充验证边界。")
        steering_events = [
            event
            for event in self.store.read_events(task_id="task-1")
            if event["event_type"] == "task.steering_added"
        ]
        self.assertEqual(steering_events[-1]["payload"]["steering_id"], result["steering_id"])

        ordered_messages = [f"追加消息 {index}" for index in range(20)]
        for instruction in ordered_messages:
            self.store.append_active_session_steering(
                "thread-active-steering",
                instruction,
            )
        steering = self.store.get_task("task-1")["steering"]
        self.assertEqual(
            [item["instruction"] for item in steering],
            ["继续当前目标，并补充验证边界。", *ordered_messages],
        )

    def test_active_session_steering_rejects_missing_and_terminal_tasks(self) -> None:
        with self.assertRaises((KeyError, StateConflictError)):
            self.store.append_active_session_steering("unknown-thread", "继续")

        context_ref = "sha256:" + "d" * 64 + ":tar.gz"
        self.store.record_session_context(
            command_id="terminal-steering-context",
            request_hash="terminal-steering-request",
            source_thread_id="thread-terminal-steering",
            context_ref=context_ref,
            archive_ref=context_ref,
            manifest={"schema_version": 1},
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            allowed_scopes=("src",),
            context_excerpt="terminal objective",
        )
        self.store.create_task(
            self.contract,
            verified([NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")], "task-1"),
            "terminal-steering-task",
        )
        self.store.bind_task_to_session("thread-terminal-steering", "task-1")
        revision = self.store.transition_task("task-1", "cancelled", expected_revision=1)
        with self.assertRaises(StateConflictError):
            self.store.append_active_session_steering(
                "thread-terminal-steering",
                "不应追加到终态任务",
                expected_revision=revision,
            )

    def test_nonfixture_result_must_match_governance_contract(self) -> None:
        contract = TaskContract(
            task_id="governed-task",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="enforce governance receipt",
            allowed_scope=("src",),
            verification_tier="L1",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec(
                    "work",
                    contract.task_id,
                    "work",
                    "codex",
                    "gpt-5.6-luna",
                    "work",
                ),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            "governed-create",
        )
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("worker", self.epoch)
        with self.assertRaisesRegex(ValueError, "verification tier"):
            self.store.settle_claimed(
                claimed,
                NodeResult(
                    "succeeded",
                    "done",
                    actual_model="gpt-5.6-luna",
                    result_kind="worker",
                    checks=("focused",),
                    verification_tier="L2",
                ),
            )

    def test_cycle_is_rejected(self) -> None:
        nodes = [
            NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok", depends_on=("b",)),
            NodeSpec("b", "task-1", "B", "fixture", "fixture", "ok", depends_on=("a",)),
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-cycle")

    def test_node_scope_cannot_cover_a_forbidden_child(self) -> None:
        contract = TaskContract(
            task_id="scope-task",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="bounded scope",
            allowed_scope=("src",),
            forbidden_scope=("src/private",),
        )
        node = NodeSpec(
            "work",
            "scope-task",
            "work",
            "fixture",
            "fixture",
            "ok",
            write_scopes=("src",),
        )
        with self.assertRaisesRegex(ValueError, "overlaps forbidden"):
            self.store.create_task(contract, [node], "cmd-forbidden")

    def test_scope_conflict_does_not_block_independent_parallel_node(self) -> None:
        nodes = [
            NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok", write_scopes=("src/shared",), ordinal=1),
            NodeSpec("b", "task-1", "B", "fixture", "fixture", "ok", write_scopes=("src/shared",), ordinal=1),
            NodeSpec("c", "task-1", "C", "fixture", "fixture", "ok", write_scopes=("src/independent",), ordinal=1),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-parallel")
        self.store.queue_task("task-1")
        first = self.store.claim_ready_node("worker-1", self.epoch)
        second = self.store.claim_ready_node("worker-2", self.epoch)
        self.assertEqual(first["node_id"], "a")
        self.assertEqual(second["node_id"], "c")
        health = self.store.health()
        self.assertEqual(health["active_executors"], {"fixture": 2})
        self.assertEqual(health["active_models"], {"fixture": 2})
        self.store.settle_claimed(first, NodeResult("succeeded", "ok"))
        third = self.store.claim_ready_node("worker-3", self.epoch)
        self.assertEqual(third["node_id"], "b")

    def test_spark_lane_claim_is_capped_and_records_its_capacity_receipt(self) -> None:
        nodes = [
            NodeSpec("a", "task-1", "A", "codex", "gpt-5.3-codex-spark", "bounded work", write_scopes=("src/a",)),
            NodeSpec("b", "task-1", "B", "codex", "gpt-5.3-codex-spark", "bounded work", write_scopes=("src/b",)),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-spark-cap")
        self.store.queue_task("task-1")

        first = self.store.claim_ready_node(
            "spark-1",
            self.epoch,
            execution_lanes=("spark",),
            lane_capacities={"spark": 1},
        )
        assert first is not None
        self.assertEqual(first["node_id"], "a")
        self.assertEqual(first["spec"]["execution_lane"], "spark")
        self.assertEqual(first["spec"]["quota_pool_id"], "codex-spark")
        self.assertIsNone(
            self.store.claim_ready_node(
                "spark-2",
                self.epoch,
                execution_lanes=("spark",),
                lane_capacities={"spark": 1},
            )
        )
        started = [
            event
            for event in self.store.read_events(task_id="task-1")
            if event["event_type"] == "node.started"
        ][-1]
        self.assertEqual(started["payload"]["execution_lane"], "spark")
        self.assertEqual(started["payload"]["quota_pool_id"], "codex-spark")
        self.assertEqual(started["payload"]["lane_capacity"], 1)
        self.assertEqual(started["payload"]["lane_active_units"], 1)
        self.assertEqual(started["payload"]["claimed_at"], started["created_at"])

        self.store.settle_claimed(
            first,
            NodeResult(
                "succeeded",
                "ok",
                actual_model="gpt-5.3-codex-spark",
                result_kind="worker",
                checks=("fixture-check",),
            ),
        )
        second = self.store.claim_ready_node(
            "spark-2",
            self.epoch,
            execution_lanes=("spark",),
            lane_capacities={"spark": 1},
        )
        assert second is not None
        self.assertEqual(second["node_id"], "b")

    def test_non_spark_model_is_not_admitted_to_spark_lane(self) -> None:
        worker = NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")
        self.store.create_task(self.contract, verified([worker], "task-1"), "cmd-not-spark")
        self._set_node_execution_metadata("task-1", "work", execution_lane="spark")
        self.store.queue_task("task-1")

        self.assertIsNone(
            self.store.claim_ready_node(
                "spark-worker",
                self.epoch,
                execution_lanes=("spark",),
                lane_capacities={"spark": 1},
            )
        )
        general = self.store.claim_ready_node(
            "general-worker",
            self.epoch,
            execution_lanes=("general",),
            lane_capacities={"spark": 1},
        )
        assert general is not None
        self.assertEqual(general["node_id"], "work")
        self.assertEqual(general["spec"]["execution_lane"], "general")

    def test_spark_retry_that_escalates_to_luna_moves_to_general_lane(self) -> None:
        contract = TaskContract(
            task_id="spark-retry",
            repository=self.contract.repository,
            base_sha=self.contract.base_sha,
            objective="retry bounded work",
            allowed_scope=("src",),
            retry_limit=1,
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "worker",
            "codex",
            "gpt-5.3-codex-spark",
            "bounded work",
        )
        self.store.create_task(contract, verified([worker], contract.task_id), "cmd-spark-retry")
        self.store.queue_task(contract.task_id)
        first = self.store.claim_ready_node(
            "spark-1",
            self.epoch,
            execution_lanes=("spark",),
            lane_capacities={"spark": 1},
        )
        assert first is not None
        self.store.settle_claimed(
            first,
            NodeResult(
                "failed",
                "retryable Spark failure",
                actual_model="gpt-5.3-codex-spark",
                retryable=True,
                result_kind="worker",
            ),
        )

        self.assertIsNone(
            self.store.claim_ready_node(
                "spark-2",
                self.epoch,
                execution_lanes=("spark",),
                lane_capacities={"spark": 1},
            )
        )
        retried = self.store.claim_ready_node(
            "general-1",
            self.epoch,
            execution_lanes=("general",),
            lane_capacities={"spark": 1},
        )
        assert retried is not None
        self.assertEqual(retried["spec"]["model"], "gpt-5.6-luna")
        self.assertEqual(retried["spec"]["execution_lane"], "general")

    def test_spark_lane_keeps_dependency_and_scope_gates(self) -> None:
        nodes = [
            NodeSpec("base", "task-1", "base", "codex", "gpt-5.3-codex-spark", "bounded work", write_scopes=("src/shared",), ordinal=1),
            NodeSpec(
                "child",
                "task-1",
                "child",
                "codex",
                "gpt-5.3-codex-spark",
                "bounded work",
                depends_on=("base",),
                write_scopes=("src/child",),
                ordinal=0,
            ),
            NodeSpec("conflict", "task-1", "conflict", "codex", "gpt-5.3-codex-spark", "bounded work", write_scopes=("src/shared",), ordinal=2),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-spark-gates")
        self.store.queue_task("task-1")

        base = self.store.claim_ready_node(
            "spark-1",
            self.epoch,
            execution_lanes=("spark",),
            lane_capacities={"spark": 2},
        )
        assert base is not None
        self.assertEqual(base["node_id"], "base")
        # child still waits for an accepted dependency; conflict still waits
        # for the running writer.  Spark admission does not bypass either gate.
        self.assertIsNone(
            self.store.claim_ready_node(
                "spark-2",
                self.epoch,
                execution_lanes=("spark",),
                lane_capacities={"spark": 2},
            )
        )

    def test_nonparallel_node_serializes_only_its_own_task(self) -> None:
        nodes = [
            NodeSpec(
                "serial",
                "task-1",
                "serial",
                "fixture",
                "fixture",
                "serial work",
                parallelizable=False,
                ordinal=1,
            ),
            NodeSpec(
                "parallel",
                "task-1",
                "parallel",
                "fixture",
                "fixture",
                "parallel work",
                parallelizable=True,
                ordinal=2,
            ),
        ]
        second_contract = TaskContract(
            task_id="task-2",
            repository=self.contract.repository,
            base_sha=self.contract.base_sha,
            objective="independent task",
            allowed_scope=("src",),
        )
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-serial-task")
        self.store.create_task(
            second_contract,
            verified(
                [NodeSpec("other", "task-2", "other", "fixture", "fixture", "other work")],
                "task-2",
            ),
            "cmd-independent-task",
        )
        self.store.queue_task("task-1")
        self.store.queue_task("task-2")
        self.store.set_task_priority("task-1", 1, expected_revision=2)

        first = self.store.claim_ready_node("worker-1", self.epoch)
        self.assertEqual((first["task_id"], first["node_id"]), ("task-1", "serial"))
        second = self.store.claim_ready_node("worker-2", self.epoch)
        self.assertEqual((second["task_id"], second["node_id"]), ("task-2", "other"))
        self.store.settle_claimed(first, NodeResult("succeeded", "serial complete"))
        third = self.store.claim_ready_node("worker-3", self.epoch)
        self.assertEqual((third["task_id"], third["node_id"]), ("task-1", "parallel"))

    def test_priority_orders_ready_tasks_and_steering_reaches_future_attempts(self) -> None:
        second_contract = TaskContract(
            task_id="task-2",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="high priority task",
            allowed_scope=("src",),
        )
        self.store.create_task(
            self.contract,
            verified([NodeSpec("work", "task-1", "low", "fixture", "fixture", "ok")], "task-1"),
            "cmd-low-priority",
        )
        self.store.create_task(
            second_contract,
            verified([NodeSpec("work", "task-2", "high", "fixture", "fixture", "ok")], "task-2"),
            "cmd-high-priority",
        )
        self.store.queue_task("task-1")
        self.store.queue_task("task-2")
        task = self.store.get_task("task-2")
        revision = self.store.set_task_priority(
            "task-2", 7, expected_revision=task["state_revision"]
        )
        revision = self.store.append_task_steering(
            "task-2",
            "先保留现有公开接口，再修改内部实现。",
            expected_revision=revision,
        )

        claimed = self.store.claim_ready_node("priority-worker", self.epoch)
        self.assertEqual(claimed["task_id"], "task-2")
        self.assertEqual(claimed["steering"], ("先保留现有公开接口，再修改内部实现。",))
        task = self.store.get_task("task-2")
        self.assertEqual(task["priority"], 7)
        self.assertEqual(task["state_revision"], revision + 1)
        self.assertEqual(task["steering"][0]["instruction"], "先保留现有公开接口，再修改内部实现。")
        event_types = {event["event_type"] for event in self.store.read_events(task_id="task-2")}
        self.assertIn("task.priority_changed", event_types)
        self.assertIn("task.steering_added", event_types)

    def test_claim_exposes_effective_codex_profile_and_reasoning_effort(self) -> None:
        worker = NodeSpec(
            "worker",
            "task-1",
            "worker",
            "codex",
            "gpt-5.6-luna",
            "perform bounded work",
            write_scopes=("src",),
        )
        self.store.create_task(self.contract, verified([worker], "task-1"), "cmd-luna-profile")
        self.store.queue_task("task-1")

        claimed = self.store.claim_ready_node("worker-1", self.epoch)

        self.assertEqual(claimed["spec"]["model"], "gpt-5.6-luna")
        self.assertEqual(claimed["spec"]["model_profile"], "luna_worker")
        self.assertEqual(claimed["spec"]["model_reasoning_effort"], "max")
        started = [event for event in self.store.read_events(task_id="task-1") if event["event_type"] == "node.started"]
        self.assertEqual(started[-1]["payload"]["model_profile"], "luna_worker")
        self.assertEqual(started[-1]["payload"]["model_reasoning_effort"], "max")

    def test_exact_astra_verifier_is_claimed_and_can_settle(self) -> None:
        contract = TaskContract(
            task_id="astra-store",
            repository=self.contract.repository,
            base_sha=self.contract.base_sha,
            objective="Astra final verification",
            allowed_scope=("src",),
            required_artifacts=(),
            planner_model=CODEX_ASTRA_MODEL,
            verifier_model=CODEX_ASTRA_MODEL,
        )
        nodes = [
            NodeSpec("worker", contract.task_id, "worker", "fixture", "fixture", "ok"),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "codex",
                CODEX_ASTRA_MODEL,
                "independently verify",
                depends_on=("worker",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-astra-verifier")
        self.store.queue_task(contract.task_id)
        worker = self.store.claim_ready_node("fixture-worker", self.epoch)
        self.store.settle_claimed(worker, NodeResult("succeeded", "worker complete"))

        verifier = self.store.claim_ready_node("astra-verifier", self.epoch)
        self.assertEqual(
            (
                verifier["spec"]["model"],
                verifier["spec"]["model_profile"],
                verifier["spec"]["model_reasoning_effort"],
                verifier["spec"]["execution_lane"],
                verifier["spec"]["quota_pool_id"],
            ),
            (CODEX_ASTRA_MODEL, "astra_control_plane", "max", "control", "codex-control"),
        )
        evidence = self.store.artifacts.put_text("Astra verifier evidence", "astra-verifier.json")
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded",
                "Astra accepted",
                actual_model=CODEX_ASTRA_MODEL,
                result_kind="verifier",
                checks=("focused verifier check",),
                evidence=(evidence,),
                verdict="accepted",
            ),
        )
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "accepted")

    def test_retry_after_codex_fallback_keeps_codex_route(self) -> None:
        contract = TaskContract(
            task_id="fallback-retry",
            repository=self.contract.repository,
            base_sha=self.contract.base_sha,
            objective="preserve fallback route across retry",
            allowed_scope=("src",),
            retry_limit=1,
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "worker",
            "claude",
            "sonnet",
            "bounded work",
            read_scopes=("src",),
        )
        self.store.create_task(contract, verified([worker], contract.task_id), "fallback-retry-create")
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("worker-1", self.epoch)
        self.store.record_node_route(
            contract.task_id,
            "worker",
            executor="codex",
            model="gpt-5.6-luna",
            payload={"from": "claude", "to": "codex", "reason": "test fallback"},
            attempt=claimed["attempt"],
            coordinator_epoch=self.epoch,
            lease_epoch=claimed["lease_epoch"],
        )
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "failed",
                "Codex transient failure",
                actual_model="gpt-5.6-luna",
                retryable=True,
                result_kind="worker",
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )

        retried = self.store.claim_ready_node("worker-2", self.epoch)
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(retried["spec"]["executor"], "codex")
        self.assertEqual(retried["spec"]["model"], "gpt-5.6-terra")
        task_node = next(node for node in self.store.get_task(contract.task_id)["nodes"] if node["node_id"] == "worker")
        self.assertEqual(task_node["effective_executor"], "codex")
        self.assertEqual(task_node["effective_model"], "gpt-5.6-terra")

        self.store.settle_claimed(
            retried,
            NodeResult(
                "failed",
                "Codex retry exhausted",
                actual_model="gpt-5.6-terra",
                result_kind="worker",
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )
        self.store.queue_task(contract.task_id)
        manually_retried = self.store.claim_ready_node("worker-3", self.epoch)
        self.assertEqual(manually_retried["spec"]["executor"], "codex")
        self.assertEqual(manually_retried["spec"]["model"], "gpt-5.6-sol")

    def test_routing_v3_retry_keeps_the_pinned_worker_model(self) -> None:
        contract = TaskContract(
            task_id="v3-pinned-retry",
            repository=self.contract.repository,
            base_sha=self.contract.base_sha,
            objective="preserve the admitted v3 capability across retries",
            allowed_scope=("src",),
            retry_limit=1,
            capability_snapshot_id="catalog-20260902-001",
            capability_digest="a" * 64,
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "worker",
            "codex",
            "gpt-5.6-luna",
            "bounded work",
            read_scopes=("src",),
            capability_snapshot_id=contract.capability_snapshot_id,
            capability_digest=contract.capability_digest,
            model_capability_id="codex:gpt-5.6-luna",
            agent_capability_id="codex:0.149.1",
            routing_policy_version="model-routing-v3",
        )
        self.store.create_task(
            contract,
            verified([worker], contract.task_id),
            "v3-pinned-retry-create",
        )
        self.store.queue_task(contract.task_id)
        first = self.store.claim_ready_node("worker-1", self.epoch)
        self.assertEqual(first["spec"]["model"], "gpt-5.6-luna")
        self.store.settle_claimed(
            first,
            NodeResult(
                "failed",
                "transient failure",
                actual_model="gpt-5.6-luna",
                retryable=True,
                result_kind="worker",
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )

        second = self.store.claim_ready_node("worker-2", self.epoch)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["spec"]["model"], "gpt-5.6-luna")
        self.assertEqual(second["spec"]["model_capability_id"], "codex:gpt-5.6-luna")

    def test_archify_validate_and_migrate_persist_host_command_validation_evidence(self) -> None:
        for command in ("validate", "migrate"):
            with self.subTest(command=command):
                task_id = f"archify-{command}"
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(Path(self.temp.name).resolve()),
                    base_sha="abc123",
                    objective="Create an architecture artifact",
                    allowed_scope=("src",),
                    task_type="architecture",
                    complexity="high",
                    verifier_model="fixture",
                )
                worker = NodeSpec(
                    "worker",
                    task_id,
                    "worker",
                    "codex",
                    "gpt-5.6-luna",
                    "create the bounded architecture evidence",
                    read_scopes=("src",),
                    write_scopes=("src",),
                    archify=archify_internal_directive("architecture", True),
                )
                self.store.create_task(contract, verified([worker], task_id), f"{task_id}-create")
                self.store.queue_task(task_id)
                claimed = self.store.claim_ready_node(
                    f"{command}-worker",
                    self.epoch,
                    admissible=lambda spec: not spec.get("verifier"),
                )
                assert claimed is not None
                def binding(path: Path) -> dict[str, object]:
                    data = path.read_bytes()
                    return {
                        "path": str(path.resolve()),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }

                if command == "validate":
                    input_path = Path(self.temp.name) / "validate-input.json"
                    input_path.write_text("{}", encoding="utf-8")
                    receipt = {
                        "command": command,
                        "input": str(input_path.resolve()),
                    }
                    frozen_input = binding(input_path)
                    frozen_source = None
                    frozen_destination = None
                else:
                    source_path = Path(self.temp.name) / "source.workflow.json"
                    destination_path = Path(self.temp.name) / "destination.workflow.json"
                    source_path.write_text("{\"schema_version\":1}", encoding="utf-8")
                    destination_path.write_text("{\"schema_version\":2}", encoding="utf-8")
                    frozen_source = binding(source_path)
                    frozen_destination = binding(destination_path)
                    receipt = {
                        "command": command,
                        "source": frozen_source,
                        "destination": frozen_destination,
                    }
                    frozen_input = None
                receipt_ref = self.store.artifacts.put_text(
                    json.dumps(receipt),
                    "archify-receipt.json",
                )
                stdout = '{"ok":true}\n'
                stderr = ""
                stdout_ref = self.store.artifacts.put_text(stdout, "archify-command.stdout.log")
                stderr_ref = self.store.artifacts.put_text(stderr, "archify-command.stderr.log")
                execution_ref = self.store.artifacts.put_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "archify-executor-command-validation",
                            "receipt_ref": receipt_ref,
                            "receipt_command": command,
                            "frozen_input": frozen_input,
                            "frozen_source": frozen_source,
                            "frozen_destination": frozen_destination,
                            "proof": {
                                "mode": (
                                    "pinned-validate-and-frozen-input-binding"
                                    if command == "validate"
                                    else "pinned-migrate-and-frozen-source-destination-binding"
                                ),
                                "renderer_check": "not-applicable",
                            },
                            "argv": ["/usr/bin/node", "/workbench/archify.mjs", command],
                            "stdout": stdout,
                            "stderr": stderr,
                            "exit_code": 0,
                            "provenance": {"schema_version": 1, "ok": True},
                            "stdout_ref": stdout_ref,
                            "stderr_ref": stderr_ref,
                            "cli_receipt": {"ok": True},
                        }
                    ),
                    "archify-execution.json",
                )
                result = NodeResult(
                    status="succeeded",
                    summary=f"{command} receipt accepted",
                    actual_model="gpt-5.6-luna",
                    result_kind="worker",
                    artifacts={
                        "archify-receipt": receipt_ref,
                        "archify-execution": execution_ref,
                    },
                    checks=(f"archify:{command}",),
                )

                self.store.settle_claimed(claimed, result)

                worker_row = next(
                    node for node in self.store.get_task(task_id)["nodes"] if node["node_id"] == "worker"
                )
                self.assertEqual(worker_row["state"], "accepted")
                self.assertEqual(worker_row["result"]["artifacts"]["archify-execution"], execution_ref)

    def test_archify_legacy_receipt_only_evidence_is_rejected(self) -> None:
        receipt_ref = self.store.artifacts.put_text(
            json.dumps({"command": "validate"}),
            "archify-receipt.json",
        )
        execution_ref = self.store.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "archify-executor-command-evidence",
                    "receipt_ref": receipt_ref,
                    "receipt_command": "validate",
                    "proof": {
                        "mode": "command-specific-receipt",
                        "renderer_check": "not-applicable",
                    },
                    "artifact_checker": {"exit_code": 0},
                }
            ),
            "archify-execution.json",
        )
        result = NodeResult(
            status="succeeded",
            summary="invalid command evidence",
            artifacts={
                "archify-receipt": receipt_ref,
                "archify-execution": execution_ref,
            },
            checks=("archify:validate",),
        )

        with self.assertRaisesRegex(ValueError, "host command-validation envelope"):
            self.store._validate_archify_worker_evidence(result)

    def test_archify_command_validation_rejects_log_mismatch(self) -> None:
        input_path = Path(self.temp.name) / "validate-input.json"
        input_path.write_text("{}", encoding="utf-8")
        receipt_ref = self.store.artifacts.put_text(
            json.dumps({"command": "validate", "input": str(input_path.resolve())}),
            "archify-receipt.json",
        )
        stdout_ref = self.store.artifacts.put_text('{"actual":true}\n', "archify-command.stdout.log")
        stderr_ref = self.store.artifacts.put_text("", "archify-command.stderr.log")
        execution_ref = self.store.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "archify-executor-command-validation",
                    "receipt_ref": receipt_ref,
                    "receipt_command": "validate",
                    "frozen_input": {
                        "path": str(input_path.resolve()),
                        "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                        "bytes": input_path.stat().st_size,
                    },
                    "frozen_source": None,
                    "frozen_destination": None,
                    "proof": {
                        "mode": "pinned-validate-and-frozen-input-binding",
                        "renderer_check": "not-applicable",
                    },
                    "argv": ["/usr/bin/node", "/workbench/archify.mjs", "validate"],
                    "stdout": '{"claimed":true}\n',
                    "stderr": "",
                    "exit_code": 0,
                    "provenance": {"schema_version": 1, "ok": True},
                    "stdout_ref": stdout_ref,
                    "stderr_ref": stderr_ref,
                    "cli_receipt": {"ok": True},
                }
            ),
            "archify-execution.json",
        )
        result = NodeResult(
            status="succeeded",
            summary="invalid command log evidence",
            artifacts={
                "archify-receipt": receipt_ref,
                "archify-execution": execution_ref,
            },
            checks=("archify:validate",),
        )

        with self.assertRaisesRegex(ValueError, "logs do not match"):
            self.store._validate_archify_worker_evidence(result)

    def test_parent_and_child_write_scopes_conflict(self) -> None:
        nodes = [
            NodeSpec("parent", "task-1", "parent", "fixture", "fixture", "ok", write_scopes=("src/parser",), ordinal=1),
            NodeSpec("child", "task-1", "child", "fixture", "fixture", "ok", write_scopes=("src/parser/tokenizer",), ordinal=1),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-parent-child")
        self.store.queue_task("task-1")
        self.assertEqual(self.store.claim_ready_node("worker-1", self.epoch)["node_id"], "child")
        self.assertIsNone(self.store.claim_ready_node("worker-2", self.epoch))

    def test_read_write_parent_child_scopes_conflict(self) -> None:
        nodes = [
            NodeSpec("reader", "task-1", "reader", "fixture", "fixture", "ok", read_scopes=("src/parser",), ordinal=1),
            NodeSpec("writer", "task-1", "writer", "fixture", "fixture", "ok", write_scopes=("src/parser/tokenizer",), ordinal=2),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-read-write")
        self.store.queue_task("task-1")
        self.assertEqual(self.store.claim_ready_node("worker-1", self.epoch)["node_id"], "reader")
        self.assertIsNone(self.store.claim_ready_node("worker-2", self.epoch))

    def test_read_only_nodes_on_same_scope_can_run_in_parallel(self) -> None:
        nodes = [
            NodeSpec("reader-a", "task-1", "reader A", "fixture", "fixture", "ok", read_scopes=("src/parser",), ordinal=1),
            NodeSpec("reader-b", "task-1", "reader B", "fixture", "fixture", "ok", read_scopes=("src/parser/tokenizer",), ordinal=1),
        ]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-read-read")
        self.store.queue_task("task-1")
        claimed = {
            self.store.claim_ready_node("worker-1", self.epoch)["node_id"],
            self.store.claim_ready_node("worker-2", self.epoch)["node_id"],
        }
        self.assertEqual(claimed, {"reader-a", "reader-b"})

    def test_only_verifier_accepts_task(self) -> None:
        nodes = [
            NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok"),
            NodeSpec(
                "verify",
                "task-1",
                "verify",
                "fixture",
                "fixture",
                "accepted",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(self.contract, nodes, "cmd-verify")
        self.store.queue_task("task-1")
        work = self.store.claim_ready_node("worker-1", self.epoch)
        self.store.settle_claimed(work, NodeResult("succeeded", "worker done"))
        self.assertEqual(self.store.get_task("task-1")["state"], "verifying")
        verify = self.store.claim_ready_node("verifier", self.epoch)
        self.store.settle_claimed(verify, NodeResult("succeeded", "independent verdict"))
        task = self.store.get_task("task-1")
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(task["verdict"], "independent verdict")

    def test_real_task_missing_required_evidence_is_rejected(self) -> None:
        contract = TaskContract(
            task_id="missing-evidence",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="enforce acceptance evidence",
            allowed_scope=("src",),
            retry_limit=0,
        )
        nodes = [
            NodeSpec(
                "work",
                contract.task_id,
                "work",
                "codex",
                "gpt-5.6-luna",
                "work",
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "codex",
                "gpt-5.6-sol",
                "verify",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-missing-evidence")
        self.store.queue_task(contract.task_id)
        worker = self.store.claim_ready_node("worker", self.epoch)
        self.store.settle_claimed(
            worker,
            NodeResult(
                "succeeded", "worker done", actual_model="gpt-5.6-luna",
                result_kind="worker", checks=("check",),
            ),
        )
        verifier = self.store.claim_ready_node("verifier", self.epoch)
        evidence_ref = self.store.artifacts.put_text("verifier evidence", "result.json")
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded", "verifier accepted", actual_model="gpt-5.6-sol",
                result_kind="verifier", checks=("check",), evidence=(evidence_ref,),
                verdict="accepted",
            ),
        )
        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "needs_fix")
        self.assertIn("diff, test-log", task["blocker"])

    def test_real_task_with_required_evidence_is_accepted(self) -> None:
        contract = TaskContract(
            task_id="complete-evidence",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="accept complete evidence",
            allowed_scope=("src",),
        )
        nodes = [
            NodeSpec(
                "work",
                contract.task_id,
                "work",
                "codex",
                "gpt-5.6-luna",
                "work",
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "codex",
                "gpt-5.6-sol",
                "verify",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-complete-evidence")
        self.store.queue_task(contract.task_id)
        patch_ref = self.store.artifacts.put_text("patch", "patch")
        test_ref = self.store.artifacts.put_text("test", "stdout.log")
        worker = self.store.claim_ready_node("worker", self.epoch)
        self.store.settle_claimed(
            worker,
            NodeResult(
                "succeeded", "worker done", artifacts={"patch": patch_ref},
                actual_model="gpt-5.6-luna", result_kind="worker", checks=("check",),
            ),
        )
        verifier = self.store.claim_ready_node("verifier", self.epoch)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded", "verifier accepted", artifacts={"test-log": test_ref},
                actual_model="gpt-5.6-sol", result_kind="verifier",
                checks=("check",), evidence=(test_ref,), verdict="accepted",
            ),
        )
        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(task["verdict"], "verifier accepted")

    def test_claude_full_model_id_may_settle_a_matching_family_lease(self) -> None:
        contract = TaskContract(
            task_id="claude-model-evidence",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="preserve actual Claude model evidence",
            allowed_scope=("src",),
            verifier_model="fixture",
        )
        nodes = [
            NodeSpec(
                "work",
                contract.task_id,
                "work",
                "claude",
                "sonnet",
                "work",
                write_scopes=("src",),
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "fixture",
                "fixture",
                "accepted",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-claude-model-evidence")
        self.store.queue_task(contract.task_id)
        worker = self.store.claim_ready_node("claude-worker", self.epoch)
        self.store.settle_claimed(
            worker,
            NodeResult(
                "succeeded",
                "worker done",
                actual_model="claude-sonnet-4-5-20250929",
                result_kind="worker",
                checks=("structured-result",),
            ),
        )
        settled_work = next(
            node
            for node in self.store.get_task(contract.task_id)["nodes"]
            if node["node_id"] == "work"
        )
        self.assertEqual(settled_work["result"]["actual_model"], "claude-sonnet-4-5-20250929")
        verifier = self.store.claim_ready_node("fixture-verifier", self.epoch)
        self.assertEqual(verifier["task_id"], contract.task_id)
        self.store.settle_claimed(verifier, NodeResult("succeeded", "accepted"))

        mismatch_contract = TaskContract(
            task_id="claude-model-mismatch",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="reject a different Claude family",
            allowed_scope=("src",),
            verifier_model="fixture",
        )
        mismatch_nodes = [
            NodeSpec(
                "work",
                mismatch_contract.task_id,
                "work",
                "claude",
                "sonnet",
                "work",
                write_scopes=("src",),
            ),
            NodeSpec(
                "verify",
                mismatch_contract.task_id,
                "verify",
                "fixture",
                "fixture",
                "accepted",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(mismatch_contract, mismatch_nodes, "cmd-claude-model-mismatch")
        self.store.queue_task(mismatch_contract.task_id)
        mismatch = self.store.claim_ready_node("claude-worker", self.epoch)
        with self.assertRaisesRegex(ValueError, "does not match leased model"):
            self.store.settle_claimed(
                mismatch,
                NodeResult(
                    "succeeded",
                    "wrong family",
                    actual_model="claude-opus-4-1-20250805",
                    result_kind="worker",
                    checks=("structured-result",),
                ),
            )

    def test_restart_marks_running_node_indeterminate(self) -> None:
        nodes = [NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-recover")
        self.store.queue_task("task-1")
        self.store.claim_ready_node("worker-1", self.epoch)
        self.assertEqual(self.store.recover_interrupted(), 1)
        task = self.store.get_task("task-1")
        self.assertEqual(task["state"], "needs_approval")
        work = next(node for node in task["nodes"] if node["node_id"] == "work")
        self.assertEqual(work["state"], "indeterminate")
        approvals = self.store.list_approvals()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["task_id"], "task-1")
        self.assertEqual(approvals[0]["request"]["node_id"], "work")
        revision = self.store.decide_approval(
            approvals[0]["approval_id"],
            "retry",
            expected_revision=task["state_revision"],
        )
        retried = self.store.get_task("task-1")
        self.assertEqual(retried["state_revision"], revision)
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["nodes"][0]["state"], "pending")
        decided = self.store.list_approvals(pending_only=False)[0]
        self.assertEqual(decided["decision"], "retry")
        self.assertEqual(
            self.store.decide_approval(
                decided["approval_id"], "retry", expected_revision=task["state_revision"]
            ),
            revision,
        )
        with self.assertRaises(StateConflictError):
            self.store.decide_approval(
                decided["approval_id"], "fail", expected_revision=revision
            )

    def test_indeterminate_settlement_creates_one_durable_approval(self) -> None:
        nodes = [NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")]
        self.store.create_task(self.contract, verified(nodes, "task-1"), "cmd-indeterminate")
        self.store.queue_task("task-1")
        claim = self.store.claim_ready_node("worker-1", self.epoch)
        self.store.settle_claimed(
            claim,
            NodeResult("indeterminate", "worker outcome unknown"),
        )

        approvals = self.store.list_approvals()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["kind"], "indeterminate_resolution")
        self.assertEqual(approvals[0]["request"]["attempt"], 1)
        self.assertEqual(approvals[0]["request"]["allowed_decisions"], ["retry", "fail", "cancel"])
        event_types = {event["event_type"] for event in self.store.read_events(task_id="task-1")}
        self.assertIn("approval.requested", event_types)


if __name__ == "__main__":
    unittest.main()
