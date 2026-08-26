from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        self.contract = TaskContract(
            task_id="task-1",
            repository=str(Path(self.temp.name).resolve()),
            base_sha="abc123",
            objective="test persistent DAG",
            allowed_scope=("src",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_idempotent_submit_and_command_conflict(self) -> None:
        nodes = [NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok")]
        self.assertEqual(self.store.create_task(self.contract, nodes, "cmd-1"), "task-1")
        self.assertEqual(self.store.create_task(self.contract, nodes, "cmd-1"), "task-1")
        changed = [NodeSpec("a", "task-1", "changed", "fixture", "fixture", "ok")]
        with self.assertRaises(CommandConflictError):
            self.store.create_task(self.contract, changed, "cmd-1")

    def test_schema_one_migrates_to_current_schema(self) -> None:
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
            connection.execute("DROP TABLE delivery_receipts")
        self.store.initialize()
        self.assertEqual(self.store.health()["schema_version"], 5)
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
        self.assertEqual(migrated.health()["schema_version"], 5)
        with migrated.connection() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
        self.assertIn("effective_executor", columns)
        self.assertIn("effective_model", columns)

    def test_cycle_is_rejected(self) -> None:
        nodes = [
            NodeSpec("a", "task-1", "A", "fixture", "fixture", "ok", depends_on=("b",)),
            NodeSpec("b", "task-1", "B", "fixture", "fixture", "ok", depends_on=("a",)),
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.create_task(self.contract, nodes, "cmd-cycle")

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
        self.store.create_task(self.contract, nodes, "cmd-parallel")
        self.store.queue_task("task-1")
        first = self.store.claim_ready_node("worker-1")
        second = self.store.claim_ready_node("worker-2")
        self.assertEqual(first["node_id"], "a")
        self.assertEqual(second["node_id"], "c")
        health = self.store.health()
        self.assertEqual(health["active_executors"], {"fixture": 2})
        self.assertEqual(health["active_models"], {"fixture": 2})
        self.store.settle_node("task-1", "a", NodeResult("succeeded", "ok"))
        third = self.store.claim_ready_node("worker-3")
        self.assertEqual(third["node_id"], "b")

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
            [NodeSpec("work", "task-1", "low", "fixture", "fixture", "ok")],
            "cmd-low-priority",
        )
        self.store.create_task(
            second_contract,
            [NodeSpec("work", "task-2", "high", "fixture", "fixture", "ok")],
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

        claimed = self.store.claim_ready_node("priority-worker")
        self.assertEqual(claimed["task_id"], "task-2")
        self.assertEqual(claimed["steering"], ("先保留现有公开接口，再修改内部实现。",))
        task = self.store.get_task("task-2")
        self.assertEqual(task["priority"], 7)
        self.assertEqual(task["state_revision"], revision + 1)
        self.assertEqual(task["steering"][0]["instruction"], "先保留现有公开接口，再修改内部实现。")
        event_types = {event["event_type"] for event in self.store.read_events(task_id="task-2")}
        self.assertIn("task.priority_changed", event_types)
        self.assertIn("task.steering_added", event_types)

    def test_parent_and_child_write_scopes_conflict(self) -> None:
        nodes = [
            NodeSpec("parent", "task-1", "parent", "fixture", "fixture", "ok", write_scopes=("src/parser",), ordinal=1),
            NodeSpec("child", "task-1", "child", "fixture", "fixture", "ok", write_scopes=("src/parser/tokenizer",), ordinal=1),
        ]
        self.store.create_task(self.contract, nodes, "cmd-parent-child")
        self.store.queue_task("task-1")
        self.assertEqual(self.store.claim_ready_node("worker-1")["node_id"], "child")
        self.assertIsNone(self.store.claim_ready_node("worker-2"))

    def test_read_write_parent_child_scopes_conflict(self) -> None:
        nodes = [
            NodeSpec("reader", "task-1", "reader", "fixture", "fixture", "ok", read_scopes=("src/parser",), ordinal=1),
            NodeSpec("writer", "task-1", "writer", "fixture", "fixture", "ok", write_scopes=("src/parser/tokenizer",), ordinal=2),
        ]
        self.store.create_task(self.contract, nodes, "cmd-read-write")
        self.store.queue_task("task-1")
        self.assertEqual(self.store.claim_ready_node("worker-1")["node_id"], "reader")
        self.assertIsNone(self.store.claim_ready_node("worker-2"))

    def test_read_only_nodes_on_same_scope_can_run_in_parallel(self) -> None:
        nodes = [
            NodeSpec("reader-a", "task-1", "reader A", "fixture", "fixture", "ok", read_scopes=("src/parser",), ordinal=1),
            NodeSpec("reader-b", "task-1", "reader B", "fixture", "fixture", "ok", read_scopes=("src/parser/tokenizer",), ordinal=1),
        ]
        self.store.create_task(self.contract, nodes, "cmd-read-read")
        self.store.queue_task("task-1")
        claimed = {
            self.store.claim_ready_node("worker-1")["node_id"],
            self.store.claim_ready_node("worker-2")["node_id"],
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
        work = self.store.claim_ready_node("worker-1")
        self.store.settle_node("task-1", work["node_id"], NodeResult("succeeded", "worker done"))
        self.assertEqual(self.store.get_task("task-1")["state"], "verifying")
        verify = self.store.claim_ready_node("verifier")
        self.store.settle_node("task-1", verify["node_id"], NodeResult("succeeded", "independent verdict"))
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
        )
        nodes = [
            NodeSpec(
                "work",
                contract.task_id,
                "work",
                "deterministic",
                "local",
                command=("true",),
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "deterministic",
                "local",
                command=("true",),
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-missing-evidence")
        self.store.queue_task(contract.task_id)
        self.store.settle_node(
            contract.task_id,
            self.store.claim_ready_node("worker")["node_id"],
            NodeResult("succeeded", "worker done"),
        )
        self.store.settle_node(
            contract.task_id,
            self.store.claim_ready_node("verifier")["node_id"],
            NodeResult("succeeded", "verifier accepted"),
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
                "deterministic",
                "local",
                command=("true",),
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "deterministic",
                "local",
                command=("true",),
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "cmd-complete-evidence")
        self.store.queue_task(contract.task_id)
        self.store.settle_node(
            contract.task_id,
            self.store.claim_ready_node("worker")["node_id"],
            NodeResult("succeeded", "worker done", artifacts={"patch": "sha256:patch"}),
        )
        self.store.settle_node(
            contract.task_id,
            self.store.claim_ready_node("verifier")["node_id"],
            NodeResult("succeeded", "verifier accepted", artifacts={"test-log": "sha256:test"}),
        )
        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(task["verdict"], "verifier accepted")

    def test_restart_marks_running_node_indeterminate(self) -> None:
        nodes = [NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")]
        self.store.create_task(self.contract, nodes, "cmd-recover")
        self.store.queue_task("task-1")
        self.store.claim_ready_node("worker-1")
        self.assertEqual(self.store.recover_interrupted(), 1)
        task = self.store.get_task("task-1")
        self.assertEqual(task["state"], "needs_approval")
        self.assertEqual(task["nodes"][0]["state"], "indeterminate")
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
        self.store.create_task(self.contract, nodes, "cmd-indeterminate")
        self.store.queue_task("task-1")
        self.store.claim_ready_node("worker-1")
        self.store.settle_node(
            "task-1",
            "work",
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
