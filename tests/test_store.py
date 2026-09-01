from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_workbench.model import NodeResult, NodeSpec, TaskContract
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
        self.assertEqual(self.store.health()["schema_version"], 9)
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
        self.assertEqual(migrated.health()["schema_version"], 9)
        with migrated.connection() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
        self.assertIn("effective_executor", columns)
        self.assertIn("effective_model", columns)

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
