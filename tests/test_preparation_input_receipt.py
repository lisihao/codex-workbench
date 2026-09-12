from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.dirty_worktree_recovery import PnpmOfflineMaterializer
from codex_workbench.executors import ExecutionRequest
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class PreparationInputReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        (self.repository / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
        )
        (self.repository / "pnpm-lock.yaml").write_text(
            "lockfileVersion: '9.0'\n", encoding="utf-8"
        )
        (self.repository / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (self.repository / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        self._git(self.repository, "add", ".")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.pnpm_shim = self.root / "pnpm"
        self.pnpm_shim.write_text("#!/bin/sh\nprintf '%s\\n' '11.25.0'\n", encoding="utf-8")
        self.pnpm_shim.chmod(0o755)

        self.state_root = self.root / "state"
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("preparation-input", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.contract = TaskContract(
            task_id="preparation-input",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="preserve completed accepted input when local preparation blocks",
            allowed_scope=("package.json", "pnpm-lock.yaml", "src"),
            required_artifacts=(),
            verifier_model="fixture",
        )
        self.ancestor = NodeSpec(
            "ancestor",
            self.contract.task_id,
            "produce immutable inherited input",
            "fixture",
            "fixture",
            write_scopes=("src/ancestor.txt",),
        )
        self.worker = NodeSpec(
            "work",
            self.contract.task_id,
            "consume immutable inherited input",
            "codex",
            "gpt-5.6-luna",
            "inspect the accepted input without changing it",
            depends_on=(self.ancestor.node_id,),
            read_scopes=("src/ancestor.txt",),
            write_scopes=("src/work.txt",),
        )
        self.verifier = NodeSpec(
            "verify",
            self.contract.task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            depends_on=(self.ancestor.node_id, self.worker.node_id),
            verifier=True,
        )
        self.store.create_task(
            self.contract,
            [self.ancestor, self.worker, self.verifier],
            "preparation-input-create",
        )
        self.store.queue_task(self.contract.task_id)
        self._accept_ancestor()

    def _git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _accept_ancestor(self) -> None:
        claimed = self.store.claim_ready_node("ancestor-fixture", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], self.ancestor.node_id)
        worktree = self.worktrees.prepare(
            self.contract.repository,
            self.contract.base_sha,
            self.contract.task_id,
            self.ancestor.node_id,
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            self.contract.task_id,
            self.ancestor.node_id,
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        (worktree / "src" / "ancestor.txt").write_text("accepted ancestor\n", encoding="utf-8")
        self.ancestor_patch_ref = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(worktree, self.contract.base_sha), "patch"
        )
        self.ancestor_attempt = int(claimed["attempt"])
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                "accepted ancestor prepared input",
                artifacts={"patch": self.ancestor_patch_ref},
                changed_paths=("src/ancestor.txt",),
                checks=("fixture",),
            ),
        )

    def _claim_worker(self) -> dict:
        claimed = self.store.claim_ready_node("work-fixture", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], self.worker.node_id)
        return claimed

    @staticmethod
    def _materializer(*, fail: bool) -> PnpmOfflineMaterializer:
        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
            if fail:
                return subprocess.CompletedProcess(args, 1, "", "offline store miss\n")
            cwd = kwargs["cwd"]
            assert isinstance(cwd, Path)
            node_modules = cwd / "node_modules"
            node_modules.mkdir()
            (node_modules / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
            (node_modules / ".bin").mkdir()
            return subprocess.CompletedProcess(args, 0, "offline fixture ok\n", "")

        return PnpmOfflineMaterializer(binary=sys.executable, runner=runner)

    def _coordinator(self, *, fail_materialization: bool) -> Coordinator:
        return Coordinator(
            self.store,
            self.state_root,
            coordinator_epoch=self.epoch,
            pnpm_materializer=self._materializer(fail=fail_materialization),
        )

    def _worker_node(self) -> dict:
        task = self.store.get_task(self.contract.task_id)
        return next(node for node in task["nodes"] if node["node_id"] == self.worker.node_id)

    def _assert_exact_accepted_input(self, receipt: object) -> None:
        self.assertIsInstance(receipt, dict)
        assert isinstance(receipt, dict)
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "kind": "accepted-ancestor-patch-input",
                "task_id": self.contract.task_id,
                "node_id": self.worker.node_id,
                "contract_base_sha": self.contract.base_sha,
                "input_tree_sha": receipt["input_tree_sha"],
                "ancestors": [
                    {
                        "node_id": self.ancestor.node_id,
                        "attempt": self.ancestor_attempt,
                        "patch_ref": self.ancestor_patch_ref,
                    }
                ],
            },
        )
        self.assertIsInstance(receipt["input_tree_sha"], str)
        self.assertTrue(receipt["input_tree_sha"])

    def _dependency_input_receipt(self, result: dict) -> tuple[str, dict]:
        artifacts = result["artifacts"]
        assert isinstance(artifacts, dict)
        reference = artifacts["dependency-input"]
        assert isinstance(reference, str)
        receipt = json.loads(self.store.artifacts.verify(reference).read_text(encoding="utf-8"))
        assert isinstance(receipt, dict)
        return reference, receipt

    def _accept_initial_worker(self) -> str:
        claimed = self._claim_worker()
        worktree = self.worktrees.prepare(
            self.contract.repository,
            self.contract.base_sha,
            self.contract.task_id,
            self.worker.node_id,
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            self.contract.task_id,
            self.worker.node_id,
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(self.contract.task_id),
            self.worker.node_id,
            worktree,
            self.store.artifacts,
            self.worktrees,
        )
        assert dependency_input is not None
        dependency_input_ref = self.store.artifacts.put_text(
            canonical_json(dependency_input.receipt), "dependency-input.json"
        )
        (worktree / "src" / "work.txt").write_text("accepted worker\n", encoding="utf-8")
        worker_patch_ref = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(worktree, dependency_input.input_tree_sha), "patch"
        )
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                "accepted worker prepared from accepted input",
                provider="codex",
                actual_model="gpt-5.6-luna",
                artifacts={
                    "dependency-input": dependency_input_ref,
                    "patch": worker_patch_ref,
                },
                changed_paths=("src/work.txt",),
                checks=("fixture",),
                result_kind="worker",
            ),
        )
        return dependency_input_ref

    def test_materialization_block_records_accepted_input_without_executor_call(self) -> None:
        claimed = self._claim_worker()
        coordinator = self._coordinator(fail_materialization=True)
        executor = MagicMock()
        try:
            with patch.object(coordinator, "_executor", return_value=executor) as select_executor:
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        select_executor.assert_not_called()
        task = self.store.get_task(self.contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == self.worker.node_id)
        self.assertEqual((task["state"], worker["state"]), ("blocked", "blocked"))
        result = worker["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "blocked")
        _, receipt = self._dependency_input_receipt(result)
        self._assert_exact_accepted_input(receipt)
        materialization_ref = result["artifacts"]["dependency-materialization"]
        assert isinstance(materialization_ref, str)
        materialization = json.loads(
            self.store.artifacts.verify(materialization_ref).read_text(encoding="utf-8")
        )
        self.assertEqual(materialization["status"], "blocked")

    def test_successful_preparation_passes_the_same_accepted_input_to_the_executor(self) -> None:
        claimed = self._claim_worker()
        coordinator = self._coordinator(fail_materialization=False)
        executor = MagicMock()
        requests: list[ExecutionRequest] = []

        def execute(request: ExecutionRequest) -> NodeResult:
            requests.append(request)
            self.assertIsNotNone(request.worktree)
            assert request.worktree is not None
            self.assertEqual(
                (request.worktree / "src" / "ancestor.txt").read_text(encoding="utf-8"),
                "accepted ancestor\n",
            )
            self._assert_exact_accepted_input(request.input_receipt)
            return NodeResult(
                "succeeded",
                "fixture executor consumed accepted input",
                provider="codex",
                actual_model="gpt-5.6-luna",
                result_kind="worker",
                checks=("fixture",),
            )

        try:
            with (
                patch.dict(os.environ, {"CODEX_WORKBENCH_PNPM": str(self.pnpm_shim)}),
                patch.object(coordinator, "_executor", return_value=executor) as select_executor,
            ):
                executor.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        select_executor.assert_called_once()
        executor.execute.assert_called_once()
        self.assertEqual(len(requests), 1)
        worker = self._worker_node()
        self.assertEqual(worker["state"], "accepted", worker)
        result = worker["result"]
        assert isinstance(result, dict)
        reference, receipt = self._dependency_input_receipt(result)
        self._assert_exact_accepted_input(receipt)
        self.assertEqual(requests[0].input_receipt, receipt)
        self.assertEqual(requests[0].input_receipt_ref, reference)

    def test_accepted_source_repair_records_its_frozen_input_before_materialization(self) -> None:
        source_input_ref = self._accept_initial_worker()
        claimed_verifier = self.store.claim_ready_node("verify-fixture", self.epoch)
        assert claimed_verifier is not None
        self.assertEqual(claimed_verifier["node_id"], self.verifier.node_id)
        self.store.settle_claimed(
            claimed_verifier,
            NodeResult(
                "failed",
                "repair the accepted worker source",
                verdict="needs_fix",
                repair_node_ids=(self.worker.node_id,),
                checks=("fixture",),
            ),
        )
        coordinator = self._coordinator(fail_materialization=True)
        executor = MagicMock()
        try:
            claimed_repair = coordinator._claim_next_ready_node("repair-fixture")
            assert claimed_repair is not None
            self.assertIn("accepted_source_repair", claimed_repair)
            with patch.object(coordinator, "_executor", return_value=executor) as select_executor:
                coordinator._execute_claimed(claimed_repair)
        finally:
            coordinator._pool.shutdown(wait=True)

        select_executor.assert_not_called()
        worker = self._worker_node()
        self.assertEqual((worker["state"], worker["attempt"]), ("blocked", 2))
        result = worker["result"]
        assert isinstance(result, dict)
        reference, receipt = self._dependency_input_receipt(result)
        self.assertEqual(reference, source_input_ref)
        self._assert_exact_accepted_input(receipt)


if __name__ == "__main__":
    unittest.main()
