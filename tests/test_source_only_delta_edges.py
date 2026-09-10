from __future__ import annotations

import unittest
from unittest.mock import patch

from codex_workbench.dirty_worktree_recovery import (
    DirtyWorktreeRecoveryError,
    inspect_recovery_source_delta,
)
from tests.test_scope_normalization import ScopeNormalizationFixture


class SourceDeltaHashTests(ScopeNormalizationFixture, unittest.TestCase):
    def _inspect(self):
        return inspect_recovery_source_delta(self.repository, self.base_sha)

    def test_same_path_content_drift_changes_digest(self) -> None:
        target = self.repository / "src/task-template.spec.ts"
        target.write_text("first delta")
        first = self._inspect()
        target.write_text("other delta")
        second = self._inspect()
        self.assertEqual(first.changed_paths, second.changed_paths)
        self.assertNotEqual(first.sha256, second.sha256)
        target.write_text("first delta")
        self.assertEqual(self._inspect().sha256, first.sha256)

    def test_ignored_content_is_not_part_of_source_digest(self) -> None:
        (self.repository / ".git/info/exclude").write_text(".cache/\n")
        cache = self.repository / ".cache"
        cache.mkdir()
        ignored = cache / "unknown.bin"
        ignored.write_bytes(b"unknown before")
        before = self._inspect()
        ignored.write_bytes(b"unknown after with different size")
        self.assertEqual(self._inspect().sha256, before.sha256)
        self.assertNotIn(".cache/unknown.bin", before.changed_paths)

    def test_mode_and_deletion_are_bound(self) -> None:
        target = self.repository / "src/task-template.spec.ts"
        target.write_text("delta")
        target.chmod(0o644)
        before = self._inspect()
        target.chmod(0o755)
        executable = self._inspect()
        self.assertNotEqual(executable.sha256, before.sha256)
        target.unlink()
        deleted = self._inspect()
        self.assertNotEqual(deleted.sha256, executable.sha256)
        self.assertEqual(deleted.changed_paths, ("src/task-template.spec.ts",))

    def test_untracked_file_addition_changes_digest(self) -> None:
        before = self._inspect()
        (self.repository / "src/extra.ts").write_text("extra")
        after = self._inspect()
        self.assertNotEqual(before.sha256, after.sha256)
        self.assertIn("src/extra.ts", after.untracked_paths)

    def test_symlink_delta_is_rejected(self) -> None:
        (self.repository / "src/link.ts").symlink_to("task-template.spec.ts")
        with self.assertRaises(DirtyWorktreeRecoveryError):
            self._inspect()

    def test_scope_rejection_precedes_content_hashing(self) -> None:
        (self.repository / "outside.bin").write_bytes(b"must not be hashed")
        def reject(paths):
            self.assertIn("outside.bin", paths)
            raise DirtyWorktreeRecoveryError("outside declared scope")
        with patch("codex_workbench.dirty_worktree_recovery._read_source_delta_file") as read:
            with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "outside declared scope"):
                inspect_recovery_source_delta(
                    self.repository, self.base_sha, validate_paths=reject,
                )
            read.assert_not_called()
