from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.dirty_worktree_recovery import (
    DirtyWorktreeRecoveryError,
    observed_indeterminate_recovery_paths,
)
from codex_workbench.executors import ExecutionRequest
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeError, WorktreeManager


class _FailedAttemptRecoveryFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repository, check=True)
        (self.repository / ".gitignore").write_text(
            ".workbench-ignored/\n__pycache__/\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "src/value.txt"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repository, check=True, capture_output=True)
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.config = WorkbenchConfig(
            self.state_root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("failed-attempt-fixture", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.artifacts = ArtifactStore(self.state_root / "artifacts")
        self.mcp = WorkbenchMCPServer(self.config, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _call_control(self, arguments: dict) -> dict:
        response = self.mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workbench_control_task", "arguments": arguments},
            }
        )
        assert response is not None
        return response["result"]

    def _dirty_failed_task(
        self,
        *,
        task_id: str = "failed-attempt",
        unsafe_path: str | None = None,
        symlink_path: str | None = None,
        retryable: bool = False,
        python_residue: bool = False,
        result_status: str = "failed",
        external_write_permission: bool = False,
        destructive_action_permission: bool = False,
    ) -> tuple[TaskContract, Path]:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="resume a dirty failed worker without rerunning accepted ancestors",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
            external_write_permission=external_write_permission,
            destructive_action_permission=destructive_action_permission,
        )
        ancestor = NodeSpec(
            "ancestor",
            contract.task_id,
            "create immutable inherited input",
            "fixture",
            "fixture",
            "fixture ancestor",
            write_scopes=("src",),
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "continue the failed worker from its own patch",
            "fixture",
            "fixture",
            "fixture worker",
            depends_on=(ancestor.node_id,),
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify",
            contract.task_id,
            "fixture final verifier",
            "fixture",
            "fixture",
            "accepted",
            depends_on=(ancestor.node_id, worker.node_id),
            verifier=True,
        )
        self.store.create_task(
            contract,
            [ancestor, worker, verifier],
            f"{task_id}-create",
        )
        self.store.queue_task(contract.task_id)

        claimed_ancestor = self.store.claim_ready_node("ancestor-worker", self.epoch)
        assert claimed_ancestor is not None
        ancestor_tree = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            contract.task_id,
            ancestor.node_id,
            int(claimed_ancestor["attempt"]),
        )
        self.store.assign_worktree(
            contract.task_id,
            ancestor.node_id,
            str(ancestor_tree),
            attempt=int(claimed_ancestor["attempt"]),
            coordinator_epoch=int(claimed_ancestor["coordinator_epoch"]),
            lease_epoch=int(claimed_ancestor["lease_epoch"]),
        )
        (ancestor_tree / "src" / "ancestor.txt").write_text("accepted ancestor\n", encoding="utf-8")
        ancestor_patch = self.artifacts.put_bytes(
            self.worktrees.diff_patch(ancestor_tree, contract.base_sha), "patch"
        )
        self.store.settle_claimed(
            claimed_ancestor,
            NodeResult(
                "succeeded",
                "ancestor accepted",
                artifacts={"patch": ancestor_patch},
                changed_paths=("src/ancestor.txt",),
                checks=("fixture ancestor",),
            ),
        )

        claimed_worker = self.store.claim_ready_node("failed-worker", self.epoch)
        assert claimed_worker is not None
        source = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            contract.task_id,
            worker.node_id,
            int(claimed_worker["attempt"]),
        )
        self.store.assign_worktree(
            contract.task_id,
            worker.node_id,
            str(source),
            attempt=int(claimed_worker["attempt"]),
            coordinator_epoch=int(claimed_worker["coordinator_epoch"]),
            lease_epoch=int(claimed_worker["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(contract.task_id),
            worker.node_id,
            source,
            self.artifacts,
            self.worktrees,
        )
        assert dependency_input is not None
        dependency_ref = self.artifacts.put_text(
            json.dumps(dependency_input.receipt, sort_keys=True), "dependency-input.json"
        )
        changed_path = unsafe_path or "src/value.txt"
        target = source / changed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("dirty prior attempt\n", encoding="utf-8")
        changed_paths = [changed_path]
        if unsafe_path is None:
            continuation = source / "src" / "continuation.txt"
            continuation.write_text("untracked prior attempt\n", encoding="utf-8")
            changed_paths.append("src/continuation.txt")
        if symlink_path is not None:
            link = source / symlink_path
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to("value.txt")
            changed_paths.append(symlink_path)
        if python_residue:
            residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
            residue.parent.mkdir(parents=True)
            residue.write_bytes(b"failed-attempt bytecode\0")
            changed_paths.append("tests/__pycache__/fixture.cpython-313.pyc")
        self.store.settle_claimed(
            claimed_worker,
            NodeResult(
                result_status,  # type: ignore[arg-type]
                "fixture failed after a recoverable dirty change",
                artifacts={"dependency-input": dependency_ref},
                changed_paths=tuple(sorted(changed_paths)),
                checks=("fixture failed",),
                retryable=retryable,
            ),
        )
        return contract, source

class FailedAttemptRecoveryTests(_FailedAttemptRecoveryFixture, unittest.TestCase):
    def test_retry_restores_tracked_and_untracked_changes_without_rerunning_accepted_ancestors(self) -> None:
        contract, source = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}

        def execute(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            observed["node_id"] = request.node_id  # type: ignore[attr-defined]
            observed["attempt"] = request.attempt  # type: ignore[attr-defined]
            observed["ancestor"] = (worktree / "src" / "ancestor.txt").read_text(encoding="utf-8")
            observed["tracked"] = (worktree / "src" / "value.txt").read_text(encoding="utf-8")
            observed["untracked"] = (worktree / "src" / "continuation.txt").read_text(encoding="utf-8")
            return NodeResult("succeeded", "fake executor continued recovered worker", checks=("fake",))

        try:
            claimed = coordinator._claim_next_ready_node("retry-worker")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))
            self.assertIn("failed_attempt_recovery", claimed)
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "node_id": "worker",
                "attempt": 2,
                "ancestor": "accepted ancestor\n",
                "tracked": "dirty prior attempt\n",
                "untracked": "untracked prior attempt\n",
            },
        )
        task = self.store.get_task(contract.task_id)
        ancestor = next(node for node in task["nodes"] if node["node_id"] == "ancestor")
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((ancestor["state"], ancestor["attempt"]), ("accepted", 1))
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual((source / "src" / "value.txt").read_text(encoding="utf-8"), "dirty prior attempt\n")
        self.assertEqual(
            (source / "src" / "continuation.txt").read_text(encoding="utf-8"),
            "untracked prior attempt\n",
        )
        allocations = self.store.list_worktree_allocations()
        source_allocation = next(
            item for item in allocations if item["node_id"] == "worker" and item["attempt"] == 1
        )
        self.assertEqual(source_allocation["state"], "superseded")

    def test_stale_or_invalid_instruction_cannot_queue_or_launch(self) -> None:
        contract = TaskContract(
            task_id="mcp-queue",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="exercise atomic MCP queue control",
            allowed_scope=("src",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", contract.task_id, "work", "fixture", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "mcp-queue-create",
        )
        revision = int(self.store.get_task(contract.task_id)["state_revision"])
        stale = self._call_control(
            {
                "task_id": contract.task_id,
                "action": "queue",
                "instruction": "valid but stale",
                "expected_revision": revision + 1,
            }
        )
        self.assertTrue(stale["isError"])
        invalid = self._call_control(
            {
                "task_id": contract.task_id,
                "action": "queue",
                "instruction": "   ",
                "expected_revision": revision,
            }
        )
        self.assertTrue(invalid["isError"])
        unchanged = self.store.get_task(contract.task_id)
        self.assertEqual((unchanged["state"], unchanged["state_revision"]), ("inbox", revision))
        self.assertIsNone(self.store.claim_ready_node("must-not-launch", self.epoch))

        queued = self._call_control(
            {
                "task_id": contract.task_id,
                "action": "queue",
                "instruction": "queue only after this instruction is valid",
                "expected_revision": revision,
            }
        )
        receipt = json.loads(queued["content"][0]["text"])
        self.assertEqual(receipt["state"], "queued")
        self.assertEqual(receipt["steering"]["delivery"]["mode"], "future_attempts_only")

    def test_unsafe_recovery_is_rejected_before_queueing(self) -> None:
        contract, _ = self._dirty_failed_task(unsafe_path="outside.txt")
        failed = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "outside task scope"):
            self.store.queue_task(
                contract.task_id,
                expected_revision=int(failed["state_revision"]),
            )
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"]), ("needs_fix", "failed"))

    def test_capture_uncertainty_rejects_the_retry_before_fake_executor_dispatch(self) -> None:
        contract, source = self._dirty_failed_task()
        # This file was not present in the failed result receipt. The service
        # must preserve a1 and reject recovery rather than selectively copying
        # only the reported paths into a2.
        (source / "src" / "late-unreported.txt").write_text("uncertain\n", encoding="utf-8")
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("uncertain-retry-worker")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("unsafe recovery must not dispatch a fake executor"),
            ):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 1))
        self.assertEqual(worker["worktree"], str(source))
        self.assertEqual(
            (source / "src" / "late-unreported.txt").read_text(encoding="utf-8"),
            "uncertain\n",
        )
        events = self.store.read_events(task_id=contract.task_id)
        self.assertIn("node.failed_attempt_recovery_rejected", {event["event_type"] for event in events})

    def test_ignored_source_path_is_reported_and_never_dispatched(self) -> None:
        contract, source = self._dirty_failed_task()
        ignored = source / ".workbench-ignored" / "private.cache"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("must not be copied or discarded\n", encoding="utf-8")
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("ignored-source")
            assert claimed is not None
            with patch.object(coordinator, "_executor") as executor:
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 1))
        self.assertEqual(worker["worktree"], str(source))
        self.assertEqual(
            ignored.read_text(encoding="utf-8"),
            "must not be copied or discarded\n",
        )
        self.assertIn("ignored paths", str(task["blocker"]))

    def test_untracked_symlink_is_preserved_in_source_and_never_dispatched(self) -> None:
        contract, source = self._dirty_failed_task(
            task_id="failed-attempt-symlink",
            symlink_path="src/shortcut.txt",
        )
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("symlink-source")
            assert claimed is not None
            with patch.object(coordinator, "_executor") as executor:
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 1))
        self.assertEqual(worker["worktree"], str(source))
        self.assertTrue((source / "src" / "shortcut.txt").is_symlink())
        self.assertIn("regular file", str(task["blocker"]))

    def test_running_steering_receipt_never_claims_current_delivery(self) -> None:
        contract = TaskContract(
            task_id="running-steering",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="prove steering receipt remains honest",
            allowed_scope=("src",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", contract.task_id, "work", "fixture", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "running-steering-create",
        )
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("running-worker", self.epoch)
        assert claimed is not None
        current = self.store.get_task(contract.task_id)
        response = self._call_control(
            {
                "task_id": contract.task_id,
                "action": "steer",
                "instruction": "apply this only to a future attempt",
                "expected_revision": int(current["state_revision"]),
            }
        )
        receipt = json.loads(response["content"][0]["text"])
        delivery = receipt["delivery"]
        self.assertEqual(delivery["mode"], "future_attempts_only")
        self.assertEqual(delivery["status"], "not_delivered")
        self.assertEqual(delivery["scheduled_for"], "future_attempt")
        self.assertFalse(delivery["current_attempt_received"])
        self.assertFalse(delivery["current_running_attempt_delivered"])
        self.assertEqual(delivery["running_attempts"], [{"node_id": "worker", "attempt": 1}])
        self.assertEqual(claimed["steering"], ())

    def test_retryable_dirty_failure_queues_the_same_recovery_path(self) -> None:
        contract, source = self._dirty_failed_task(retryable=True, python_residue=True)
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")

        self.assertEqual(task["state"], "queued")
        self.assertEqual((worker["state"], worker["attempt"], worker["worktree"]), ("pending", 1, None))
        self.assertTrue((source / "tests" / "__pycache__" / "fixture.cpython-313.pyc").exists())
        observed: dict[str, str] = {}
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("automatic-retry")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))
            self.assertEqual(
                claimed["failed_attempt_recovery"]["source"]["worktree"],
                str(source),
            )

            def execute(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["tracked"] = (worktree / "src" / "value.txt").read_text(
                    encoding="utf-8"
                )
                observed["untracked"] = (worktree / "src" / "continuation.txt").read_text(
                    encoding="utf-8"
                )
                return NodeResult(
                    "succeeded",
                    "automatic retry continued the recovered worker",
                    checks=("fixture",),
                )

            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "tracked": "dirty prior attempt\n",
                "untracked": "untracked prior attempt\n",
            },
        )
        self.assertFalse((source / "tests" / "__pycache__").exists())
        completed = self.store.get_task(contract.task_id)
        completed_worker = next(
            node for node in completed["nodes"] if node["node_id"] == "worker"
        )
        self.assertEqual((completed_worker["state"], completed_worker["attempt"]), ("accepted", 2))

    def test_typed_retryable_block_restores_changes_instead_of_becoming_terminal(self) -> None:
        contract, source = self._dirty_failed_task(
            task_id="retryable-blocked-attempt",
            retryable=True,
            python_residue=True,
            result_status="blocked",
        )
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"]), ("queued", "pending"))

        observed: dict[str, str] = {}
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("typed-blocked-retry")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))

            def execute(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["tracked"] = (worktree / "src" / "value.txt").read_text(
                    encoding="utf-8"
                )
                observed["untracked"] = (worktree / "src" / "continuation.txt").read_text(
                    encoding="utf-8"
                )
                return NodeResult(
                    "succeeded",
                    "typed blocked retry continued from the captured patch",
                    checks=("fixture",),
                )

            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "tracked": "dirty prior attempt\n",
                "untracked": "untracked prior attempt\n",
            },
        )
        self.assertFalse((source / "tests" / "__pycache__").exists())
        completed = self.store.get_task(contract.task_id)
        completed_worker = next(
            node for node in completed["nodes"] if node["node_id"] == "worker"
        )
        self.assertEqual((completed_worker["state"], completed_worker["attempt"]), ("accepted", 2))

    def test_coordinator_marks_only_safe_local_partial_blocks_retryable(self) -> None:
        contract, source = self._dirty_failed_task(
            task_id="coordinator-classified-block",
            python_residue=True,
        )
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        dependency_ref = worker["result"]["artifacts"]["dependency-input"]
        dependency_receipt = json.loads(
            self.artifacts.verify(dependency_ref).read_text(encoding="utf-8")
        )
        request = ExecutionRequest(
            task_id=contract.task_id,
            node_id="worker",
            attempt=1,
            contract=contract.to_dict(),
            spec=worker,
            worktree=source,
            input_tree_sha=dependency_receipt["input_tree_sha"],
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            classified = coordinator._with_observed_failure_paths(
                request,
                NodeResult("blocked", "local worker stopped after making progress"),
            )
            external_request = ExecutionRequest(
                **{
                    **request.__dict__,
                    "contract": {
                        **request.contract,
                        "external_write_permission": True,
                    },
                }
            )
            external = coordinator._with_observed_failure_paths(
                external_request,
                NodeResult("blocked", "external operation may have run"),
            )
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertTrue(classified.retryable)
        self.assertIn("src/value.txt", classified.changed_paths)
        self.assertIn(
            "tests/__pycache__/fixture.cpython-313.pyc",
            classified.changed_paths,
        )
        self.assertFalse(external.retryable)

    def test_incompatible_source_allocation_blocks_queue_without_mutation(self) -> None:
        contract, _ = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations
                SET branch = 'incompatible-source'
                WHERE task_id = ? AND node_id = 'worker' AND attempt = 1
                """,
                (contract.task_id,),
            )
        before = self.store.get_task(contract.task_id)

        with self.assertRaisesRegex(StateConflictError, "branch does not match"):
            self.store.queue_task(
                contract.task_id,
                expected_revision=int(failed["state_revision"]),
            )
        self.assertEqual(self.store.get_task(contract.task_id), before)

    def test_tampered_recovery_patch_hash_is_rejected_before_dispatch(self) -> None:
        contract, source = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        original_capture = coordinator.failed_attempt_recovery.capture

        def tampered_capture(**kwargs: object) -> dict[str, object]:
            recovery = original_capture(**kwargs)
            return {**recovery, "patch_sha256": "0" * 64}

        try:
            claimed = coordinator._claim_next_ready_node("tampered-recovery")
            assert claimed is not None
            with (
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "capture",
                    side_effect=tampered_capture,
                ),
                patch.object(coordinator, "_executor") as executor,
            ):
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 1))
        self.assertEqual(worker["worktree"], str(source))
        self.assertEqual(
            (source / "src" / "continuation.txt").read_text(encoding="utf-8"),
            "untracked prior attempt\n",
        )
        target = self.state_root / "worktrees" / contract.task_id / "worker-a2"
        self.assertFalse(target.exists())

    def test_duplicate_queue_and_old_attempt_settlement_do_not_mutate_retry(self) -> None:
        contract, _ = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        revision = int(failed["state_revision"])
        self.store.queue_task(contract.task_id, expected_revision=revision)
        queued = self.store.get_task(contract.task_id)

        with self.assertRaises(StateConflictError):
            self.store.queue_task(contract.task_id, expected_revision=revision)
        started = next(
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.started" and event["node_id"] == "worker"
        )
        with self.assertRaises(StateConflictError):
            self.store.settle_node(
                contract.task_id,
                "worker",
                NodeResult("succeeded", "late stale result", checks=("late",)),
                attempt=1,
                coordinator_epoch=int(started["payload"]["coordinator_epoch"]),
                lease_epoch=int(started["payload"]["lease_epoch"]),
            )
        self.assertEqual(self.store.get_task(contract.task_id), queued)
        recovery_events = [
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.failed_attempt_recovery_queued"
        ]
        self.assertEqual(len(recovery_events), 1)

    def test_steering_delivery_is_recorded_only_for_the_attempt_that_received_it(self) -> None:
        contract = TaskContract(
            task_id="steering-delivery",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="bind steering to exact attempts",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", contract.task_id, "work", "fixture", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "steering-delivery-create",
        )
        queued = self.store.queue_task_with_instruction(
            contract.task_id,
            "before claim",
            expected_revision=1,
        )
        self.assertEqual(queued["steering"]["delivery"]["scheduled_for"], "next_attempt")
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed_one = coordinator._claim_next_ready_node("steering-a1")
            assert claimed_one is not None
            self.assertEqual(claimed_one["steering"], ("before claim",))
            self.assertEqual(
                {
                    (item["steering_id"], item["node_id"], item["attempt"])
                    for item in claimed_one["steering_deliveries"]
                },
                {(queued["steering"]["steering_id"], "worker", 1)},
            )
            running = self.store.get_task(contract.task_id)
            future = self.store.append_task_steering_receipt(
                contract.task_id,
                "after claim",
                expected_revision=int(running["state_revision"]),
            )
            self.assertEqual(future["delivery"]["status"], "not_delivered")
            self.assertEqual(future["delivery"]["scheduled_for"], "future_attempt")
            self.assertFalse(future["delivery"]["current_attempt_received"])

            first_request: dict[str, tuple[str, ...]] = {}
            with patch.object(coordinator, "_executor") as executor:
                def fail(request: object) -> NodeResult:
                    first_request["steering"] = request.steering  # type: ignore[attr-defined]
                    return NodeResult("failed", "retry with later steering")

                executor.return_value.execute.side_effect = fail
                coordinator._execute_claimed(claimed_one)
            self.assertEqual(first_request["steering"], ("before claim",))

            failed = self.store.get_task(contract.task_id)
            self.store.queue_task(
                contract.task_id,
                expected_revision=int(failed["state_revision"]),
            )
            claimed_two = coordinator._claim_next_ready_node("steering-a2")
            assert claimed_two is not None
            self.assertEqual(claimed_two["steering"], ("before claim", "after claim"))
            delivered = [
                event
                for event in self.store.read_events(task_id=contract.task_id)
                if event["event_type"] == "task.steering_delivered"
                and event["payload"]["steering_id"] == future["steering_id"]
            ]
            self.assertEqual(
                [(event["node_id"], event["payload"]["attempt"]) for event in delivered],
                [("worker", 2)],
            )
        finally:
            coordinator._pool.shutdown(wait=True)

    def test_indeterminate_recovered_worktree_cannot_be_retried_or_duplicated(self) -> None:
        contract, _ = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("crashing-retry")
            assert claimed is not None
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = RuntimeError("fixture crash after assignment")
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        indeterminate = self.store.get_task(contract.task_id)
        worker = next(node for node in indeterminate["nodes"] if node["node_id"] == "worker")
        self.assertEqual((indeterminate["state"], worker["state"], worker["attempt"]), ("needs_approval", "indeterminate", 2))
        approval = self.store.list_approvals()[0]
        with self.assertRaisesRegex(StateConflictError, "explicit recovery"):
            self.store.decide_approval(
                approval["approval_id"],
                "retry",
                expected_revision=int(indeterminate["state_revision"]),
            )
        self.assertEqual(self.store.get_task(contract.task_id), indeterminate)
        self.assertIsNone(self.store.claim_ready_node("must-not-duplicate", self.epoch))

    def test_restart_after_recovery_assignment_preserves_target_and_fences_retry(self) -> None:
        contract, _ = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("crash-after-assignment")
            assert claimed is not None
            target, _, _, _ = coordinator._prepare_failed_attempt_recovery(claimed)
            self.assertEqual(
                (target / "src" / "continuation.txt").read_text(encoding="utf-8"),
                "untracked prior attempt\n",
            )
        finally:
            coordinator._pool.shutdown(wait=True)

        restarted_store = WorkbenchStore(self.config.database)
        restarted_store.initialize()
        new_epoch = restarted_store.activate_coordinator(
            "restarted-coordinator",
            "fixture-machine",
        )
        self.assertEqual(restarted_store.recover_interrupted(), 1)
        indeterminate = restarted_store.get_task(contract.task_id)
        worker = next(node for node in indeterminate["nodes"] if node["node_id"] == "worker")
        self.assertEqual(
            (indeterminate["state"], worker["state"], worker["attempt"], worker["worktree"]),
            ("needs_approval", "indeterminate", 2, str(target)),
        )
        allocations = restarted_store.list_worktree_allocations()
        source_allocation = next(
            item for item in allocations if item["node_id"] == "worker" and item["attempt"] == 1
        )
        target_allocation = next(
            item for item in allocations if item["node_id"] == "worker" and item["attempt"] == 2
        )
        self.assertEqual(source_allocation["state"], "superseded")
        self.assertEqual((target_allocation["state"], target_allocation["current_path"]), ("active", str(target)))

        approval = restarted_store.list_approvals()[0]
        with self.assertRaisesRegex(StateConflictError, "explicit recovery"):
            restarted_store.decide_approval(
                approval["approval_id"],
                "retry",
                expected_revision=int(indeterminate["state_revision"]),
            )
        self.assertEqual(restarted_store.get_task(contract.task_id), indeterminate)
        self.assertIsNone(restarted_store.claim_ready_node("restarted-worker", new_epoch))

    def test_restart_before_recovery_assignment_rolls_back_and_resumes_safely(self) -> None:
        contract, source = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        claimed = coordinator._claim_next_ready_node("crash-before-assignment")
        assert claimed is not None
        target = self.state_root / "worktrees" / contract.task_id / "worker-a2"
        try:
            with patch.object(
                self.store,
                "assign_failed_attempt_recovery_worktree",
                side_effect=KeyboardInterrupt("fixture process termination"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    coordinator._prepare_failed_attempt_recovery(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)
        self.assertTrue(target.is_dir())

        restarted_store = WorkbenchStore(self.config.database)
        restarted_store.initialize()
        new_epoch = restarted_store.activate_coordinator(
            "restarted-before-assignment",
            "fixture-machine",
        )
        recovered, orphaned = restarted_store.recover_interrupted_with_orphans()
        self.assertEqual(recovered, 1)
        self.assertEqual(len(orphaned), 1)
        self.assertTrue(target.is_dir())
        self.assertEqual(
            len(restarted_store.pending_failed_attempt_recovery_orphans()),
            1,
        )
        cleanup_attempt = Coordinator(
            restarted_store,
            self.state_root,
            coordinator_epoch=new_epoch,
        )
        try:
            with patch.object(
                cleanup_attempt.worktrees,
                "archive_failed_recovery",
                side_effect=WorktreeError("fixture archive unavailable"),
            ):
                self.assertEqual(cleanup_attempt.recover(), 0)
        finally:
            cleanup_attempt._pool.shutdown(wait=True)
        self.assertTrue(target.is_dir())
        self.assertEqual(
            len(restarted_store.pending_failed_attempt_recovery_orphans()),
            1,
        )

        resumed_store = WorkbenchStore(self.config.database)
        resumed_store.initialize()
        resumed_epoch = resumed_store.activate_coordinator(
            "resumed-orphan-cleanup",
            "fixture-machine",
        )
        restarted = Coordinator(
            resumed_store,
            self.state_root,
            coordinator_epoch=resumed_epoch,
        )
        self.assertEqual(restarted.recover(), 0)
        self.assertFalse(target.exists())
        self.assertEqual(
            resumed_store.pending_failed_attempt_recovery_orphans(),
            (),
        )
        rolled_back = resumed_store.get_task(contract.task_id)
        worker = next(node for node in rolled_back["nodes"] if node["node_id"] == "worker")
        self.assertEqual(
            (rolled_back["state"], worker["state"], worker["attempt"], worker["worktree"]),
            ("needs_fix", "failed", 1, str(source)),
        )
        archive_root = target.parent / "recovery-failures"
        self.assertEqual(len(list(archive_root.iterdir())), 1)

        resumed_store.queue_task(
            contract.task_id,
            expected_revision=int(rolled_back["state_revision"]),
        )
        observed: dict[str, str] = {}
        try:
            retry = restarted._claim_next_ready_node("resumed-after-restart")
            assert retry is not None

            def execute(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["tracked"] = (worktree / "src" / "value.txt").read_text(
                    encoding="utf-8"
                )
                observed["untracked"] = (worktree / "src" / "continuation.txt").read_text(
                    encoding="utf-8"
                )
                return NodeResult("succeeded", "resumed after restart", checks=("fixture",))

            with patch.object(restarted, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                restarted._execute_claimed(retry)
        finally:
            restarted._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "tracked": "dirty prior attempt\n",
                "untracked": "untracked prior attempt\n",
            },
        )
        completed = resumed_store.get_task(contract.task_id)
        completed_worker = next(
            node for node in completed["nodes"] if node["node_id"] == "worker"
        )
        self.assertEqual((completed_worker["state"], completed_worker["attempt"]), ("accepted", 2))
        self.assertEqual(len(list(archive_root.iterdir())), 1)

    def test_target_created_before_recovery_failure_is_archived(self) -> None:
        contract, source = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("archive-failed-target")
            assert claimed is not None
            with (
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "prepare_for_retry",
                    side_effect=RuntimeError("fixture target validation crash"),
                ),
                patch.object(coordinator, "_executor") as executor,
            ):
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 1))
        self.assertEqual(worker["worktree"], str(source))
        target = self.state_root / "worktrees" / contract.task_id / "worker-a2"
        self.assertFalse(target.exists())
        archive_root = target.parent / "recovery-failures"
        self.assertEqual(len(list(archive_root.iterdir())), 1)

    def test_failed_recovery_filesystem_work_runs_outside_write_transactions(self) -> None:
        contract, _ = self._dirty_failed_task()
        failed = self.store.get_task(contract.task_id)
        self.store.queue_task(
            contract.task_id,
            expected_revision=int(failed["state_revision"]),
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        claimed = coordinator._claim_next_ready_node("transaction-boundary")
        assert claimed is not None

        original_transaction = self.store.transaction
        original_validate = coordinator.failed_attempt_recovery.validate_retry_source
        original_prepare_clean = coordinator.worktrees.prepare_clean
        original_capture = coordinator.failed_attempt_recovery.capture
        original_prepare_retry = coordinator.failed_attempt_recovery.prepare_for_retry
        in_write_transaction = False

        @contextmanager
        def tracked_transaction():
            nonlocal in_write_transaction
            with original_transaction() as connection:
                in_write_transaction = True
                try:
                    yield connection
                finally:
                    in_write_transaction = False

        def outside_write_transaction(function):
            def checked(*args: object, **kwargs: object):
                self.assertFalse(in_write_transaction)
                return function(*args, **kwargs)

            return checked

        try:
            with (
                patch.object(self.store, "transaction", tracked_transaction),
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "validate_retry_source",
                    side_effect=outside_write_transaction(original_validate),
                ),
                patch.object(
                    coordinator.worktrees,
                    "prepare_clean",
                    side_effect=outside_write_transaction(original_prepare_clean),
                ),
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "capture",
                    side_effect=outside_write_transaction(original_capture),
                ),
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "prepare_for_retry",
                    side_effect=outside_write_transaction(original_prepare_retry),
                ),
            ):
                target, _, _, _ = coordinator._prepare_failed_attempt_recovery(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertTrue(target.is_dir())
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"], worker["worktree"]), ("running", 2, str(target)))

    def test_artifact_validation_runs_before_the_sqlite_write_transaction(self) -> None:
        contract = TaskContract(
            task_id="settlement-preflight",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="validate artifacts without a write lock",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", contract.task_id, "work", "fixture", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "settlement-preflight-create",
        )
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("settlement-worker", self.epoch)
        assert claimed is not None
        artifact_ref = self.artifacts.put_text("fixture", "log.txt")
        original_transaction = self.store.transaction
        original_verify = self.store._verify_artifact_refs
        in_write_transaction = False

        @contextmanager
        def tracked_transaction():
            nonlocal in_write_transaction
            with original_transaction() as connection:
                in_write_transaction = True
                try:
                    yield connection
                finally:
                    in_write_transaction = False

        def verify(artifacts: dict[str, str]) -> None:
            self.assertFalse(in_write_transaction)
            original_verify(artifacts)

        with (
            patch.object(self.store, "transaction", tracked_transaction),
            patch.object(self.store, "_verify_artifact_refs", side_effect=verify),
        ):
            self.store.settle_claimed(
                claimed,
                NodeResult(
                    "succeeded",
                    "preflight complete",
                    artifacts={"log": artifact_ref},
                    checks=("fixture",),
                ),
            )

    def test_claim_repository_identity_runs_outside_the_write_transaction(self) -> None:
        task_id = "claim-repository-preflight"
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="resolve repository identity before write-lock claim",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", task_id, "work", "fixture", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "claim-repository-preflight-create",
        )
        self.store.queue_task(task_id)
        original_transaction = self.store.transaction
        in_write_transaction = False

        @contextmanager
        def tracked_transaction():
            nonlocal in_write_transaction
            with original_transaction() as connection:
                in_write_transaction = True
                try:
                    yield connection
                finally:
                    in_write_transaction = False

        from codex_workbench import store as store_module

        original_identity = store_module._repository_identity

        def identity(repository: str) -> str:
            self.assertFalse(in_write_transaction)
            return original_identity(repository)

        with (
            patch.object(self.store, "transaction", tracked_transaction),
            patch.object(store_module, "_repository_identity", side_effect=identity),
        ):
            claimed = self.store.claim_ready_node("repository-preflight-worker", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], "worker")

    def test_pause_or_cancel_wins_a_race_with_node_settlement(self) -> None:
        for control_state in ("paused", "cancelled"):
            with self.subTest(control_state=control_state):
                task_id = f"settlement-{control_state}"
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(self.repository),
                    base_sha=self.base_sha,
                    objective=f"preserve operator {control_state} during settlement",
                    allowed_scope=("src",),
                    executor_model="fixture",
                    verifier_model="fixture",
                )
                self.store.create_task(
                    contract,
                    [
                        NodeSpec("worker", task_id, "work", "fixture", "fixture", "fixture"),
                        NodeSpec(
                            "verify",
                            task_id,
                            "verify",
                            "fixture",
                            "fixture",
                            "accepted",
                            depends_on=("worker",),
                            verifier=True,
                        ),
                    ],
                    f"{task_id}-create",
                )
                self.store.queue_task(task_id)
                claimed = self.store.claim_ready_node(f"{control_state}-worker", self.epoch)
                assert claimed is not None
                original_preflight = self.store._prevalidate_settlement
                transitioned = False

                def preflight_then_control(*args: object, **kwargs: object) -> dict[str, object]:
                    nonlocal transitioned
                    signature = original_preflight(*args, **kwargs)
                    if not transitioned:
                        current = self.store.get_task(task_id)
                        self.store.transition_task(
                            task_id,
                            control_state,
                            expected_revision=int(current["state_revision"]),
                        )
                        transitioned = True
                    return signature

                with patch.object(
                    self.store,
                    "_prevalidate_settlement",
                    side_effect=preflight_then_control,
                ):
                    self.store.settle_claimed(
                        claimed,
                        NodeResult(
                            "succeeded",
                            "late leased result",
                            checks=("fixture",),
                        ),
                    )

                settled = self.store.get_task(task_id)
                worker = next(node for node in settled["nodes"] if node["node_id"] == "worker")
                verifier = next(node for node in settled["nodes"] if node["node_id"] == "verify")
                self.assertEqual(settled["state"], control_state)
                self.assertEqual(worker["state"], "accepted")
                self.assertEqual(verifier["state"], "pending")

    def test_pause_or_cancel_wins_a_race_with_recovery_rollback(self) -> None:
        for control_state in ("paused", "cancelled"):
            with self.subTest(control_state=control_state):
                contract, source = self._dirty_failed_task(
                    task_id=f"recovery-rollback-{control_state}"
                )
                failed = self.store.get_task(contract.task_id)
                self.store.queue_task(
                    contract.task_id,
                    expected_revision=int(failed["state_revision"]),
                )
                coordinator = Coordinator(
                    self.store,
                    self.state_root,
                    coordinator_epoch=self.epoch,
                )
                try:
                    claimed = coordinator._claim_next_ready_node(
                        f"recovery-{control_state}"
                    )
                    assert claimed is not None
                    current = self.store.get_task(contract.task_id)
                    self.store.transition_task(
                        contract.task_id,
                        control_state,
                        expected_revision=int(current["state_revision"]),
                    )
                    with (
                        patch.object(
                            coordinator.failed_attempt_recovery,
                            "prepare_for_retry",
                            side_effect=RuntimeError("fixture recovery failure"),
                        ),
                        patch.object(coordinator, "_executor") as executor,
                    ):
                        coordinator._execute_claimed(claimed)
                        executor.assert_not_called()
                finally:
                    coordinator._pool.shutdown(wait=True)

                task = self.store.get_task(contract.task_id)
                worker = next(
                    node for node in task["nodes"] if node["node_id"] == "worker"
                )
                self.assertEqual(task["state"], control_state)
                self.assertEqual(
                    (worker["state"], worker["attempt"], worker["worktree"]),
                    ("failed", 1, str(source)),
                )
                preserved = [
                    event
                    for event in self.store.read_events(task_id=contract.task_id)
                    if event["event_type"] == "task.control_state_preserved"
                ]
                self.assertEqual(len(preserved), 1)


class IndeterminateLocalRecoveryTests(_FailedAttemptRecoveryFixture, unittest.TestCase):
    """Explicit operator-confirmed local recovery for owned-worktree indeterminate nodes."""

    def _indeterminate_owned_worktree_task(
        self,
        *,
        task_id: str = "indeterminate-recovery",
        external_write_permission: bool = False,
        destructive_action_permission: bool = False,
    ) -> tuple[TaskContract, Path, str]:
        """Leave the worker ``indeterminate`` while it still owns an attempt-2 worktree.

        ``_dirty_failed_task(retryable=True)`` already settles the worker as a
        retryable failed attempt, which builds the normal ``capture_pending``
        recovery binding and auto-queues the task; this then claims that
        binding and crashes the fixture executor after the coordinator has
        already restored the patch onto a fresh attempt-2 target and assigned
        it -- exactly the scenario already covered by
        ``test_indeterminate_recovered_worktree_cannot_be_retried_or_duplicated``.

        Also returns the worker's recorded ``dependency-input`` artifact ref
        (durable in ArtifactStore from the original attempt-1 settlement)
        so callers can preserve the accepted ``ancestor`` dependency rather
        than recomputing lineage from the raw task base.
        """

        contract, _ = self._dirty_failed_task(
            task_id=task_id,
            retryable=True,
            external_write_permission=external_write_permission,
            destructive_action_permission=destructive_action_permission,
        )
        failed_event = next(
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.failed" and event["node_id"] == "worker"
        )
        dependency_input_ref = failed_event["payload"]["result"]["artifacts"]["dependency-input"]

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node(f"{task_id}-crashing-retry")
            assert claimed is not None
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = RuntimeError("fixture crash after assignment")
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        indeterminate = self.store.get_task(contract.task_id)
        node = next(item for item in indeterminate["nodes"] if item["node_id"] == "worker")
        self.assertEqual(
            (indeterminate["state"], node["state"], node["attempt"]),
            ("needs_approval", "indeterminate", 2),
        )
        target = Path(str(node["worktree"]))
        self.assertEqual(
            (target / "src" / "continuation.txt").read_text(encoding="utf-8"),
            "untracked prior attempt\n",
        )
        return contract, target, dependency_input_ref

    def test_candidate_requires_owned_worktree_with_no_pending_recovery_binding(self) -> None:
        contract, target, _ = self._indeterminate_owned_worktree_task()
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        self.assertEqual(candidate["node"]["worktree"], str(target))
        self.assertEqual(candidate["node"]["attempt"], 2)
        self.assertEqual(candidate["task"]["repository"], str(self.repository))

    def test_unknown_ignored_paths_are_rejected_with_a_bounded_sample(self) -> None:
        contract, target, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-bounded-ignored"
        )
        ignored = target / ".workbench-ignored"
        ignored.mkdir()
        for index in range(12):
            (ignored / f"generated-{index:02d}-{'x' * 160}.cache").write_text(
                "not platform-attested\n", encoding="utf-8"
            )
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )

        with self.assertRaisesRegex(
            DirtyWorktreeRecoveryError,
            r"12 path\(s\).*4 additional path\(s\) omitted",
        ) as raised:
            observed_indeterminate_recovery_paths(
                candidate,
                dependency_input_ref=dependency_input_ref,
                artifacts=self.artifacts,
            )

        self.assertLessEqual(len(str(raised.exception)), 1_024)
        self.assertEqual(
            (ignored / f"generated-11-{'x' * 160}.cache").read_text(encoding="utf-8"),
            "not platform-attested\n",
        )
        self.assertEqual(self.store.get_task(contract.task_id), task)

    def test_local_recovery_preserves_tracked_and_untracked_changes_and_dispatches_new_attempt(
        self,
    ) -> None:
        contract, target, dependency_input_ref = self._indeterminate_owned_worktree_task()
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        self.assertEqual(changed_paths, ("src/continuation.txt", "src/value.txt"))
        self.assertEqual(generated_residue_paths, ())

        result = self.store.queue_indeterminate_local_recovery(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
            reason="operator confirmed the crashed executor exited and effects stayed in-scope",
            confirm_old_executor_ended=True,
            confirm_effects_restricted_to_owned_files=True,
            observed_changed_paths=changed_paths,
            observed_generated_residue_paths=generated_residue_paths,
            dependency_input_ref=dependency_input_ref,
        )
        self.assertEqual(result["task"]["state"], "queued")
        self.assertEqual(result["node"]["state"], "pending")
        self.assertEqual(result["node"]["next_attempt"], 3)

        queued = self.store.get_task(contract.task_id)
        node = next(item for item in queued["nodes"] if item["node_id"] == "worker")
        self.assertEqual((node["state"], node["attempt"], node["worktree"]), ("pending", 2, None))

        # The prior attempt-2 worktree is left untouched on disk: recovery
        # restores its patch onto a brand-new attempt-3 target rather than
        # mutating or reusing the indeterminate source in place.
        self.assertEqual(
            (target / "src" / "continuation.txt").read_text(encoding="utf-8"),
            "untracked prior attempt\n",
        )
        self.assertEqual(
            (target / "src" / "value.txt").read_text(encoding="utf-8"),
            "dirty prior attempt\n",
        )

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}

        def execute(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            observed["attempt"] = request.attempt  # type: ignore[attr-defined]
            observed["ancestor"] = (worktree / "src" / "ancestor.txt").read_text(encoding="utf-8")
            observed["tracked"] = (worktree / "src" / "value.txt").read_text(encoding="utf-8")
            observed["untracked"] = (worktree / "src" / "continuation.txt").read_text(encoding="utf-8")
            return NodeResult("succeeded", "fake executor continued recovered worker", checks=("fake",))

        try:
            claimed = coordinator._claim_next_ready_node("resume-after-local-recovery")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 3))
            self.assertIn("failed_attempt_recovery", claimed)
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "attempt": 3,
                "ancestor": "accepted ancestor\n",
                "tracked": "dirty prior attempt\n",
                "untracked": "untracked prior attempt\n",
            },
        )
        accepted = self.store.get_task(contract.task_id)
        node = next(item for item in accepted["nodes"] if item["node_id"] == "worker")
        self.assertEqual((node["state"], node["attempt"]), ("accepted", 3))

    def test_local_recovery_via_mcp_control_action(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-mcp"
        )
        task = self.store.get_task(contract.task_id)
        response = self.mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "workbench_control_task",
                    "arguments": {
                        "task_id": contract.task_id,
                        "action": "resolve_indeterminate_locally",
                        "expected_revision": int(task["state_revision"]),
                        "node_id": "worker",
                        "expected_attempt": 2,
                        "reason": "operator confirmed process exit via ps/pgrep before recovery",
                        "confirm_old_executor_ended": True,
                        "confirm_effects_restricted_to_owned_files": True,
                        "dependency_input_ref": dependency_input_ref,
                    },
                },
            }
        )
        assert response is not None
        self.assertNotIn("isError", response["result"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task"]["state"], "queued")

    def test_bare_resolve_indeterminate_still_rejects_owned_worktree_retry(self) -> None:
        """The original fail-closed guard remains intact for the un-recovered path."""

        contract, _, _ = self._indeterminate_owned_worktree_task(task_id="indeterminate-bare-reject")
        indeterminate = self.store.get_task(contract.task_id)
        approval = self.store.list_approvals()[0]
        with self.assertRaisesRegex(StateConflictError, "explicit recovery"):
            self.store.decide_approval(
                approval["approval_id"],
                "retry",
                expected_revision=int(indeterminate["state_revision"]),
            )

    def test_stale_expected_attempt_is_rejected(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(task_id="indeterminate-stale-attempt")
        task = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "expected node attempt"):
            self.store.indeterminate_local_recovery_candidate(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=1,
            )

    def test_stale_expected_revision_is_rejected(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(task_id="indeterminate-stale-revision")
        task = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "expected task revision"):
            self.store.indeterminate_local_recovery_candidate(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]) + 1,
                expected_attempt=2,
            )

    def test_missing_confirmation_flags_are_rejected(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(task_id="indeterminate-missing-confirm")
        task = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(ValueError, "old executor ended"):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=2,
                reason="missing confirmation",
                confirm_old_executor_ended=False,
                confirm_effects_restricted_to_owned_files=True,
                observed_changed_paths=("src/value.txt",),
            )
        with self.assertRaisesRegex(ValueError, "restricted to owned files"):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=2,
                reason="missing confirmation",
                confirm_old_executor_ended=True,
                confirm_effects_restricted_to_owned_files=False,
                observed_changed_paths=("src/value.txt",),
            )
        unchanged = self.store.get_task(contract.task_id)
        self.assertEqual(unchanged, task)

    def test_dependent_node_requires_its_recorded_input_before_local_recovery(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-missing-dependency-input"
        )
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        with self.assertRaisesRegex(
            DirtyWorktreeRecoveryError, "recorded dependency-input artifact"
        ):
            observed_indeterminate_recovery_paths(
                candidate,
                dependency_input_ref=None,
                artifacts=self.artifacts,
            )
        with self.assertRaisesRegex(
            StateConflictError, "recorded dependency-input artifact"
        ):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=2,
                reason="operator confirmed process exit and local effects",
                confirm_old_executor_ended=True,
                confirm_effects_restricted_to_owned_files=True,
                observed_changed_paths=("src/value.txt",),
            )
        self.assertEqual(self.store.get_task(contract.task_id), task)

    def test_local_recovery_rejects_contracts_with_possible_external_effects(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-external-effects",
            external_write_permission=True,
        )
        task = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "no external or destructive permissions"):
            self.store.indeterminate_local_recovery_candidate(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=2,
            )

    def test_out_of_node_scope_path_is_rejected_before_confirmation(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-node-scope"
        )
        task = self.store.get_task(contract.task_id)
        with self.store.transaction() as connection:
            raw = connection.execute(
                "SELECT spec_json FROM nodes WHERE task_id = ? AND node_id = 'worker'",
                (contract.task_id,),
            ).fetchone()
            assert raw is not None
            spec = json.loads(raw["spec_json"])
            spec["write_scopes"] = ["src/narrow"]
            connection.execute(
                "UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = 'worker'",
                (json.dumps(spec, sort_keys=True), contract.task_id),
            )
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "outside node write scope"):
            observed_indeterminate_recovery_paths(
                candidate,
                dependency_input_ref=dependency_input_ref,
                artifacts=self.artifacts,
            )

    def test_known_generated_residue_is_bound_without_becoming_worker_input(self) -> None:
        contract, target, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-generated-residue"
        )
        residue = target / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
        residue.parent.mkdir(parents=True)
        residue.write_bytes(b"indeterminate residue\0")
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        self.assertEqual(generated_residue_paths, ("tests/__pycache__/fixture.cpython-313.pyc",))
        preview = self.store.queue_indeterminate_local_recovery(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
            reason="operator confirmed process exit and local effects",
            confirm_old_executor_ended=True,
            confirm_effects_restricted_to_owned_files=True,
            observed_changed_paths=changed_paths,
            observed_generated_residue_paths=generated_residue_paths,
            dependency_input_ref=dependency_input_ref,
            dry_run=True,
        )
        source = preview["would_authorize"]["source"]
        self.assertEqual(source["changed_paths"], list(changed_paths))
        self.assertEqual(source["generated_residue_paths"], list(generated_residue_paths))
        self.assertEqual(self.store.get_task(contract.task_id), task)

    def test_local_observation_runs_before_the_queue_write_transaction(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-observation-transaction"
        )
        task = self.store.get_task(contract.task_id)
        original_transaction = self.store.transaction
        from codex_workbench import mcp as mcp_module

        original_observation = mcp_module.observed_indeterminate_recovery_paths
        in_write_transaction = False

        @contextmanager
        def tracked_transaction():
            nonlocal in_write_transaction
            with original_transaction() as connection:
                in_write_transaction = True
                try:
                    yield connection
                finally:
                    in_write_transaction = False

        def observed_outside_write_transaction(*args: object, **kwargs: object):
            self.assertFalse(in_write_transaction)
            return original_observation(*args, **kwargs)

        with (
            patch.object(self.store, "transaction", tracked_transaction),
            patch.object(
                mcp_module,
                "observed_indeterminate_recovery_paths",
                side_effect=observed_outside_write_transaction,
            ),
        ):
            response = self.mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "workbench_control_task",
                        "arguments": {
                            "task_id": contract.task_id,
                            "action": "resolve_indeterminate_locally",
                            "expected_revision": int(task["state_revision"]),
                            "node_id": "worker",
                            "expected_attempt": 2,
                            "reason": "operator confirmed process exit and local effects",
                            "confirm_old_executor_ended": True,
                            "confirm_effects_restricted_to_owned_files": True,
                            "dependency_input_ref": dependency_input_ref,
                        },
                    },
                }
            )
        assert response is not None
        self.assertNotIn("isError", response["result"])

    def test_foreign_out_of_scope_path_is_rejected_before_confirmation(self) -> None:
        contract, target, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-foreign-path"
        )
        (target / "outside.txt").write_text("not in scope\n", encoding="utf-8")
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "outside task scope"):
            observed_indeterminate_recovery_paths(
                candidate,
                dependency_input_ref=dependency_input_ref,
                artifacts=self.artifacts,
            )
        # The rejection happens before any store mutation is attempted.
        self.assertEqual(self.store.get_task(contract.task_id), task)

    def test_stale_changed_paths_receipt_is_rejected_at_dispatch(self) -> None:
        """An inaccurate changed_paths receipt is caught before executor dispatch.

        ``queue_indeterminate_local_recovery`` authorizes the reused
        ``failed-attempt-worktree-recovery`` binding from the caller-supplied
        receipt; the coordinator's existing drift check re-derives the actual
        changed paths from the live worktree at dispatch time and rejects a
        receipt that claims a path never touched by the worker.
        """

        contract, target, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-drift"
        )
        task = self.store.get_task(contract.task_id)
        (target / "src" / "does-not-exist.txt").write_text("added after capture\n", encoding="utf-8")
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        self.store.queue_indeterminate_local_recovery(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
            reason="operator confirmed the crashed executor exited and effects stayed in-scope",
            confirm_old_executor_ended=True,
            confirm_effects_restricted_to_owned_files=True,
            observed_changed_paths=changed_paths,
            observed_generated_residue_paths=generated_residue_paths,
            dependency_input_ref=dependency_input_ref,
        )
        (target / "src" / "does-not-exist.txt").unlink()

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("drift-detection")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("drifted recovery must not dispatch an executor"),
            ):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        drifted = self.store.get_task(contract.task_id)
        node = next(item for item in drifted["nodes"] if item["node_id"] == "worker")
        self.assertEqual((drifted["state"], node["state"], node["attempt"]), ("needs_fix", "failed", 2))

    def test_old_attempt_fencing_rejects_wrong_attempt_number(self) -> None:
        contract, _, _ = self._indeterminate_owned_worktree_task(task_id="indeterminate-attempt-fence")
        task = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "expected node attempt"):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=1,
                reason="wrong attempt",
                confirm_old_executor_ended=True,
                confirm_effects_restricted_to_owned_files=True,
                observed_changed_paths=("src/value.txt",),
            )

    def test_stale_revision_cas_rejects_commit(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-stale-cas"
        )
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        with self.assertRaisesRegex(StateConflictError, "expected task revision"):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]) + 1,
                expected_attempt=2,
                reason="stale revision",
                confirm_old_executor_ended=True,
                confirm_effects_restricted_to_owned_files=True,
                observed_changed_paths=changed_paths,
                observed_generated_residue_paths=generated_residue_paths,
                dependency_input_ref=dependency_input_ref,
            )
        self.assertEqual(self.store.get_task(contract.task_id), task)

    def test_observed_revision_that_changes_before_commit_is_rejected(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-racing-cas"
        )
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        original_authorization = self.store._failed_attempt_recovery_authorization
        authorization_calls = 0

        def stale_after_preflight(*args: object, **kwargs: object) -> dict[str, object] | None:
            nonlocal authorization_calls
            authorization_calls += 1
            authorization = original_authorization(*args, **kwargs)
            if authorization_calls == 1:
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET state_revision = state_revision + 1 WHERE task_id = ?",
                        (contract.task_id,),
                    )
            return authorization

        with (
            patch.object(
                self.store,
                "_failed_attempt_recovery_authorization",
                side_effect=stale_after_preflight,
            ),
            self.assertRaisesRegex(StateConflictError, "expected task revision"),
        ):
            self.store.queue_indeterminate_local_recovery(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]),
                expected_attempt=2,
                reason="operator confirmed process exit and local effects",
                confirm_old_executor_ended=True,
                confirm_effects_restricted_to_owned_files=True,
                observed_changed_paths=changed_paths,
                observed_generated_residue_paths=generated_residue_paths,
                dependency_input_ref=dependency_input_ref,
            )
        self.assertEqual(authorization_calls, 1)

    def test_local_recovery_hash_mismatch_is_rejected_before_executor_dispatch(self) -> None:
        contract, _, dependency_input_ref = self._indeterminate_owned_worktree_task(
            task_id="indeterminate-hash-mismatch"
        )
        task = self.store.get_task(contract.task_id)
        candidate = self.store.indeterminate_local_recovery_candidate(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
        )
        changed_paths, generated_residue_paths = observed_indeterminate_recovery_paths(
            candidate,
            dependency_input_ref=dependency_input_ref,
            artifacts=self.artifacts,
        )
        self.store.queue_indeterminate_local_recovery(
            contract.task_id,
            "worker",
            expected_revision=int(task["state_revision"]),
            expected_attempt=2,
            reason="operator confirmed process exit and local effects",
            confirm_old_executor_ended=True,
            confirm_effects_restricted_to_owned_files=True,
            observed_changed_paths=changed_paths,
            observed_generated_residue_paths=generated_residue_paths,
            dependency_input_ref=dependency_input_ref,
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        original_capture = coordinator.failed_attempt_recovery.capture

        def tampered_capture(**kwargs: object) -> dict[str, object]:
            recovery = original_capture(**kwargs)
            return {**recovery, "patch_sha256": "0" * 64}

        try:
            claimed = coordinator._claim_next_ready_node("local-recovery-hash-mismatch")
            assert claimed is not None
            with (
                patch.object(
                    coordinator.failed_attempt_recovery,
                    "capture",
                    side_effect=tampered_capture,
                ),
                patch.object(coordinator, "_executor") as executor,
            ):
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        rejected = self.store.get_task(contract.task_id)
        worker = next(node for node in rejected["nodes"] if node["node_id"] == "worker")
        self.assertEqual((rejected["state"], worker["state"], worker["attempt"]), ("needs_fix", "failed", 2))


if __name__ == "__main__":
    unittest.main()
