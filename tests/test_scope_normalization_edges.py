from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.scope_normalization import (
    ScopeNormalizationError,
    inspect_scope_normalization,
    normalize_exact_path,
    parse_legacy_basename_pattern,
    scope_conflict_pairs,
)
from codex_workbench.model import NodeSpec, TaskContract
from tests.test_scope_normalization import ScopeNormalizationFixture


class ScopeNormalizationEdgesTests(unittest.TestCase):
    def test_only_canonical_single_final_basename_star_is_supported(self) -> None:
        for value in ("*", "src/**.py", "*/task.py", "src/task?.py",
                      "src/task[ab].py", "../task*.py", "/task*.py",
                      "src//task*.py", "src/./task*.py", " src/task*.py"):
            with self.subTest(value=value), self.assertRaises(ScopeNormalizationError):
                parse_legacy_basename_pattern(value)

    def test_prefix_suffix_cannot_overlap(self) -> None:
        pattern = parse_legacy_basename_pattern("src/a*a")
        with self.assertRaises(ScopeNormalizationError):
            normalize_exact_path("src/a", pattern)
        self.assertEqual(normalize_exact_path("src/aa", pattern), "src/aa")

    def test_exact_path_cannot_escape_or_change_parent(self) -> None:
        pattern = parse_legacy_basename_pattern("src/task*.py")
        for path in ("../task.py", "other/task.py", "src/../task.py",
                     "/src/task.py", "src/task*.py", "src//task.py"):
            with self.subTest(path=path), self.assertRaises(ScopeNormalizationError):
                normalize_exact_path(path, pattern)

    def test_unique_match_includes_directories_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp).resolve()
            (source / "src").mkdir()
            target = source / "src/task.py"
            target.write_text("fixture")
            with patch("codex_workbench.scope_normalization.assert_recovery_source_idle"):
                def inspect():
                    return inspect_scope_normalization(
                        source, scope_pattern="src/task*.py", exact_path="src/task.py",
                        read_scopes=("src/task*.py",), write_scopes=("src/task*.py",),
                    )
                self.assertEqual(inspect().after_write_scopes, ("src/task.py",))
                extra = source / "src/task-extra.py"
                extra.mkdir()
                with self.assertRaisesRegex(ScopeNormalizationError, "exactly one"):
                    inspect()
                extra.rmdir()
                extra.symlink_to(target)
                with self.assertRaisesRegex(ScopeNormalizationError, "exactly one"):
                    inspect()

    def test_leaf_and_parent_symlinks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp).resolve()
            (source / "real").mkdir()
            (source / "real/task.py").write_text("fixture")
            (source / "src").symlink_to(source / "real", target_is_directory=True)
            with patch("codex_workbench.scope_normalization.assert_recovery_source_idle"):
                with self.assertRaises(ScopeNormalizationError):
                    inspect_scope_normalization(
                        source, scope_pattern="src/task*.py", exact_path="src/task.py",
                        read_scopes=(), write_scopes=("src/task*.py",),
                    )
                (source / "real/link.py").symlink_to(source / "real/task.py")
                with self.assertRaises(ScopeNormalizationError):
                    inspect_scope_normalization(
                        source, scope_pattern="real/link*.py", exact_path="real/link.py",
                        read_scopes=(), write_scopes=("real/link*.py",),
                    )

    def test_read_write_conflicts_but_read_read_is_allowed(self) -> None:
        self.assertFalse(scope_conflict_pairs(("src",), (), ("src/task.py",), ()))
        self.assertTrue(scope_conflict_pairs((), ("src/task.py",), ("src",), ()))
        self.assertTrue(scope_conflict_pairs(("src/task.py",), (), (), ("src",)))
        self.assertTrue(scope_conflict_pairs((), ("src/task.py",), (), ("src",)))
        self.assertFalse(scope_conflict_pairs((), ("src/task.py",), (), ("other",)))

    def test_active_legacy_glob_is_not_silently_treated_as_disjoint(self) -> None:
        with self.assertRaises(ScopeNormalizationError):
            scope_conflict_pairs((), ("src/task.py",), (), ("src/task*.py",))


class ScopeNormalizationStoreEdgesTests(ScopeNormalizationFixture, unittest.TestCase):
    def _invoke(self, contract, **overrides):
        arguments = self._normalization_arguments(contract, **overrides)
        arguments.pop("action")
        return self.store.normalize_indeterminate_scope(**arguments)

    def test_attempt_and_confirmation_are_required_without_writes(self) -> None:
        contract, _ = self._make_indeterminate()
        preview = self._invoke(contract)
        before = self.store.get_task(contract.task_id)
        events = self.store.read_events(task_id=contract.task_id)
        for overrides in (
            {"expected_attempt": 2},
            {"dry_run": False, "expected_file_sha256": preview["file_sha256"]},
            {"dry_run": False, "confirm_scope_normalization": True},
        ):
            with self.subTest(overrides=overrides), self.assertRaises((ValueError, RuntimeError)):
                self._invoke(contract, **overrides)
            self.assertEqual(self.store.get_task(contract.task_id), before)
            self.assertEqual(self.store.read_events(task_id=contract.task_id), events)

    def test_task_forbidden_path_is_not_overridden_by_legacy_pattern(self) -> None:
        contract, _ = self._make_indeterminate()
        # Seed a legacy task constraint in this disposable database only.
        with self.store.transaction() as connection:
            row = connection.execute("SELECT contract_json FROM tasks WHERE task_id = ?",
                                     (contract.task_id,)).fetchone()
            data = json.loads(row["contract_json"])
            data["forbidden_scope"] = ["src/task-template.spec.ts"]
            connection.execute("UPDATE tasks SET contract_json = ? WHERE task_id = ?",
                               (json.dumps(data), contract.task_id))
        before = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(ValueError, "allowed/forbidden"):
            self._invoke(contract)
        self.assertEqual(self.store.get_task(contract.task_id), before)

    def test_other_running_reader_blocks_normalization(self) -> None:
        contract, _ = self._make_indeterminate()
        other = TaskContract(
            task_id="reader", repository=str(self.repository), base_sha=self.base_sha,
            objective="fixture reader", allowed_scope=("src",),
            executor_model="fixture", verifier_model="fixture",
        )
        worker = NodeSpec("read", "reader", "reader", "fixture", "fixture", "read",
                          read_scopes=("src",), write_scopes=())
        verifier = NodeSpec("verify", "reader", "verify", "fixture", "fixture", "verify",
                            depends_on=("read",), verifier=True)
        self.store.create_task(other, [worker, verifier], "reader-create")
        self.store.queue_task("reader")
        self.assertIsNotNone(self.store.claim_ready_node("reader-worker", self.epoch))
        before = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(RuntimeError, "conflicts with active node"):
            self._invoke(contract)
        self.assertEqual(self.store.get_task(contract.task_id), before)

    def test_changed_source_branch_is_rejected(self) -> None:
        contract, source = self._make_indeterminate()
        self._git(source, "switch", "-c", "different-branch")
        before = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(ValueError, "allocated branch"):
            self._invoke(contract)
        self.assertEqual(self.store.get_task(contract.task_id), before)
