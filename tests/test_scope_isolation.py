from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.scope_isolation import scope_entity_alias_conflicts
from codex_workbench.worktrees import WorktreeManager


class ScopeIsolationTests(unittest.TestCase):
    """Exercise live worktree and scope-entity evidence without a store."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="scope-isolation-")
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "--initial-branch=main", cwd=self.repository)
        self._git("config", "user.email", "fixture@example.invalid", cwd=self.repository)
        self._git("config", "user.name", "Workbench fixture", cwd=self.repository)
        (self.repository / "src" / "candidate").mkdir(parents=True)
        (self.repository / "src" / "running").mkdir(parents=True)
        (self.repository / "src" / "candidate" / "seed.txt").write_text("candidate\n")
        (self.repository / "src" / "running" / "seed.txt").write_text("running\n")
        self._git("add", ".", cwd=self.repository)
        self._git("commit", "-m", "fixture base", cwd=self.repository)
        self.base_sha = self._git("rev-parse", "HEAD", cwd=self.repository)
        self.manager = WorktreeManager(self.root / "worktrees")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _git(*arguments: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def _prepare_pair(self) -> tuple[Path, Path]:
        running = self.manager.prepare(
            str(self.repository), self.base_sha, "running-task", "worker", 1
        )
        candidate = self.manager.prepare(
            str(self.repository), self.base_sha, "candidate-task", "worker", 1
        )
        return candidate, running

    def test_scope_entities_observed_in_private_worktrees_remain_distinct(self) -> None:
        candidate, running = self._prepare_pair()

        evidence = scope_entity_alias_conflicts(
            candidate_worktree=candidate,
            candidate_reads=("src/candidate",),
            candidate_writes=(),
            running_worktree=running,
            running_reads=(),
            running_writes=("src/running",),
        )

        self.assertEqual(evidence, {"status": "observed_distinct", "conflicts": []})

    def test_unavailable_scope_root_is_unproven(self) -> None:
        _, running = self._prepare_pair()
        missing_candidate = self.manager.worktree_path("missing-task", "worker", 1)

        evidence = scope_entity_alias_conflicts(
            candidate_worktree=missing_candidate,
            candidate_reads=("src/candidate",),
            candidate_writes=(),
            running_worktree=running,
            running_reads=(),
            running_writes=("src/running",),
        )

        self.assertEqual(
            evidence,
            {"status": "unproven", "reason": "candidate_worktree_unavailable"},
        )

    def test_external_symlink_aliases_share_one_writable_scope_entity(self) -> None:
        shared_resource = self.root / "shared-resource"
        shared_resource.mkdir()
        (shared_resource / "seed.txt").write_text("shared\n")
        os.symlink(shared_resource, self.repository / "src" / "shared")
        os.symlink(shared_resource, self.repository / "src" / "shared-alias")
        self._git("add", ".", cwd=self.repository)
        self._git("commit", "-m", "fixture shared scope aliases", cwd=self.repository)
        self.base_sha = self._git("rev-parse", "HEAD", cwd=self.repository)
        candidate, running = self._prepare_pair()
        (running / "src" / "shared" / "external-change.txt").write_text("not in Git diff\n")
        self.assertEqual(self.manager.changed_paths(running, self.base_sha), set())

        evidence = scope_entity_alias_conflicts(
            candidate_worktree=candidate,
            candidate_reads=("src/shared-alias",),
            candidate_writes=(),
            running_worktree=running,
            running_reads=(),
            running_writes=("src/shared",),
        )

        self.assertEqual(evidence["status"], "conflict")
        self.assertEqual(len(evidence["conflicts"]), 1)
        conflict = evidence["conflicts"][0]
        self.assertEqual(conflict["candidate_access"], "read")
        self.assertEqual(conflict["candidate_scope"], "src/shared-alias")
        self.assertEqual(conflict["running_access"], "write")
        self.assertEqual(conflict["running_scope"], "src/shared")
        self.assertEqual(conflict["entity"], str(shared_resource.resolve(strict=True)))


if __name__ == "__main__":
    unittest.main()
