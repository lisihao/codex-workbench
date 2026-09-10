from __future__ import annotations

import unittest
import json
from unittest.mock import patch

from codex_workbench.cli import build_parser
from codex_workbench.store import StateConflictError
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecoveryError
from tests.test_scope_normalization import ScopeNormalizationFixture


class SourceOnlyInterfaceTests(ScopeNormalizationFixture, unittest.TestCase):
    def _source_only_arguments(self, contract):
        return {
            "task_id": contract.task_id, "node_id": "worker", "action": "resolve_indeterminate_locally",
            "expected_revision": self.store.get_task(contract.task_id)["state_revision"],
            "expected_attempt": 1, "reason": "extract verified source", "source_only": True,
            "confirm_old_executor_ended": True, "confirm_preserve_unknown_ignored": True,
            "confirm_source_only_extraction": True, "dry_run": True,
        }

    def test_real_mcp_apply_rejects_same_path_content_drift(self) -> None:
        contract, source = self._make_indeterminate(scope_pattern="src")
        target = source / "src/task-template.spec.ts"
        target.write_text("preview content")
        arguments = self._source_only_arguments(contract)
        preview = self._control(**arguments)
        target.write_text("changed content at exactly the same path")
        before = self.store.get_task(contract.task_id)
        events = self.store.read_events(task_id=contract.task_id)
        with self.assertRaisesRegex(StateConflictError, "expected_source_delta_sha256"):
            self.mcp._resolve_indeterminate_locally(
                contract.task_id,
                {**arguments, "dry_run": False,
                 "expected_source_delta_sha256": preview["source_delta_sha256"]},
                expected_revision=arguments["expected_revision"],
            )
        self.assertEqual(self.store.get_task(contract.task_id), before)
        self.assertEqual(self.store.read_events(task_id=contract.task_id), events)
        self.assertEqual(target.read_text(), "changed content at exactly the same path")

    def test_real_mcp_apply_rechecks_changed_node_scope(self) -> None:
        contract, source = self._make_indeterminate(scope_pattern="src")
        (source / "src/task-template.spec.ts").write_text("source delta")
        arguments = self._source_only_arguments(contract)
        preview = self._control(**arguments)
        # Simulate durable scope drift in this disposable test database only.
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT spec_json FROM nodes WHERE task_id = ? AND node_id = 'worker'",
                (contract.task_id,),
            ).fetchone()
            spec = json.loads(row["spec_json"])
            spec["write_scopes"] = ["src/other"]
            connection.execute(
                "UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = 'worker'",
                (json.dumps(spec), contract.task_id),
            )
        before = self.store.get_task(contract.task_id)
        events = self.store.read_events(task_id=contract.task_id)
        with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "outside node write scope"):
            self.mcp._resolve_indeterminate_locally(
                contract.task_id,
                {**arguments, "dry_run": False,
                 "expected_source_delta_sha256": preview["source_delta_sha256"]},
                expected_revision=arguments["expected_revision"],
            )
        self.assertEqual(self.store.get_task(contract.task_id), before)
        self.assertEqual(self.store.read_events(task_id=contract.task_id), events)

    def test_real_mcp_source_only_preview_has_no_writes(self) -> None:
        contract, source = self._make_indeterminate(scope_pattern="src")
        target = source / "src/task-template.spec.ts"
        target.write_text("source delta to extract")
        before_task = self.store.get_task(contract.task_id)
        with self.store.connection() as connection:
            before_sql = tuple(connection.iterdump())
        artifacts = self.state_root / "artifacts"
        before_artifacts = tuple(sorted(str(p) for p in artifacts.rglob("*")))
        source_before = target.read_bytes()
        preview = self._control(
            task_id=contract.task_id, node_id="worker", action="resolve_indeterminate_locally",
            expected_revision=before_task["state_revision"], expected_attempt=1,
            reason="preview verified source extraction", source_only=True,
            confirm_old_executor_ended=True, confirm_preserve_unknown_ignored=True,
            confirm_source_only_extraction=True, dry_run=True,
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["historical_effects"], "unknown")
        self.assertFalse(preview["retrospective_compliance_claimed"])
        self.assertFalse(preview["external_replay_authorized"])
        self.assertEqual(len(preview["source_delta_sha256"]), 64)
        self.assertEqual(self.store.get_task(contract.task_id), before_task)
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), before_sql)
        self.assertEqual(tuple(sorted(str(p) for p in artifacts.rglob("*"))), before_artifacts)
        self.assertEqual(target.read_bytes(), source_before)

    def test_mcp_forwards_extraction_authorization_without_historical_assertion(self) -> None:
        contract, _ = self._make_indeterminate(scope_pattern="src")
        revision = self.store.get_task(contract.task_id)["state_revision"]
        with patch("codex_workbench.mcp.observed_indeterminate_recovery_paths",
                   return_value=(("src/task-template.spec.ts",), ())), patch.object(
                       self.store, "queue_indeterminate_local_recovery",
                       return_value={"dry_run": True, "historical_effects": "unknown"},
                   ) as queue:
            preview = self._control(
                task_id=contract.task_id, node_id="worker", action="resolve_indeterminate_locally",
                expected_revision=revision, expected_attempt=1, reason="extract verified source",
                source_only=True, confirm_old_executor_ended=True,
                confirm_preserve_unknown_ignored=True, confirm_source_only_extraction=True,
                expected_source_delta_sha256="a" * 64, dry_run=True,
            )
        self.assertEqual(preview["historical_effects"], "unknown")
        self.assertFalse(queue.call_args.kwargs["confirm_effects_restricted_to_owned_files"])
        self.assertTrue(queue.call_args.kwargs["confirm_source_only_extraction"])
        self.assertEqual(queue.call_args.kwargs["expected_source_delta_sha256"], "a" * 64)

    def test_cli_source_only_does_not_require_historical_effects_assertion(self) -> None:
        arguments = build_parser().parse_args([
            "task", "resolve-indeterminate-locally", "fixture", "A",
            "--expected-revision", "12", "--expected-attempt", "2",
            "--reason", "extract verified source", "--source-only",
            "--confirm-old-executor-ended", "--confirm-preserve-unknown-ignored",
            "--confirm-source-only-extraction", "--expected-source-delta-sha256", "a" * 64,
            "--dry-run",
        ])
        self.assertFalse(arguments.confirm_effects_restricted_to_owned_files)
        self.assertTrue(arguments.confirm_source_only_extraction)
        self.assertEqual(arguments.expected_source_delta_sha256, "a" * 64)

    def test_mcp_rejects_malformed_extraction_fields_before_observation(self) -> None:
        arguments = {
            "task_id": "fixture", "node_id": "A", "action": "resolve_indeterminate_locally",
            "expected_revision": 12, "expected_attempt": 2, "reason": "extract source",
            "source_only": True, "dry_run": True,
        }
        with patch("codex_workbench.mcp.observed_indeterminate_recovery_paths") as observe:
            for bad in ({"confirm_source_only_extraction": "true"},
                        {"expected_source_delta_sha256": "invalid"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    self.mcp._resolve_indeterminate_locally(
                        "fixture", {**arguments, **bad}, expected_revision=12,
                    )
            observe.assert_not_called()
