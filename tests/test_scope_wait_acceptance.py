from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class ScopeWaitAcceptanceTests(unittest.TestCase):
    """Exercise the evidence required before a reader bypasses a scope wait."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="scope-wait-acceptance-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main", cwd=self.repo)
        self._git("config", "user.email", "fixture@example.invalid", cwd=self.repo)
        self._git("config", "user.name", "Workbench fixture", cwd=self.repo)
        (self.repo / "src" / "shared").mkdir(parents=True)
        (self.repo / "src" / "shared" / "seed.txt").write_text("fixture\n")
        self._git("add", ".", cwd=self.repo)
        self._git("commit", "-m", "fixture base", cwd=self.repo)
        self.base_sha = self._git("rev-parse", "HEAD", cwd=self.repo)

        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("scope-fixture", "fixture-machine")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def _create_task(
        self,
        task_id: str,
        *,
        repository: Path,
        base_sha: str,
        read_scopes: tuple[str, ...] = (),
        write_scopes: tuple[str, ...] = (),
    ) -> None:
        contract = TaskContract(
            task_id=task_id,
            repository=str(repository),
            base_sha=base_sha,
            objective=f"scope fixture {task_id}",
            allowed_scope=("src",),
        )
        worker = NodeSpec(
            "worker",
            task_id,
            task_id,
            "codex",
            "gpt-5.6-luna",
            "scope fixture worker",
            read_scopes=read_scopes,
            write_scopes=write_scopes,
            ordinal=1,
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "verify",
            "fixture",
            "fixture",
            "scope fixture verifier",
            depends_on=("worker",),
            verifier=True,
            ordinal=100,
        )
        self.store.create_task(contract, [worker, verifier], f"create-{task_id}")
        self.store.queue_task(task_id)

    def _claim_writer(
        self,
        task_id: str,
        *,
        repository: Path,
        base_sha: str,
        worktree_root: Path,
        prepare_worktree: bool,
    ) -> tuple[dict[str, object], Path]:
        claimed = self.store.claim_ready_node(f"{task_id}-worker", self.epoch)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        manager = WorktreeManager(worktree_root)
        worktree = (
            manager.prepare(
                str(repository),
                base_sha,
                task_id,
                "worker",
                int(claimed["attempt"]),
            )
            if prepare_worktree
            else manager.worktree_path(task_id, "worker", int(claimed["attempt"]))
        )
        self.store.assign_worktree(
            task_id,
            "worker",
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=self.epoch,
            lease_epoch=int(claimed["lease_epoch"]),
        )
        return claimed, worktree

    def _admission_wait(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        node = next(node for node in task["nodes"] if node["node_id"] == "worker")
        wait = node["admission_wait"]
        self.assertIsNotNone(wait)
        assert wait is not None
        return wait

    def test_unknown_repository_identity_cannot_prove_private_reader_isolation(self) -> None:
        missing_repository = self.root / "repository-that-does-not-exist"
        base_sha = "a" * 40
        self._create_task(
            "unknown-writer",
            repository=missing_repository,
            base_sha=base_sha,
            write_scopes=("src/shared",),
        )
        self._create_task(
            "unknown-reader",
            repository=missing_repository,
            base_sha=base_sha,
            read_scopes=("src/shared",),
        )
        self._claim_writer(
            "unknown-writer",
            repository=missing_repository,
            base_sha=base_sha,
            worktree_root=self.state_root / "worktrees",
            prepare_worktree=False,
        )

        claimed_reader = self.store.claim_ready_node("unknown-reader-worker", self.epoch)

        self.assertIsNone(
            claimed_reader,
            "a missing Git repository must not be treated as a proven private worktree pair",
        )
        wait = self._admission_wait("unknown-reader")
        blocker = wait["blocking"]["nodes"][0]
        self.assertEqual((blocker["task_id"], blocker["node_id"]), ("unknown-writer", "worker"))
        self.assertNotEqual(blocker["isolation"]["status"], "proven_private_fixed_base")

    def test_nonexistent_candidate_worktree_cannot_prove_private_reader_isolation(self) -> None:
        worktree_root = self.state_root / "worktrees"
        self._create_task(
            "real-writer",
            repository=self.repo,
            base_sha=self.base_sha,
            write_scopes=("src/shared",),
        )
        self._create_task(
            "missing-reader-worktree",
            repository=self.repo,
            base_sha=self.base_sha,
            read_scopes=("src/shared",),
        )
        self._claim_writer(
            "real-writer",
            repository=self.repo,
            base_sha=self.base_sha,
            worktree_root=worktree_root,
            prepare_worktree=True,
        )
        candidate_path = WorktreeManager(worktree_root).worktree_path(
            "missing-reader-worktree", "worker", 1
        )
        self.assertFalse(candidate_path.exists())

        claimed_reader = self.store.claim_ready_node("missing-reader-worker", self.epoch)

        self.assertIsNone(
            claimed_reader,
            "a deterministic candidate path that does not exist is not isolation evidence",
        )
        wait = self._admission_wait("missing-reader-worktree")
        blocker = wait["blocking"]["nodes"][0]
        self.assertEqual((blocker["task_id"], blocker["node_id"]), ("real-writer", "worker"))
        self.assertNotEqual(blocker["isolation"]["status"], "proven_private_fixed_base")

    def test_nonoverlapping_declared_scopes_can_still_run_in_parallel(self) -> None:
        self._create_task("independent-writer", repository=self.repo, base_sha=self.base_sha, write_scopes=("src/shared",))
        self._create_task("independent-reader", repository=self.repo, base_sha=self.base_sha, read_scopes=("src/unrelated",))
        self._claim_writer("independent-writer", repository=self.repo, base_sha=self.base_sha,
                           worktree_root=self.state_root / "worktrees", prepare_worktree=True)
        reader = self.store.claim_ready_node("independent-reader-worker", self.epoch)
        self.assertIsNotNone(reader)
        self.assertEqual(reader["task_id"], "independent-reader")

    def test_private_worktree_paths_do_not_isolate_an_absolute_shared_resource_alias(self) -> None:
        shared_resource = self.root / "shared-resource"
        shared_resource.mkdir()
        shutil.rmtree(self.repo / "src" / "shared")
        os.symlink(shared_resource, self.repo / "src" / "shared")
        os.symlink(shared_resource, self.repo / "src" / "shared-alias")
        self._git("add", "-A", cwd=self.repo)
        self._git("commit", "-m", "fixture shared resource aliases", cwd=self.repo)
        self.base_sha = self._git("rev-parse", "HEAD", cwd=self.repo)

        worktree_root = self.state_root / "worktrees"
        self._create_task(
            "shared-resource-writer",
            repository=self.repo,
            base_sha=self.base_sha,
            write_scopes=("src/shared",),
        )
        self._create_task(
            "shared-resource-reader",
            repository=self.repo,
            base_sha=self.base_sha,
            read_scopes=("src/shared-alias",),
        )
        _, writer_path = self._claim_writer(
            "shared-resource-writer",
            repository=self.repo,
            base_sha=self.base_sha,
            worktree_root=worktree_root,
            prepare_worktree=True,
        )
        reader_path = WorktreeManager(worktree_root).prepare(
            str(self.repo),
            self.base_sha,
            "shared-resource-reader",
            "worker",
            1,
        )
        self.assertEqual(
            (writer_path / "src" / "shared").resolve(strict=True),
            shared_resource.resolve(strict=True),
        )
        self.assertEqual(
            (reader_path / "src" / "shared-alias").resolve(strict=True),
            shared_resource.resolve(strict=True),
        )

        claimed_reader = self.store.claim_ready_node("shared-resource-reader-worker", self.epoch)

        self.assertIsNone(
            claimed_reader,
            "different scope strings must not bypass a wait when both resolve to one mutable resource",
        )
        wait = self._admission_wait("shared-resource-reader")
        blocker = wait["blocking"]["nodes"][0]
        self.assertEqual((blocker["task_id"], blocker["node_id"]), ("shared-resource-writer", "worker"))


if __name__ == "__main__":
    unittest.main()
