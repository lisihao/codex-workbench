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

    def _blocked_dependent_task(
        self,
        *,
        untracked_path: str | None = None,
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
                artifacts={"dependency-input": dependency_input_ref},
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
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            calls.append((tuple(args), dict(environment)))
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
        install, environment = calls[1]
        self.assertIn("--offline", install)
        self.assertIn("--config.minimumReleaseAge=0", install)
        self.assertEqual(environment["npm_config_offline"], "true")
        self.assertEqual(environment["npm_config_minimum_release_age"], "0")

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

        self.assertEqual(receipt["materialization_timeout_seconds"], 120)
        self.assertEqual(receipt["store_dir"], str(store.resolve()))
        self.assertEqual([timeout for _command, timeout in calls], [120, 120])
        self.assertEqual(calls[1][0][-2:], ("--store-dir", str(store.resolve())))

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

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(args))
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

    def test_dependent_recovery_replays_recorded_input_and_only_worker_delta(self) -> None:
        contract, blocked, source, dependency_input_ref, source_worker_patch = self._blocked_dependent_task()
        source_patch_before = subprocess.run(
            ["git", "-C", str(source), "diff", "--binary", self.base_sha],
            check=True,
            capture_output=True,
        ).stdout
        self._authorize_dependent(contract, blocked, source, dependency_input_ref)
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
