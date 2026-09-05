from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority import authority_machine_id
from codex_workbench.cli import build_parser, command_task
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecovery
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class BlockedWorktreeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repository, check=True)
        (self.repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
        (self.repository / "other.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/value.txt", "other.txt"], cwd=self.repository, check=True)
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
        self.epoch = self.store.activate_coordinator("blocked-worktree-test", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.recovery = DirtyWorktreeRecovery(self.store.artifacts, self.worktrees)

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

    def _blocked_task(
        self,
        *,
        acceptance_command: str,
        patch_path: str = "src/value.txt",
        allowed_scope: tuple[str, ...] = ("src",),
        write_scopes: tuple[str, ...] = ("src",),
    ) -> tuple[TaskContract, dict, Path, bytes]:
        contract = TaskContract(
            task_id="blocked-worktree",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="recover a dirty worker only on a fresh worktree",
            allowed_scope=allowed_scope,
            acceptance_commands=(acceptance_command,),
            executor_model="fixture",
            verifier_model="fixture",
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "recover the tracked change",
            "fixture",
            "fixture",
            "apply a deterministic fixture change",
            write_scopes=write_scopes,
        )
        verifier = NodeSpec(
            "verify",
            contract.task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            "accepted",
            depends_on=(worker.node_id,),
            verifier=True,
        )
        self.store.create_task(contract, [worker, verifier], "blocked-worktree-create")
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        assert claimed is not None
        source = self.worktrees.prepare(
            str(self.repository), self.base_sha, contract.task_id, worker.node_id, int(claimed["attempt"])
        )
        self.store.assign_worktree(
            contract.task_id,
            worker.node_id,
            str(source),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        target_file = source / patch_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("patched\n", encoding="utf-8")
        patch_before = subprocess.run(
            ["git", "-C", str(source), "diff", "--binary", self.base_sha],
            check=True,
            capture_output=True,
        ).stdout
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "fixture execution stopped after writing a recoverable tracked patch",
                actual_model="fixture",
                result_kind="worker",
                changed_paths=(patch_path,),
                checks=("fixture blocked after tracked patch",),
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )
        return contract, self.store.get_task(contract.task_id), source, patch_before

    def _authorize(self, contract: TaskContract, blocked: dict, source: Path) -> dict:
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        recovery = self.recovery.capture(
            repository=contract.repository,
            base_sha=contract.base_sha,
            worktree=str(source),
            branch=self.worktrees.branch_name(contract.task_id, "worker", int(worker["attempt"])),
            attempt=int(worker["attempt"]),
            expected_changed_paths=tuple(worker["result"]["changed_paths"]),
        )
        return self.store.resume_blocked_worktree(
            contract.task_id,
            "worker",
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=int(worker["attempt"]),
            reason="preserve a1 and verify its tracked patch on clean a2",
            recovery=recovery,
        )

    def test_clean_a2_recovery_never_invokes_a_model_or_mutates_a1(self) -> None:
        command = f"{sys.executable} -c \"from pathlib import Path; assert Path('src/value.txt').read_text() == 'patched\\n'\""
        contract, blocked, source, source_patch = self._blocked_task(acceptance_command=command)
        self._authorize(contract, blocked, source)
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("recovery-worker")
            assert claimed is not None
            self.assertEqual(claimed["attempt"], 2)
            self.assertEqual(claimed["spec"]["executor"], "deterministic")
            self.assertEqual(claimed["spec"]["model"], "blocked-worktree-recovery")
            self.assertIn("blocked_worktree_recovery", claimed)
            with patch.object(coordinator, "_executor", side_effect=AssertionError("model executor must not run")) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual(worker["result"]["provider"], "workbench-dirty-worktree-recovery")
        self.assertIsNone(worker["result"]["actual_model"])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )
        target = Path(worker["worktree"])
        self.assertEqual((target / "src" / "value.txt").read_text(encoding="utf-8"), "patched\n")
        allocations = {item["attempt"]: item for item in self.store.list_worktree_allocations()}
        self.assertEqual(allocations[1]["state"], "superseded")
        self.assertEqual(allocations[2]["state"], "active")
        events = [event["event_type"] for event in self.store.read_events(task_id=contract.task_id)]
        self.assertIn("node.blocked_worktree_recovery_consumed", events)

    def test_failed_clean_a2_preparation_restores_the_a1_block_without_mutation(self) -> None:
        command = f"{sys.executable} -c \"raise SystemExit(7)\""
        contract, blocked, source, source_patch = self._blocked_task(acceptance_command=command)
        original_worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        authorization = self._authorize(contract, blocked, source)
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("recovery-worker")
            assert claimed is not None
            with patch.object(coordinator, "_executor", side_effect=AssertionError("model executor must not run")) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual(task["state"], "blocked")
        self.assertGreater(task["state_revision"], authorization["revision"])
        self.assertEqual((worker["state"], worker["attempt"], worker["worktree"]), ("blocked", 1, str(source)))
        self.assertEqual(worker["result"], original_worker["result"])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )
        self.assertEqual(self.store.list_worktree_allocations()[0]["state"], "active")
        target_slot = self.state_root / "worktrees" / contract.task_id / "worker-a2"
        self.assertFalse(target_slot.exists())
        archives = list((target_slot.parent / "recovery-failures").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "src" / "value.txt").read_text(encoding="utf-8"), "patched\n")
        events = [event["event_type"] for event in self.store.read_events(task_id=contract.task_id)]
        self.assertIn("node.blocked_worktree_recovery_rolled_back", events)

        # The archived a2 does not consume the deterministic a2 slot. A later
        # explicit authorization reaches the declared acceptance failure again,
        # rather than being rejected because the target still exists.
        retry_blocked = self.store.get_task(contract.task_id)
        self._authorize(contract, retry_blocked, source)
        retry = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            retry_claimed = retry._claim_next_ready_node("recovery-worker-retry")
            assert retry_claimed is not None
            self.assertEqual(retry_claimed["attempt"], 2)
            retry._execute_claimed(retry_claimed)
        finally:
            retry._pool.shutdown(wait=True)
        retried = self.store.get_task(contract.task_id)
        retried_worker = next(node for node in retried["nodes"] if node["node_id"] == "worker")
        self.assertEqual((retried["state"], retried_worker["state"], retried_worker["attempt"]), ("blocked", "blocked", 1))
        self.assertEqual(len(list((target_slot.parent / "recovery-failures").iterdir())), 2)

    def test_scope_failure_rolls_back_before_a1_is_superseded(self) -> None:
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('other.txt').read_text() == 'patched\\n'\""
        )
        contract, blocked, source, source_patch = self._blocked_task(
            acceptance_command=command,
            patch_path="other.txt",
            allowed_scope=(".",),
            write_scopes=("src",),
        )
        original_worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        self._authorize(contract, blocked, source)
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("recovery-worker")
            assert claimed is not None
            coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"], worker["worktree"]), ("blocked", "blocked", 1, str(source)))
        self.assertEqual(worker["result"], original_worker["result"])
        self.assertEqual(self.store.list_worktree_allocations()[0]["state"], "active")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )
        target_slot = self.state_root / "worktrees" / contract.task_id / "worker-a2"
        self.assertFalse(target_slot.exists())
        self.assertEqual(len(list((target_slot.parent / "recovery-failures").iterdir())), 1)
        events = [event["event_type"] for event in self.store.read_events(task_id=contract.task_id)]
        self.assertIn("node.blocked_worktree_recovery_rolled_back", events)
        self.assertNotIn("node.blocked_worktree_recovery_consumed", events)

    def test_prepare_clean_recovers_after_interrupted_archive_stages(self) -> None:
        command = f"{sys.executable} -c \"raise SystemExit(0)\""
        contract, _, _, _ = self._blocked_task(acceptance_command=command)
        target = self.worktrees.prepare(
            contract.repository, contract.base_sha, contract.task_id, "worker", 2
        )
        branch = self.worktrees.branch_name(contract.task_id, "worker", 2)
        # Simulate interruption after detach but before `git worktree move`.
        subprocess.run(
            ["git", "-C", str(target), "switch", "--detach"],
            check=True,
            capture_output=True,
        )
        reclaimed = self.worktrees.prepare_clean(
            contract.repository, contract.base_sha, contract.task_id, "worker", 2
        )
        self.assertTrue(reclaimed.exists())
        archive_root = target.parent / "recovery-failures"
        self.assertEqual(len(list(archive_root.iterdir())), 1)
        self.assertEqual(self._git(reclaimed, "branch", "--show-current"), branch)

        # Simulate interruption after move but before the old branch was
        # deleted. The detached diagnostic tree remains inspectable while the
        # deterministic a2 branch is safely released for the next attempt.
        subprocess.run(
            ["git", "-C", str(reclaimed), "switch", "--detach"],
            check=True,
            capture_output=True,
        )
        partial_archive = archive_root / "interrupted-after-move"
        self.worktrees.move(contract.repository, reclaimed, partial_archive)
        final_target = self.worktrees.prepare_clean(
            contract.repository, contract.base_sha, contract.task_id, "worker", 2
        )
        self.assertTrue(partial_archive.exists())
        self.assertTrue(final_target.exists())
        self.assertEqual(self._git(final_target, "branch", "--show-current"), branch)

    def test_cli_dry_run_captures_the_patch_without_authorizing_or_mutating_state(self) -> None:
        command = f"{sys.executable} -c \"raise SystemExit(0)\""
        contract, blocked, source, source_patch = self._blocked_task(acceptance_command=command)
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        events_before = self.store.read_events(task_id=contract.task_id)
        args = build_parser().parse_args([
            "--home", str(self.state_root),
            "task", "resume-blocked-worktree", contract.task_id, "worker",
            "--expected-revision", str(blocked["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "inspect the patch before authorizing a clean target",
            "--confirm-recovery", "--dry-run",
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["source_capture_read_only"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertEqual(self.store.read_events(task_id=contract.task_id), events_before)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )


if __name__ == "__main__":
    unittest.main()
