from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.executors import ExecutionRequest, validate_worker_scope
from codex_workbench.model import NodeResult
from codex_workbench.worktrees import (
    WorktreeManager,
    normalize_scope,
    scope_access_conflicts,
    scope_allows,
)


class ScopeTests(unittest.TestCase):
    def test_normalizes_cross_platform_relative_scopes(self) -> None:
        self.assertEqual(normalize_scope("./src\\parser/"), "src/parser")
        with self.assertRaises(ValueError):
            normalize_scope("../outside")
        with self.assertRaises(ValueError):
            normalize_scope("C:\\outside")

    def test_authorization_uses_scope_containment(self) -> None:
        self.assertTrue(scope_allows("src/parser/tokenizer.py", ["src"], []))
        self.assertFalse(scope_allows("src/private/token.txt", ["src"], ["src/private"]))

    def test_access_matrix_blocks_writes_but_not_read_read(self) -> None:
        self.assertTrue(scope_access_conflicts((), ("src/parser",), (), ("src/parser/tokenizer",)))
        self.assertTrue(scope_access_conflicts(("src/parser",), (), (), ("src/parser/tokenizer",)))
        self.assertFalse(scope_access_conflicts(("src/parser",), (), ("src/parser/tokenizer",), ()))
        self.assertFalse(scope_access_conflicts((), ("src/parser",), (), ("src/renderer",)))

    def test_worker_patch_must_stay_inside_node_write_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base_sha = self._repository(Path(directory))
            (repository / "src" / "owned.txt").write_text("changed\n")
            (repository / "src" / "other.txt").write_text("changed\n")

            result = validate_worker_scope(
                WorktreeManager(Path(directory) / "worktrees"),
                self._request(repository, base_sha, write_scopes=("src/owned.txt",)),
                NodeResult(status="succeeded", summary="worker reported success"),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("outside node write scopes", result.summary)
            self.assertIn("src/other.txt", result.summary)

    def test_empty_node_write_scope_rejects_model_worker_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base_sha = self._repository(Path(directory))
            (repository / "src" / "owned.txt").write_text("changed\n")

            result = validate_worker_scope(
                WorktreeManager(Path(directory) / "worktrees"),
                self._request(repository, base_sha, write_scopes=()),
                NodeResult(status="succeeded", summary="worker reported success"),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("declares no write scopes", result.summary)
            self.assertEqual(result.changed_paths, ("src/owned.txt",))

    def test_worker_patch_inside_node_and_task_scopes_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base_sha = self._repository(Path(directory))
            (repository / "src" / "owned.txt").write_text("changed\n")

            result = validate_worker_scope(
                WorktreeManager(Path(directory) / "worktrees"),
                self._request(repository, base_sha, write_scopes=("src",)),
                NodeResult(status="succeeded", summary="worker reported success"),
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.changed_paths, ("src/owned.txt",))

    def test_task_scope_remains_the_upper_bound_for_node_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, base_sha = self._repository(Path(directory))
            (repository / "outside.txt").write_text("changed\n")

            result = validate_worker_scope(
                WorktreeManager(Path(directory) / "worktrees"),
                self._request(repository, base_sha, write_scopes=(".",)),
                NodeResult(status="succeeded", summary="worker reported success"),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("outside the task contract", result.summary)
            self.assertEqual(result.changed_paths, ("outside.txt",))

    @staticmethod
    def _repository(root: Path) -> tuple[Path, str]:
        repository = root / "repository"
        (repository / "src").mkdir(parents=True)
        (repository / "src" / "owned.txt").write_text("base\n")
        (repository / "src" / "other.txt").write_text("base\n")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"],
            cwd=repository,
            check=True,
        )
        subprocess.run(["git", "add", "src"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repository, base_sha

    @staticmethod
    def _request(
        repository: Path,
        base_sha: str,
        *,
        write_scopes: tuple[str, ...],
    ) -> ExecutionRequest:
        return ExecutionRequest(
            task_id="task",
            node_id="worker",
            attempt=1,
            contract={
                "base_sha": base_sha,
                "allowed_scope": ["src"],
                "forbidden_scope": [],
            },
            spec={
                "executor": "codex",
                "verifier": False,
                "write_scopes": list(write_scopes),
            },
            worktree=repository,
        )


class WorktreePathTests(unittest.TestCase):
    @staticmethod
    def _symlinked_root(root: Path) -> tuple[Path, Path]:
        physical_root = root / "physical-worktrees"
        physical_root.mkdir()
        linked_root = root / "worktrees"
        linked_root.symlink_to(physical_root, target_is_directory=True)
        return linked_root, physical_root.resolve(strict=True)

    def test_prepare_registers_new_worktree_at_physical_path_under_symlinked_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_sha = ScopeTests._repository(root)
            linked_root, physical_root = self._symlinked_root(root)

            worktree = WorktreeManager(linked_root).prepare(
                str(repository), base_sha, "task", "worker", 1
            )

            expected = physical_root / "task" / "worker-a1"
            registration = subprocess.run(
                ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            actual_base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            actual_branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(worktree, expected)
            self.assertTrue(worktree.is_dir())
            self.assertEqual(actual_base, base_sha)
            self.assertEqual(actual_branch, WorktreeManager.branch_name("task", "worker", 1))
            self.assertIn(f"worktree {expected}", registration)
            self.assertNotIn(str(linked_root / "task" / "worker-a1"), registration)

    def test_prepare_reuses_legacy_symlinked_worktree_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_sha = ScopeTests._repository(root)
            linked_root, _ = self._symlinked_root(root)
            legacy = linked_root / "task" / "worker-a1"
            legacy.parent.mkdir(parents=True)
            branch = WorktreeManager.branch_name("task", "worker", 1)
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "add", "-b", branch, str(legacy), base_sha],
                check=True,
                capture_output=True,
            )
            before = subprocess.run(
                ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

            worktree = WorktreeManager(linked_root).prepare(
                str(repository), base_sha, "task", "worker", 1
            )

            after = subprocess.run(
                ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktree, legacy.resolve(strict=True))
            self.assertEqual(after, before)
            self.assertEqual(
                sum(line.startswith("worktree ") for line in after.splitlines()),
                2,
            )


if __name__ == "__main__":
    unittest.main()
