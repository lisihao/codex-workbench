from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_workbench.cli import build_parser, command_task
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.store import WorkbenchStore


class BlockedSourceOnlyInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = WorkbenchConfig(Path(self.temp.name))
        self.store = Mock(spec=WorkbenchStore)
        self.store.get_task.return_value = {
            "state": "blocked", "nodes": [{"node_id": "B", "state": "blocked",
                "result": {"changed_paths": ["src/value.txt", "node_modules/generated.js"]}}],
        }
        self.store.capture_and_resume_blocked_worktree.return_value = {
            "dry_run": True, "source_delta_sha256": "a" * 64,
            "historical_effects": "unknown", "retrospective_compliance_claimed": False,
            "external_replay_authorized": False,
        }
        self.server = WorkbenchMCPServer(self.config, self.store)
        self.arguments = {
            "action": "resume", "task_id": "fixture", "node_id": "B",
            "expected_revision": 16, "expected_attempt": 1, "reason": "extract current source",
            "source_only": True, "confirm_source_only_extraction": True,
            "confirm_preserve_unknown_ignored": True, "dry_run": True,
        }

    def test_mcp_blocked_preview_uses_source_only_store_path(self) -> None:
        result = self.server._tool_result("workbench_control_task", self.arguments)
        value = json.loads(result["content"][0]["text"])
        self.assertEqual(value["source_delta_sha256"], "a" * 64)
        self.assertEqual(value["historical_effects"], "unknown")
        self.store.capture_and_resume_blocked_worktree.assert_called_once_with(
            "fixture", "B", expected_revision=16, expected_attempt=1,
            reason="extract current source", preserve_untracked=False,
            expected_checkpoint_sha=None, source_only=True,
            confirm_source_only_extraction=True, confirm_preserve_unknown_ignored=True,
            expected_source_delta_sha256=None, dry_run=True,
        )

    def test_mcp_blocked_apply_forwards_digest(self) -> None:
        self.server._tool_result("workbench_control_task", {
            **self.arguments, "dry_run": False, "expected_source_delta_sha256": "b" * 64,
            "preserve_untracked": True,
        })
        kwargs = self.store.capture_and_resume_blocked_worktree.call_args.kwargs
        self.assertFalse(kwargs["dry_run"])
        self.assertTrue(kwargs["preserve_untracked"])
        self.assertEqual(kwargs["expected_source_delta_sha256"], "b" * 64)

    def test_mcp_rejects_malformed_fields_before_capture(self) -> None:
        for fields in ({"source_only": "true"}, {"confirm_source_only_extraction": "true"},
                       {"confirm_preserve_unknown_ignored": 1},
                       {"expected_source_delta_sha256": "invalid"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.server._tool_result("workbench_control_task", {**self.arguments, **fields})
        self.store.capture_and_resume_blocked_worktree.assert_not_called()

    def test_mcp_rejects_source_fields_on_nonblocked_resume(self) -> None:
        self.store.get_task.return_value = {"state": "paused"}
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.server._tool_result("workbench_control_task", self.arguments)
        self.store.queue_task.assert_not_called()

    def test_mcp_rejects_historical_assertions_for_source_only(self) -> None:
        for name in ("confirm_no_side_effects", "confirm_effects_restricted_to_owned_files"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "historical"):
                self.server._tool_result("workbench_control_task", {**self.arguments, name: True})
        self.store.capture_and_resume_blocked_worktree.assert_not_called()

    def _parse(self, *extra: str):
        return build_parser().parse_args([
            "task", "resume-blocked-worktree", "fixture", "B",
            "--expected-revision", "16", "--expected-attempt", "1",
            "--reason", "extract current source", *extra,
        ])

    def test_cli_source_only_does_not_require_legacy_confirmation(self) -> None:
        args = self._parse("--source-only", "--confirm-source-only-extraction",
                           "--confirm-preserve-unknown-ignored", "--dry-run")
        with patch("codex_workbench.cli._config", return_value=self.config), patch(
            "codex_workbench.cli._store", return_value=self.store,
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(command_task(args), 0)
        kwargs = self.store.capture_and_resume_blocked_worktree.call_args.kwargs
        self.assertTrue(kwargs["source_only"])
        self.assertTrue(kwargs["dry_run"])
        self.assertTrue(kwargs["confirm_source_only_extraction"])

    def test_cli_strict_mode_still_requires_confirmation(self) -> None:
        args = self._parse("--dry-run")
        with patch("codex_workbench.cli._config", return_value=self.config), patch(
            "codex_workbench.cli._store", return_value=self.store,
        ), self.assertRaisesRegex(ValueError, "confirm-recovery"):
            command_task(args)

    def test_cli_source_only_rejects_recovery_file(self) -> None:
        args = self._parse("--source-only", "--confirm-source-only-extraction",
                           "--confirm-preserve-unknown-ignored", "--recovery-file", "old.json")
        with patch("codex_workbench.cli._config", return_value=self.config), patch(
            "codex_workbench.cli._store", return_value=self.store,
        ), self.assertRaisesRegex(ValueError, "recovery-file"):
            command_task(args)
