from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import CommandConflictError, WorkbenchStore


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

    def test_schema_one_migrates_to_delivery_schema(self) -> None:
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
            connection.execute("DROP TABLE delivery_receipts")
        self.store.initialize()
        self.assertEqual(self.store.health()["schema_version"], 3)

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
        self.store.settle_node("task-1", "a", NodeResult("succeeded", "ok"))
        third = self.store.claim_ready_node("worker-3")
        self.assertEqual(third["node_id"], "b")

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

    def test_restart_marks_running_node_indeterminate(self) -> None:
        nodes = [NodeSpec("work", "task-1", "work", "fixture", "fixture", "ok")]
        self.store.create_task(self.contract, nodes, "cmd-recover")
        self.store.queue_task("task-1")
        self.store.claim_ready_node("worker-1")
        self.assertEqual(self.store.recover_interrupted(), 1)
        task = self.store.get_task("task-1")
        self.assertEqual(task["state"], "needs_approval")
        self.assertEqual(task["nodes"][0]["state"], "indeterminate")
        revision = self.store.resolve_indeterminate(
            "task-1", "work", "retry", expected_revision=task["state_revision"]
        )
        retried = self.store.get_task("task-1")
        self.assertEqual(retried["state_revision"], revision)
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["nodes"][0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
