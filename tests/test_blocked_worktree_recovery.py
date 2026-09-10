from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.authority import authority_machine_id
from codex_workbench.cli import build_parser, command_task
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dependency_inputs import (
    apply_accepted_ancestor_patches,
    load_recorded_dependency_input,
)
from codex_workbench.dirty_worktree_recovery import (
    DirtyWorktreeRecovery,
    DirtyWorktreeRecoveryError,
    PnpmOfflineMaterializer,
)
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError, WorkbenchStore
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
        (self.repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "src/value.txt", "other.txt"],
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
        self.epoch = self.store.activate_coordinator("blocked-worktree-test", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.recovery = DirtyWorktreeRecovery(self.store.artifacts, self.worktrees)
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

    def _blocked_task(
        self,
        *,
        acceptance_command: str,
        patch_path: str = "src/value.txt",
        allowed_scope: tuple[str, ...] = ("src",),
        write_scopes: tuple[str, ...] = ("src",),
        python_residue: bool = False,
        untracked_path: str | None = None,
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
        changed_paths = [patch_path]
        untracked_paths: tuple[str, ...] = ()
        if untracked_path is not None:
            untracked = source / untracked_path
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked.write_text("untracked recovery fixture\n", encoding="utf-8")
            untracked_paths = (untracked_path,)
            changed_paths.append(untracked_path)
        if python_residue:
            residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
            residue.parent.mkdir(parents=True)
            residue.write_bytes(b"fixture bytecode\0")
            changed_paths.append("tests/__pycache__/fixture.cpython-313.pyc")
        patch_before = DirtyWorktreeRecovery.captured_patch(
            source,
            self.base_sha,
            untracked_paths,
        )
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "fixture execution stopped after writing a recoverable tracked patch",
                actual_model="fixture",
                result_kind="worker",
                changed_paths=tuple(sorted(changed_paths)),
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
        authorization = self.store.resume_blocked_worktree(
            contract.task_id,
            "worker",
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=int(worker["attempt"]),
            reason="preserve a1 and verify its tracked patch on clean a2",
            recovery=recovery,
        )
        return {**authorization, "recovery": recovery}

    def _call_control(self, arguments: dict) -> dict:
        response = self.mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "workbench_control_task",
                    "arguments": arguments,
                },
            }
        )
        assert response is not None
        return response["result"]

    def test_explicit_checkpoint_is_restored_without_rewriting_source(self) -> None:
        contract, blocked, source, original_patch = self._blocked_task(acceptance_command="git diff --check")
        self._git(source, "add", "src/value.txt")
        self._git(source, "commit", "-m", "checkpoint")
        checkpoint = self._git(source, "rev-parse", "HEAD")
        arguments = dict(task_id=contract.task_id, action="resume", node_id="worker",
                         expected_revision=blocked["state_revision"], expected_attempt=1,
                         reason="preserve the reviewed checkpoint", confirm_recovery=True)
        self.assertTrue(self._call_control(arguments)["isError"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        arguments["expected_checkpoint_sha"] = checkpoint
        response = self._call_control(arguments)
        self.assertNotIn("isError", response, response)
        receipt = json.loads(response["content"][0]["text"])["recovery"]
        self.assertEqual(receipt["source_checkpoint_sha"], checkpoint)
        self.assertEqual(self.store.artifacts.verify(receipt["patch_ref"]).read_bytes(), original_patch)
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("checkpoint-test")
            self.assertIsNotNone(claimed)
            with patch.object(coordinator, "_executor", side_effect=AssertionError("no model dispatch")):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual((Path(worker["worktree"]) / "src/value.txt").read_text(), "patched\n")
        self.assertEqual(self._git(source, "rev-parse", "HEAD"), checkpoint)
        self.assertEqual(self._git(source, "status", "--porcelain"), "")

    def test_checkpoint_drift_and_invalid_identity_do_not_authorize_retry(self) -> None:
        contract, blocked, source, _ = self._blocked_task(acceptance_command="git diff --check")
        self._git(source, "add", "src/value.txt")
        self._git(source, "commit", "-m", "checkpoint")
        checkpoint = self._git(source, "rev-parse", "HEAD")
        for bad in ("HEAD", checkpoint[:8], "-bad", 1, self.base_sha):
            with self.subTest(checkpoint=bad), self.assertRaises(DirtyWorktreeRecoveryError):
                self.store.capture_and_resume_blocked_worktree(
                    contract.task_id, "worker", expected_revision=blocked["state_revision"],
                    expected_attempt=1, reason="recover", expected_checkpoint_sha=bad)
            self.assertEqual(self.store.get_task(contract.task_id), blocked)
        receipt = self.recovery.capture(
            repository=contract.repository, base_sha=self.base_sha, worktree=str(source),
            branch=self._git(source, "branch", "--show-current"), attempt=1,
            expected_changed_paths=("src/value.txt",), expected_checkpoint_sha=checkpoint)
        self._git(source, "commit", "--allow-empty", "-m", "head drift")
        with self.assertRaises(DirtyWorktreeRecoveryError):
            self.recovery._validate_snapshot(contract.repository, str(source), receipt)
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

    def test_checkpoint_outside_node_scope_cannot_be_authorized(self) -> None:
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command="git diff --check", patch_path="other.txt",
            allowed_scope=(".",), write_scopes=("src",))
        self._git(source, "add", "other.txt")
        self._git(source, "commit", "-m", "out-of-scope checkpoint")
        with self.assertRaisesRegex(StateConflictError, "write scope"):
            self.store.capture_and_resume_blocked_worktree(
                contract.task_id, "worker", expected_revision=blocked["state_revision"],
                expected_attempt=1, reason="recover", expected_checkpoint_sha=self._git(source, "rev-parse", "HEAD"))
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

    def test_mcp_resume_recovers_dirty_blocked_attempt_without_losing_changes(self) -> None:
        command = (
            f"PYTHONPATH=src {sys.executable} -c \"import os; from pathlib import Path; "
            "assert os.environ['PYTHONPATH'] == 'src'; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'\""
        )
        contract, blocked, source, source_patch = self._blocked_task(
            acceptance_command=command,
            python_residue=True,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")

        missing_confirmation = self._call_control(
            {
                "task_id": contract.task_id,
                "action": "resume",
                "expected_revision": blocked["state_revision"],
                "node_id": "worker",
                "expected_attempt": worker["attempt"],
                "reason": "resume the owned local attempt",
            }
        )
        self.assertTrue(missing_confirmation["isError"])
        self.assertIn("confirm_recovery", missing_confirmation["content"][0]["text"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

        resumed = json.loads(
            self._call_control(
                {
                    "task_id": contract.task_id,
                    "action": "resume",
                    "expected_revision": blocked["state_revision"],
                    "node_id": "worker",
                    "expected_attempt": worker["attempt"],
                    "reason": "resume the owned local attempt",
                    "confirm_recovery": True,
                }
            )["content"][0]["text"]
        )
        self.assertEqual(resumed["action"], "resume-blocked-worktree")
        self.assertEqual(resumed["task"]["state"], "queued")
        self.assertEqual(resumed["next_attempt"], 2)
        self.assertEqual(
            resumed["recovery"]["generated_residue_paths"],
            ["tests/__pycache__/fixture.cpython-313.pyc"],
        )
        self.assertFalse(
            (source / "tests" / "__pycache__" / "fixture.cpython-313.pyc").exists()
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("mcp-blocked-recovery")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("recovery must not dispatch a model"),
            ) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        recovered = self.store.get_task(contract.task_id)
        recovered_worker = next(
            node for node in recovered["nodes"] if node["node_id"] == "worker"
        )
        self.assertEqual(
            (recovered_worker["state"], recovered_worker["attempt"]),
            ("accepted", 2),
        )
        self.assertEqual(
            (Path(recovered_worker["worktree"]) / "src" / "value.txt").read_text(
                encoding="utf-8"
            ),
            "patched\n",
        )

    def test_mcp_dirty_dry_run_does_not_authorize_or_persist_artifacts(self) -> None:
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            untracked_path="src/root-preview.ts",
        )
        with self.store.connection() as connection:
            before = list(connection.iterdump())
        artifacts = {str(p): p.read_bytes() for p in self.store.artifacts.root.rglob("*") if p.is_file()}
        untracked = (source / "src/root-preview.ts").read_bytes()
        tracked_diff = self._git(source, "diff", "--binary")
        for _ in range(2):
            response = self._call_control({
                "task_id": contract.task_id, "action": "resume", "node_id": "worker",
                "expected_revision": blocked["state_revision"], "expected_attempt": 1,
                "reason": "preview only", "confirm_recovery": True,
                "preserve_untracked": True, "dry_run": True,
            })
            self.assertFalse(response.get("isError", False), response)
            preview = json.loads(response["content"][0]["text"])
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["task"]["state"], "blocked")
        with self.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.assertEqual({str(p): p.read_bytes() for p in self.store.artifacts.root.rglob("*") if p.is_file()}, artifacts)
        self.assertEqual((source / "src/root-preview.ts").read_bytes(), untracked)
        self.assertEqual(self._git(source, "diff", "--binary"), tracked_diff)

    def test_root_untracked_recovery_requires_confirmation_and_explicit_preservation(self) -> None:
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            untracked_path="src/root-continuation.ts",
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        arguments = {
            "task_id": contract.task_id,
            "action": "resume",
            "expected_revision": blocked["state_revision"],
            "node_id": "worker",
            "expected_attempt": worker["attempt"],
            "reason": "preserve the explicit root fixture file on clean a2",
            "preserve_untracked": True,
        }

        unconfirmed = self._call_control(arguments)
        self.assertTrue(unconfirmed["isError"])
        self.assertIn("confirm_recovery", unconfirmed["content"][0]["text"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

        arguments["confirm_recovery"] = True
        arguments.pop("preserve_untracked")
        not_preserved = self._call_control(arguments)
        self.assertTrue(not_preserved["isError"])
        self.assertIn("explicit preservation", not_preserved["content"][0]["text"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )

    def test_cli_root_untracked_recovery_restores_clean_a2_without_dependency_input(self) -> None:
        untracked_path = "src/root-continuation.ts"
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'; "
            "assert Path('src/root-continuation.ts').read_text() == 'untracked recovery fixture\\n'\""
        )
        contract, blocked, source, source_patch = self._blocked_task(
            acceptance_command=command,
            untracked_path=untracked_path,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        args = build_parser().parse_args([
            "--home", str(self.state_root),
            "task", "resume-blocked-worktree", contract.task_id, "worker",
            "--expected-revision", str(blocked["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "preserve the declared root fixture file on clean a2",
            "--confirm-recovery", "--preserve-untracked",
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        receipt = json.loads(output.getvalue())
        recovery = receipt["recovery"]
        self.assertEqual(recovery["schema_version"], 7)
        self.assertEqual(recovery["untracked_paths"], [untracked_path])
        self.assertNotIn("dependency_input_ref", recovery)
        self.assertNotIn("input_tree_sha", recovery)

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("root-untracked-recovery-worker")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("recovery must not dispatch a model"),
            ) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        recovered_worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((recovered_worker["state"], recovered_worker["attempt"]), ("accepted", 2))
        self.assertEqual(
            recovered_worker["result"]["changed_paths"],
            ["src/root-continuation.ts", "src/value.txt"],
        )
        self.assertNotIn("dependency-input", recovered_worker["result"]["artifacts"])
        target = Path(recovered_worker["worktree"])
        self.assertEqual((target / untracked_path).read_text(encoding="utf-8"), "untracked recovery fixture\n")
        self.assertEqual(self.worktrees.diff_patch(target, self.base_sha), source_patch)
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )

    def test_cli_dry_run_captures_root_untracked_without_dependency_input(self) -> None:
        untracked_path = "src/root-continuation.ts"
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            untracked_path=untracked_path,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        args = build_parser().parse_args([
            "--home", str(self.state_root),
            "task", "resume-blocked-worktree", contract.task_id, "worker",
            "--expected-revision", str(blocked["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "inspect the root fixture capture without authorization",
            "--confirm-recovery", "--preserve-untracked", "--dry-run",
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        payload = json.loads(output.getvalue())
        recovery = payload["recovery"]
        self.assertTrue(payload["dry_run"])
        self.assertEqual(recovery["schema_version"], 7)
        self.assertEqual(recovery["untracked_paths"], [untracked_path])
        self.assertNotIn("dependency_input_ref", recovery)
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )

    def test_root_untracked_recovery_rejects_paths_outside_the_node_scope(self) -> None:
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            allowed_scope=(".",),
            write_scopes=("src",),
            untracked_path="other-root-continuation.ts",
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")

        with self.assertRaisesRegex(StateConflictError, "outside the blocked node write scope"):
            self.store.capture_and_resume_blocked_worktree(
                contract.task_id,
                "worker",
                expected_revision=blocked["state_revision"],
                expected_attempt=worker["attempt"],
                reason="reject an untracked root file outside the node scope",
                preserve_untracked=True,
            )
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertTrue((source / "other-root-continuation.ts").is_file())

    def test_root_untracked_recovery_rejects_symlinks(self) -> None:
        untracked_path = "src/root-continuation.ts"
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            untracked_path=untracked_path,
        )
        untracked = source / untracked_path
        untracked.unlink()
        untracked.symlink_to(source / "src" / "value.txt")
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")

        with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "must not be a symlink"):
            self.store.capture_and_resume_blocked_worktree(
                contract.task_id,
                "worker",
                expected_revision=blocked["state_revision"],
                expected_attempt=worker["attempt"],
                reason="reject a symlink instead of preserving untrusted content",
                preserve_untracked=True,
            )
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertTrue(untracked.is_symlink())

    def test_root_untracked_recovery_with_generated_residue_uses_v8(self) -> None:
        untracked_path = "src/root-continuation.ts"
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'; "
            "assert Path('src/root-continuation.ts').read_text() == 'untracked recovery fixture\\n'\""
        )
        contract, blocked, source, source_patch = self._blocked_task(
            acceptance_command=command,
            python_residue=True,
            untracked_path=untracked_path,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
        authorization = self.store.capture_and_resume_blocked_worktree(
            contract.task_id,
            "worker",
            expected_revision=blocked["state_revision"],
            expected_attempt=worker["attempt"],
            reason="archive only the declared bytecode residue and preserve the root patch",
            preserve_untracked=True,
        )
        recovery = authorization["recovery"]
        self.assertEqual(recovery["schema_version"], 8)
        self.assertEqual(recovery["untracked_paths"], [untracked_path])
        self.assertEqual(
            recovery["generated_residue_paths"],
            ["tests/__pycache__/fixture.cpython-313.pyc"],
        )
        self.assertNotIn("dependency_input_ref", recovery)
        self.assertFalse(residue.exists())

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("root-v8-recovery-worker")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("recovery must not dispatch a model"),
            ) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        recovered = self.store.get_task(contract.task_id)
        recovered_worker = next(node for node in recovered["nodes"] if node["node_id"] == "worker")
        self.assertEqual((recovered_worker["state"], recovered_worker["attempt"]), ("accepted", 2))
        target = Path(recovered_worker["worktree"])
        self.assertEqual((target / untracked_path).read_text(encoding="utf-8"), "untracked recovery fixture\n")
        self.assertFalse((target / "tests" / "__pycache__").exists())
        self.assertEqual(self.worktrees.diff_patch(target, self.base_sha), source_patch)
        self.assertEqual(
            DirtyWorktreeRecovery.captured_patch(source, self.base_sha, (untracked_path,)),
            source_patch,
        )

    def test_dependent_recovery_missing_input_cannot_use_a_root_receipt(self) -> None:
        contract, blocked, source, _, _ = self._blocked_dependent_task(
            untracked_path="src/dependent-continuation.ts",
            record_dependency_input=False,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        self.assertEqual(worker["depends_on"], ["schema"])
        self.assertEqual(worker["result"]["artifacts"], {})

        with self.assertRaisesRegex(
            StateConflictError,
            "requires a recorded dependency input",
        ):
            self.store.capture_and_resume_blocked_worktree(
                contract.task_id,
                "worker",
                expected_revision=blocked["state_revision"],
                expected_attempt=worker["attempt"],
                reason="refuse to replace missing dependency provenance with a root receipt",
                preserve_untracked=True,
            )
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertTrue((source / "src" / "dependent-continuation.ts").is_file())

    def test_dependent_recovery_rejects_a_submitted_root_receipt(self) -> None:
        untracked_path = "src/dependent-continuation.ts"
        contract, blocked, source, _, _ = self._blocked_dependent_task(
            untracked_path=untracked_path,
            record_dependency_input=False,
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        submitted_root_receipt = {
            "schema_version": 7,
            "source_attempt": worker["attempt"],
            "source_worktree": str(source),
            "source_branch": self.worktrees.branch_name(contract.task_id, "worker", worker["attempt"]),
            "base_sha": contract.base_sha,
            "changed_paths": worker["result"]["changed_paths"],
            "untracked_paths": [untracked_path],
            "patch_ref": "sha256:" + "0" * 64,
            "patch_sha256": "0" * 64,
        }

        with self.assertRaisesRegex(
            StateConflictError,
            "root dirty-worktree recovery receipt cannot reproduce a dependent worker",
        ):
            self.store.resume_blocked_worktree(
                contract.task_id,
                "worker",
                expected_revision=blocked["state_revision"],
                expected_attempt=worker["attempt"],
                reason="reject a submitted root receipt for a dependent worker",
                recovery=submitted_root_receipt,
            )
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

    def test_recovery_acceptance_environment_rejects_path_override(self) -> None:
        with self.assertRaisesRegex(
            DirtyWorktreeRecoveryError,
            "environment variable PATH is not permitted",
        ):
            DirtyWorktreeRecovery._parse_command("PATH=/tmp python -V")

    def test_cli_and_mcp_share_the_same_blocked_capture_path(self) -> None:
        command = f"{sys.executable} -c \"raise SystemExit(0)\""
        contract, blocked, _, _ = self._blocked_task(acceptance_command=command)
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        args = build_parser().parse_args(
            [
                "--home",
                str(self.state_root),
                "task",
                "resume-blocked-worktree",
                contract.task_id,
                "worker",
                "--expected-revision",
                str(blocked["state_revision"]),
                "--expected-attempt",
                str(worker["attempt"]),
                "--reason",
                "resume through the shared capture path",
                "--confirm-recovery",
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        receipt = json.loads(output.getvalue())

        self.assertEqual(receipt["action"], "resume-blocked-worktree")
        self.assertEqual(receipt["task"]["state"], "queued")
        self.assertEqual(receipt["next_attempt"], 2)
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "queued")

    def _blocked_dependent_task(
        self,
        *,
        untracked_path: str | None = None,
        record_dependency_input: bool = True,
    ) -> tuple[TaskContract, dict, Path, str, bytes]:
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('src/schema.txt').read_text() == 'schema\\n'; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'\""
        )
        contract = TaskContract(
            task_id="blocked-dependent-worktree",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="recover one dependent dirty worker from its recorded input",
            allowed_scope=("src",),
            acceptance_commands=(command,),
            executor_model="fixture",
            verifier_model="fixture",
        )
        schema = NodeSpec(
            "schema",
            contract.task_id,
            "create accepted ancestor patch",
            "fixture",
            "fixture",
            "create schema source",
            write_scopes=("src",),
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "recover dependent worker patch",
            "fixture",
            "fixture",
            "change only worker source",
            depends_on=("schema",),
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify",
            contract.task_id,
            "compose accepted closure",
            "fixture",
            "fixture",
            "verify",
            depends_on=("schema", "worker"),
            verifier=True,
        )
        self.store.create_task(contract, [schema, worker, verifier], "blocked-dependent-create")
        self.store.queue_task(contract.task_id)
        artifacts = ArtifactStore(self.state_root / "artifacts")

        claimed_schema = self.store.claim_ready_node("schema-worker", self.epoch)
        assert claimed_schema is not None
        schema_worktree = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            contract.task_id,
            schema.node_id,
            int(claimed_schema["attempt"]),
        )
        self.store.assign_worktree(
            contract.task_id,
            schema.node_id,
            str(schema_worktree),
            attempt=int(claimed_schema["attempt"]),
            coordinator_epoch=int(claimed_schema["coordinator_epoch"]),
            lease_epoch=int(claimed_schema["lease_epoch"]),
        )
        (schema_worktree / "src" / "schema.txt").write_text("schema\n", encoding="utf-8")
        schema_patch = self.worktrees.diff_patch(schema_worktree, self.base_sha)
        self.store.settle_claimed(
            claimed_schema,
            NodeResult(
                "succeeded",
                "accepted ancestor completed",
                artifacts={"patch": artifacts.put_bytes(schema_patch, "patch")},
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/schema.txt",),
                checks=("fixture ancestor patch",),
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )

        claimed_worker = self.store.claim_ready_node("dependent-worker", self.epoch)
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
            artifacts,
            self.worktrees,
        )
        assert dependency_input is not None
        dependency_input_ref = artifacts.put_text(
            json.dumps(dependency_input.receipt, ensure_ascii=False, sort_keys=True),
            "dependency-input.json",
        )
        (source / "src" / "value.txt").write_text("patched\n", encoding="utf-8")
        untracked_paths: tuple[str, ...] = ()
        if untracked_path is not None:
            added = source / untracked_path
            added.parent.mkdir(parents=True, exist_ok=True)
            added.write_text("untracked recovery fixture\n", encoding="utf-8")
            untracked_paths = (untracked_path,)
        source_worker_patch = DirtyWorktreeRecovery.captured_patch(
            source,
            dependency_input.input_tree_sha,
            untracked_paths,
        )
        self.store.settle_claimed(
            claimed_worker,
            NodeResult(
                "blocked",
                "fixture worker stopped after its own tracked patch",
                artifacts=(
                    {"dependency-input": dependency_input_ref}
                    if record_dependency_input
                    else {}
                ),
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/value.txt", *untracked_paths),
                checks=("fixture dependent worker blocked",),
                governance_profile=contract.governance_profile,
                verification_tier=contract.verification_tier,
            ),
        )
        return (
            contract,
            self.store.get_task(contract.task_id),
            source,
            dependency_input_ref,
            source_worker_patch,
        )

    def _authorize_dependent(
        self,
        contract: TaskContract,
        blocked: dict,
        source: Path,
        dependency_input_ref: str,
        *,
        preserve_untracked: bool = False,
        expected_checkpoint_sha: str | None = None,
    ) -> dict:
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        dependency_input = json.loads(
            ArtifactStore(self.state_root / "artifacts")
            .verify(dependency_input_ref)
            .read_text(encoding="utf-8")
        )
        recovery = self.recovery.capture(
            repository=contract.repository,
            base_sha=contract.base_sha,
            worktree=str(source),
            branch=self.worktrees.branch_name(contract.task_id, "worker", int(worker["attempt"])),
            attempt=int(worker["attempt"]),
            expected_changed_paths=tuple(worker["result"]["changed_paths"]),
            task_id=contract.task_id,
            node_id="worker",
            input_tree_sha=dependency_input["input_tree_sha"],
            dependency_input_ref=dependency_input_ref,
            expected_checkpoint_sha=expected_checkpoint_sha,
            preserve_untracked_paths=(
                DirtyWorktreeRecovery.untracked_paths(source) if preserve_untracked else ()
            ),
        )
        self.assertEqual(recovery["schema_version"], 3 if preserve_untracked else 2)
        return self.store.resume_blocked_worktree(
            contract.task_id,
            "worker",
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=int(worker["attempt"]),
            reason="replay the persisted dependency input and only the worker delta",
            recovery=recovery,
        )

    def test_offline_materializer_disables_release_age_registry_queries(self) -> None:
        worktree = self.root / "materialization-fixture"
        worktree.mkdir()
        (worktree / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.7.0"}),
            encoding="utf-8",
        )
        (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            cwd = kwargs["cwd"]
            assert isinstance(environment, dict)
            assert isinstance(cwd, Path)
            calls.append((tuple(args), cwd, dict(environment)))
            return subprocess.CompletedProcess(
                args,
                0,
                "11.25.0\n" if args[-1] == "--version" else "offline fixture ok\n",
                "",
            )

        receipt = PnpmOfflineMaterializer(binary=sys.executable, runner=runner).materialize(
            worktree,
            timeout_seconds=5,
        )
        self.assertEqual(receipt["kind"], "pnpm-offline-materialization")
        self.assertEqual(len(calls), 2)
        version, version_cwd, _ = calls[0]
        self.assertEqual(version, (sys.executable, "--version"))
        self.assertEqual(version_cwd, Path(tempfile.gettempdir()))
        install, install_cwd, environment = calls[1]
        self.assertEqual(install_cwd, worktree)
        self.assertIn("--offline", install)
        self.assertIn("--ignore-scripts", install)
        self.assertIn("--config.minimumReleaseAge=0", install)
        self.assertIn("--config.trustLockfile=true", install)
        self.assertEqual(environment["npm_config_offline"], "true")
        self.assertEqual(environment["npm_config_minimum_release_age"], "0")
        self.assertEqual(environment["npm_config_trust_lockfile"], "true")

    def test_offline_materializer_serializes_shared_store_linking(self) -> None:
        store = self.root / "pnpm-store"
        store.mkdir()
        first = self.root / "first-materialization-fixture"
        second = self.root / "second-materialization-fixture"
        for worktree in (first, second):
            worktree.mkdir()
            (worktree / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@11.7.0"}),
                encoding="utf-8",
            )
            (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

        started_install = threading.Event()
        release_install = threading.Event()
        guard = threading.Lock()
        active_installs = 0
        peak_installs = 0
        errors: list[BaseException] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal active_installs, peak_installs
            if args[0] == "/bin/cp":
                shutil.copytree(Path(args[-2]), Path(args[-1]), symlinks=True)
                return subprocess.CompletedProcess(args, 0, "template clone ok\n", "")
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
            with guard:
                active_installs += 1
                peak_installs = max(peak_installs, active_installs)
            started_install.set()
            release_install.wait(timeout=3)
            cwd = kwargs["cwd"]
            assert isinstance(cwd, Path)
            (cwd / "node_modules").mkdir()
            (cwd / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
            (cwd / "node_modules" / ".bin").mkdir()
            with guard:
                active_installs -= 1
            return subprocess.CompletedProcess(args, 0, "offline fixture ok\n", "")

        materializer = PnpmOfflineMaterializer(
            binary=sys.executable,
            store_dir=store,
            runner=runner,
        )

        def run(worktree: Path) -> None:
            try:
                materializer.materialize(worktree, timeout_seconds=5)
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        first_thread = threading.Thread(target=run, args=(first,))
        second_thread = threading.Thread(target=run, args=(second,))
        first_thread.start()
        self.assertTrue(started_install.wait(timeout=1))
        second_thread.start()
        time.sleep(0.2)
        self.assertEqual(peak_installs, 1)
        release_install.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peak_installs, 1)

    def test_offline_materializer_rejects_known_hanging_pnpm_before_install(self) -> None:
        worktree = self.root / "old-pnpm-fixture"
        worktree.mkdir()
        (worktree / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.7.0"}),
            encoding="utf-8",
        )
        (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(args))
            return subprocess.CompletedProcess(args, 0, "11.7.0\n", "")

        with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "at least 11.25.0"):
            PnpmOfflineMaterializer(binary=sys.executable, runner=runner).materialize(
                worktree,
                timeout_seconds=5_400,
            )
        self.assertEqual(calls, [(sys.executable, "--version")])

    def test_offline_materializer_uses_explicit_store_and_hard_timeout(self) -> None:
        worktree = self.root / "configured-pnpm-fixture"
        store = self.root / "pnpm-store"
        worktree.mkdir()
        store.mkdir()
        (worktree / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.7.0"}),
            encoding="utf-8",
        )
        (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        calls: list[tuple[tuple[str, ...], int]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout = kwargs["timeout"]
            assert isinstance(timeout, int)
            calls.append((tuple(args), timeout))
            if args[0] == "/bin/cp":
                shutil.copytree(Path(args[-2]), Path(args[-1]), symlinks=True)
                return subprocess.CompletedProcess(args, 0, "template clone ok\n", "")
            if args[-1] != "--version":
                cwd = kwargs["cwd"]
                assert isinstance(cwd, Path)
                (cwd / "node_modules").mkdir()
                (cwd / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
                (cwd / "node_modules" / ".bin").mkdir()
            return subprocess.CompletedProcess(
                args,
                0,
                "11.25.0\n" if args[-1] == "--version" else "offline fixture ok\n",
                "",
            )

        receipt = PnpmOfflineMaterializer(
            binary=sys.executable,
            store_dir=store,
            runner=runner,
        ).materialize(worktree, timeout_seconds=5_400)

        self.assertEqual(receipt["materialization_timeout_seconds"], 360)
        self.assertEqual(receipt["store_dir"], str(store.resolve()))
        self.assertEqual([timeout for _command, timeout in calls], [360, 360, 360])
        self.assertIn("--pm-on-fail=ignore", calls[1][0])
        self.assertEqual(calls[1][0][-2:], ("--store-dir", str(store.resolve())))

    def test_offline_materializer_reuses_isolated_template_for_matching_inputs(self) -> None:
        store = self.root / "template-store"
        template_root = self.root / "template-root"
        first = self.root / "template-first"
        second = self.root / "template-second"
        changed = self.root / "template-changed"
        interrupted = self.root / "template-interrupted"
        store.mkdir()
        for worktree in (first, second, changed, interrupted):
            worktree.mkdir()
            (worktree / "native" / "fixture").mkdir(parents=True)
            (worktree / "packages" / "fixture").mkdir(parents=True)
            (worktree / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@11.7.0"}),
                encoding="utf-8",
            )
            (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: 9.0\n", encoding="utf-8")
        (changed / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.7.0", "name": "changed-input"}),
            encoding="utf-8",
        )
        installs = 0

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal installs
            command = tuple(args)
            if command[0] == "/bin/cp":
                shutil.copytree(Path(command[-2]), Path(command[-1]), symlinks=True)
                return subprocess.CompletedProcess(args, 0, "template clone ok\n", "")
            if command[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
            installs += 1
            cwd = kwargs["cwd"]
            assert isinstance(cwd, Path)
            (cwd / "node_modules").mkdir()
            (cwd / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
            (cwd / "node_modules" / ".bin").mkdir()
            (cwd / "node_modules" / "fixture.txt").write_text("ready\n", encoding="utf-8")
            (cwd / "node_modules" / "workspace").symlink_to("../packages/fixture")
            package_linker = cwd / "packages" / "fixture" / "node_modules"
            package_linker.mkdir()
            (package_linker / "fixture.txt").write_text("package-ready\n", encoding="utf-8")
            native_linker = cwd / "native" / "fixture" / "node_modules"
            native_linker.mkdir()
            (native_linker / "fixture.txt").write_text("native-ready\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "offline fixture ok\n", "")

        materializer = PnpmOfflineMaterializer(
            binary=sys.executable,
            store_dir=store,
            template_dir=template_root,
            runner=runner,
        )
        first_receipt = materializer.materialize(first, timeout_seconds=5_400)
        second_receipt = materializer.materialize(second, timeout_seconds=5_400)
        (interrupted / "node_modules" / ".pnpm").mkdir(parents=True)
        interrupted_receipt = materializer.materialize(interrupted, timeout_seconds=5_400)
        changed_receipt = materializer.materialize(changed, timeout_seconds=5_400)

        self.assertEqual(installs, 2)
        self.assertEqual(first_receipt["template"]["state"], "seeded")
        self.assertEqual(second_receipt["template"]["state"], "hit")
        self.assertEqual(interrupted_receipt["template"]["state"], "hit")
        self.assertTrue(interrupted_receipt["template"]["replaced_interrupted_node_modules"])
        self.assertEqual(changed_receipt["template"]["state"], "seeded")
        self.assertEqual((second / "node_modules" / "fixture.txt").read_text(encoding="utf-8"), "ready\n")
        self.assertEqual(
            (second / "packages" / "fixture" / "node_modules" / "fixture.txt").read_text(
                encoding="utf-8"
            ),
            "package-ready\n",
        )
        self.assertEqual(
            (second / "native" / "fixture" / "node_modules" / "fixture.txt").read_text(
                encoding="utf-8"
            ),
            "native-ready\n",
        )
        self.assertTrue((interrupted / "node_modules" / ".modules.yaml").is_file())
        self.assertEqual((second / "node_modules" / "workspace").resolve(), (second / "packages" / "fixture").resolve())
        self.assertEqual(len(second_receipt["commands"]), 2)

    def test_offline_materializer_reads_authority_runtime_environment(self) -> None:
        worktree = self.root / "authority-runtime-fixture"
        store = self.root / "authority-pnpm-store"
        worktree.mkdir()
        store.mkdir()
        (worktree / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.7.0"}),
            encoding="utf-8",
        )
        (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(args))
            if args[0] == "/bin/cp":
                shutil.copytree(Path(args[-2]), Path(args[-1]), symlinks=True)
                return subprocess.CompletedProcess(args, 0, "template clone ok\n", "")
            if args[-1] != "--version":
                cwd = kwargs["cwd"]
                assert isinstance(cwd, Path)
                (cwd / "node_modules").mkdir()
                (cwd / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
                (cwd / "node_modules" / ".bin").mkdir()
            return subprocess.CompletedProcess(
                args,
                0,
                "11.25.0\n" if args[-1] == "--version" else "offline fixture ok\n",
                "",
            )

        with patch.dict(
            "os.environ",
            {
                PnpmOfflineMaterializer.BINARY_ENVIRONMENT_VARIABLE: sys.executable,
                PnpmOfflineMaterializer.STORE_ENVIRONMENT_VARIABLE: str(store),
            },
        ):
            receipt = PnpmOfflineMaterializer(runner=runner).materialize(worktree, timeout_seconds=5)

        self.assertEqual(receipt["store_dir"], str(store.resolve()))
        self.assertEqual(calls[0], (sys.executable, "--version"))
        self.assertEqual(calls[1][-2:], ("--store-dir", str(store.resolve())))

    def test_recovery_acceptance_uses_process_local_pnpm_shim(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs.get("env")
            self.assertIsInstance(environment, dict)
            calls.append((tuple(args), dict(environment)))
            return subprocess.CompletedProcess(args, 0, "fixture ok\n", "")

        recovery = DirtyWorktreeRecovery(self.store.artifacts, self.worktrees, runner=runner)
        shim_environment = {"PATH": "fixture-shim", "WORKER_SHIM": "enabled"}
        with patch(
            "codex_workbench.dirty_worktree_recovery.codex_subscription_environment",
            return_value=shim_environment,
        ) as subscription_environment:
            outcome = recovery._run_command(("pnpm", "exec", "vitest", "--version"), self.root, 5)

        self.assertEqual(outcome.exit_code, 0)
        subscription_environment.assert_called_once()
        shim_directory = subscription_environment.call_args.kwargs["pnpm_shim_directory"]
        self.assertFalse(shim_directory.exists())
        self.assertEqual(calls[0][0], ("pnpm", "exec", "vitest", "--version"))
        self.assertEqual(calls[0][1]["PATH"], "fixture-shim")
        self.assertEqual(calls[0][1]["WORKER_SHIM"], "enabled")
        self.assertEqual(calls[0][1]["npm_config_offline"], "true")
        self.assertEqual(calls[0][1]["CI"], "true")

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

    def test_python_bytecode_residue_is_archived_then_excluded_from_recovery_patch(self) -> None:
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'\""
        )
        contract, blocked, source, source_patch = self._blocked_task(
            acceptance_command=command,
            python_residue=True,
        )
        residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"

        authorization = self._authorize(contract, blocked, source)
        recovery = authorization["recovery"]
        self.assertEqual(recovery["schema_version"], 4)
        self.assertEqual(recovery["changed_paths"], ["src/value.txt"])
        self.assertEqual(
            recovery["generated_residue_paths"],
            ["tests/__pycache__/fixture.cpython-313.pyc"],
        )
        self.assertFalse(residue.exists())
        residue_receipt = json.loads(
            self.store.artifacts.verify(recovery["generated_residue_ref"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(residue_receipt["missing_paths"], [])
        content_ref = residue_receipt["observed_files"][0]["content_ref"]
        self.assertEqual(self.store.artifacts.verify(content_ref).read_bytes(), b"fixture bytecode\0")

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("generated-residue-recovery")
            assert claimed is not None
            coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual(
            (worker["state"], worker["attempt"]),
            ("accepted", 2),
            task["blocker"],
        )
        self.assertEqual(
            worker["result"]["artifacts"]["generated-residue"],
            recovery["generated_residue_ref"],
        )
        self.assertFalse((Path(worker["worktree"]) / "tests" / "__pycache__").exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch,
        )

    def test_already_removed_reported_bytecode_residue_does_not_wedge_recovery(self) -> None:
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "assert Path('src/value.txt').read_text() == 'patched\\n'\""
        )
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=command,
            python_residue=True,
        )
        residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
        residue.unlink()
        residue.parent.rmdir()

        authorization = self._authorize(contract, blocked, source)
        recovery = authorization["recovery"]
        residue_receipt = json.loads(
            self.store.artifacts.verify(recovery["generated_residue_ref"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            residue_receipt["missing_paths"],
            ["tests/__pycache__/fixture.cpython-313.pyc"],
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("missing-residue-recovery")
            assert claimed is not None
            coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual(
            (worker["state"], worker["attempt"]),
            ("accepted", 2),
            task["blocker"],
        )

    def test_bytecode_named_symlink_is_not_treated_as_disposable_residue(self) -> None:
        contract, blocked, source, _ = self._blocked_task(
            acceptance_command=f"{sys.executable} -c \"raise SystemExit(0)\"",
            python_residue=True,
        )
        residue = source / "tests" / "__pycache__" / "fixture.cpython-313.pyc"
        residue.unlink()
        residue.symlink_to(source / "src" / "value.txt")

        with self.assertRaisesRegex(
            DirtyWorktreeRecoveryError,
            "regular non-symlink",
        ):
            self._authorize(contract, blocked, source)

        self.assertTrue(residue.is_symlink())
        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "blocked")

    def test_dependent_recovery_replays_recorded_input_and_only_worker_delta(self) -> None:
        self._exercise_dependent_recovery(checkpoint=False)

    def test_dependent_checkpoint_preserves_accepted_ancestor_and_worker_delta(self) -> None:
        self._exercise_dependent_recovery(checkpoint=True)

    def _exercise_dependent_recovery(self, *, checkpoint: bool) -> None:
        contract, blocked, source, dependency_input_ref, source_worker_patch = self._blocked_dependent_task()
        checkpoint_sha = None
        if checkpoint:
            self._git(source, "add", "src")
            self._git(source, "commit", "-m", "checkpoint with dependency input")
            checkpoint_sha = self._git(source, "rev-parse", "HEAD")
        source_patch_before = subprocess.run(
            ["git", "-C", str(source), "diff", "--binary", self.base_sha],
            check=True,
            capture_output=True,
        ).stdout
        self._authorize_dependent(contract, blocked, source, dependency_input_ref,
                                  expected_checkpoint_sha=checkpoint_sha)
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("dependent-recovery-worker")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("model executor must not run"),
            ) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual(worker["result"]["changed_paths"], ["src/value.txt"])
        self.assertEqual(worker["result"]["artifacts"]["patch"], worker["result"]["artifacts"]["recovery-snapshot"])
        self.assertEqual(worker["result"]["artifacts"]["dependency-input"], dependency_input_ref)
        target = Path(worker["worktree"])
        self.assertEqual((target / "src" / "schema.txt").read_text(encoding="utf-8"), "schema\n")
        self.assertEqual((target / "src" / "value.txt").read_text(encoding="utf-8"), "patched\n")
        recovered_input = load_recorded_dependency_input(
            ArtifactStore(self.state_root / "artifacts"),
            dependency_input_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=contract.base_sha,
        )
        self.assertEqual(
            self.worktrees.diff_patch(target, recovered_input.input_tree_sha),
            source_worker_patch,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "diff", "--binary", self.base_sha],
                check=True,
                capture_output=True,
            ).stdout,
            source_patch_before,
        )

        verifier_input = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            contract.task_id,
            "verify",
            1,
        )
        composed = apply_accepted_ancestor_patches(
            task,
            "verify",
            verifier_input,
            ArtifactStore(self.state_root / "artifacts"),
            self.worktrees,
        )
        self.assertIsNotNone(composed)
        self.assertEqual((verifier_input / "src" / "schema.txt").read_text(encoding="utf-8"), "schema\n")
        self.assertEqual((verifier_input / "src" / "value.txt").read_text(encoding="utf-8"), "patched\n")

    def test_dependent_recovery_requires_explicit_preservation_for_untracked_files(self) -> None:
        contract, blocked, source, dependency_input_ref, _ = self._blocked_dependent_task(
            untracked_path="src/loader-continuation.host.spec.ts"
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        with self.assertRaisesRegex(Exception, "explicit preservation"):
            self.recovery.capture(
                repository=contract.repository,
                base_sha=contract.base_sha,
                worktree=str(source),
                branch=self.worktrees.branch_name(contract.task_id, "worker", int(worker["attempt"])),
                attempt=int(worker["attempt"]),
                expected_changed_paths=tuple(worker["result"]["changed_paths"]),
                task_id=contract.task_id,
                node_id="worker",
                input_tree_sha=json.loads(
                    ArtifactStore(self.state_root / "artifacts").verify(dependency_input_ref).read_text(encoding="utf-8")
                )["input_tree_sha"],
                dependency_input_ref=dependency_input_ref,
            )
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )

    def test_explicit_dependent_untracked_recovery_preserves_a1_and_composes_closure(self) -> None:
        untracked_path = "src/loader-continuation.host.spec.ts"
        contract, blocked, source, dependency_input_ref, source_worker_patch = self._blocked_dependent_task(
            untracked_path=untracked_path
        )
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        self._authorize_dependent(
            contract,
            blocked,
            source,
            dependency_input_ref,
            preserve_untracked=True,
        )
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed = coordinator._claim_next_ready_node("dependent-untracked-recovery-worker")
            assert claimed is not None
            with patch.object(
                coordinator,
                "_executor",
                side_effect=AssertionError("model executor must not run"),
            ) as executor:
                coordinator._execute_claimed(claimed)
            executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual(
            worker["result"]["changed_paths"],
            ["src/loader-continuation.host.spec.ts", "src/value.txt"],
        )
        target = Path(worker["worktree"])
        self.assertEqual(
            (target / untracked_path).read_text(encoding="utf-8"),
            "untracked recovery fixture\n",
        )
        recovered_input = load_recorded_dependency_input(
            ArtifactStore(self.state_root / "artifacts"),
            dependency_input_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=contract.base_sha,
        )
        self.assertEqual(
            self.worktrees.diff_patch(target, recovered_input.input_tree_sha),
            source_worker_patch,
        )
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )
        verifier_input = self.worktrees.prepare(
            contract.repository, contract.base_sha, contract.task_id, "verify", 1
        )
        apply_accepted_ancestor_patches(
            task,
            "verify",
            verifier_input,
            ArtifactStore(self.state_root / "artifacts"),
            self.worktrees,
        )
        self.assertEqual(
            (verifier_input / untracked_path).read_text(encoding="utf-8"),
            "untracked recovery fixture\n",
        )

    def test_cli_dry_run_preserves_untracked_only_when_explicitly_requested(self) -> None:
        contract, blocked, source, _, _ = self._blocked_dependent_task(
            untracked_path="src/loader-continuation.host.spec.ts"
        )
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        source_status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        args = build_parser().parse_args([
            "--home", str(self.state_root),
            "task", "resume-blocked-worktree", contract.task_id, "worker",
            "--expected-revision", str(blocked["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "preserve the declared in-scope fixture file on clean a2",
            "--confirm-recovery", "--preserve-untracked", "--dry-run",
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        recovery = json.loads(output.getvalue())["recovery"]
        self.assertEqual(recovery["schema_version"], 3)
        self.assertEqual(recovery["untracked_paths"], ["src/loader-continuation.host.spec.ts"])
        self.assertEqual(
            self._git(source, "status", "--porcelain=v1", "--untracked-files=all"),
            source_status,
        )

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


    def test_cli_dry_run_preserves_recorded_dependency_input(self) -> None:
        command = f"{sys.executable} -c \"raise SystemExit(0)\""
        contract, blocked, source, dependency_input_ref, source_worker_patch = self._blocked_dependent_task()
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        events_before = self.store.read_events(task_id=contract.task_id)
        args = build_parser().parse_args([
            "--home", str(self.state_root),
            "task", "resume-blocked-worktree", contract.task_id, "worker",
            "--expected-revision", str(blocked["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "inspect the dependent worker delta before authorizing recovery",
            "--confirm-recovery", "--dry-run",
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_task(args), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["source_capture_read_only"])
        recovery = payload["recovery"]
        self.assertEqual(recovery["schema_version"], 2)
        self.assertEqual(recovery["dependency_input_ref"], dependency_input_ref)
        recovered_input = load_recorded_dependency_input(
            ArtifactStore(self.state_root / "artifacts"),
            dependency_input_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=contract.base_sha,
        )
        self.assertEqual(recovery["input_tree_sha"], recovered_input.input_tree_sha)
        self.assertEqual(
            self.worktrees.diff_patch(source, recovered_input.input_tree_sha),
            source_worker_patch,
        )
        self.assertEqual(self.store.get_task(contract.task_id), blocked)
        self.assertEqual(self.store.read_events(task_id=contract.task_id), events_before)


if __name__ == "__main__":
    unittest.main()
