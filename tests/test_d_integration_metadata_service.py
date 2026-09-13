"""Focused D metadata service guards without a production task or runner."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench import controlled_validation_service as service
from codex_workbench.config import WorkbenchConfig
from codex_workbench.d_integration_metadata import AgentNoteMetadata, PairingMetadata
from codex_workbench.dirty_worktree_recovery import SourceDeltaEntry
from codex_workbench.store import StateConflictError


class _Artifacts:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    def put_text(self, payload: str, _name: str) -> str:
        self.payloads.append(payload)
        return f"artifact:{len(self.payloads)}"


class _Store:
    def __init__(self) -> None:
        self.artifacts = _Artifacts()


class DIntegrationMetadataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="wb-d-service-")
        self.addCleanup(self.directory.cleanup)
        self.config = WorkbenchConfig(Path(self.directory.name) / "state", deployment_role="authority")
        self.config.install_manifest.parent.mkdir(parents=True)
        self.config.install_manifest.write_text('{"fixture":true}\n', encoding="utf-8")
        self.store = _Store()
        self.worktree = Path(self.directory.name).resolve() / "worktree"
        self.worktree.mkdir()
        self.snapshot = {
            "candidate": {
                "task": {
                    "state": "blocked",
                    "allowed_scope": ("docs", "packages", ".agents/notes/implemented"),
                    "forbidden_scope": (),
                },
                "node": {
                    "node_id": "D",
                    "state": "blocked",
                    "write_scopes": ("docs", "packages", ".agents/notes/implemented"),
                },
                "source": {"allocation_id": "active", "worktree": str(self.worktree)},
            },
            "spec_json": json.dumps({"read_scopes": ["docs", "packages", ".agents/notes/implemented"]}),
        }
        self.arguments = {
            "task_id": "fixture-d", "node_id": "D", "expected_revision": 37,
            "expected_attempt": 2, "worktree": str(self.worktree),
            "check_id": service.D_NOTE_WRITE, "reason": "write the exact reviewed note", "dry_run": True,
            "note_anchor": ".agents/notes/implemented/architecture/2026-09-13-d-service.md",
            "note_english": "# English\n", "note_chinese": "# 中文\n",
        }

    @staticmethod
    def _delta(*paths: str, digest: str) -> SimpleNamespace:
        return SimpleNamespace(
            sha256=digest,
            entries=tuple(
                SourceDeltaEntry(path, "tracked", False, "100644", sha256(path.encode()).hexdigest())
                for path in paths
            ),
        )

    def test_d_scope_ownership_requires_fixed_d_and_exact_read_write_paths(self) -> None:
        pairing = PairingMetadata(("docs/guide.md",))
        service._assert_d_scope_ownership(self.snapshot, self.arguments, pairing)

        wrong_node = {**self.arguments, "node_id": "worker"}
        with self.assertRaisesRegex(StateConflictError, "fixed blocked D node"):
            service._assert_d_scope_ownership(self.snapshot, wrong_node, pairing)

        no_read = {**self.snapshot, "spec_json": json.dumps({"read_scopes": ["packages"]})}
        with self.assertRaisesRegex(StateConflictError, "owned read scope"):
            service._assert_d_scope_ownership(no_read, self.arguments, pairing)

        no_write = {
            **self.snapshot,
            "candidate": {
                **self.snapshot["candidate"],
                "node": {**self.snapshot["candidate"]["node"], "write_scopes": ("packages",)},
            },
        }
        with self.assertRaisesRegex(StateConflictError, "owned write scope"):
            service._assert_d_scope_ownership(no_write, self.arguments, pairing)

    def test_d_fields_are_check_specific_and_apply_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Agent Note writes require confirm_note_write"):
            service._arguments({
                **self.arguments,
                "dry_run": False,
                "validation_id": "d-note-1",
                "expected_fingerprint": "a" * 64,
            })
        with self.assertRaisesRegex(ValueError, "do not accept pairing fields"):
            service._arguments({**self.arguments, "pair_anchors": ["docs/guide.md"]})
        with self.assertRaisesRegex(ValueError, "do not accept D metadata"):
            service._arguments({
                "task_id": "fixture-d", "node_id": "D", "expected_revision": 37,
                "expected_attempt": 2, "worktree": str(self.worktree),
                "check_id": "dsh-b-ipc-v1", "reason": "fixed IPC", "dry_run": True,
                "note_anchor": self.arguments["note_anchor"],
            })

    def test_preview_is_read_only_and_apply_allows_only_exact_note_outputs(self) -> None:
        before = self._delta("src/value.txt", digest="a" * 64)
        expected_outputs = (
            self.arguments["note_anchor"],
            self.arguments["note_anchor"].removesuffix(".md") + ".zh.md",
        )
        after = self._delta("src/value.txt", *expected_outputs, digest="b" * 64)
        plan_data = {
            "check_id": service.D_NOTE_WRITE,
            "metadata": {"request": {"note_anchor": self.arguments["note_anchor"]}},
        }
        plan = SimpleNamespace(to_dict=lambda: dict(plan_data))
        runner = SimpleNamespace(to_dict=lambda: {"ok": True})
        with (
            patch.object(service, "_snapshot", return_value=self.snapshot) as snapshot,
            patch.object(service, "_delta", side_effect=[before, before, before, before, after]),
            patch.object(service, "resolve_runtime", return_value=object()),
            patch.object(service, "plan_validation", return_value=plan) as planner,
            patch.object(service, "run_validation", return_value=runner) as run,
            patch.object(service, "_require_journal_reservation") as reservation,
        ):
            preview = service.validate_blocked_node(self.config, self.store, self.arguments)
            self.assertTrue(preview["dry_run"])
            self.assertTrue(preview["requires_pairing"])
            self.assertEqual(self.store.artifacts.payloads, [])
            run.assert_not_called()
            apply = service.validate_blocked_node(
                self.config,
                self.store,
                {
                    **self.arguments,
                    "dry_run": False,
                    "validation_id": "d-note-1",
                    "expected_fingerprint": preview["fingerprint"],
                    "confirm_note_write": True,
                },
            )
        self.assertTrue(apply["ok"])
        self.assertTrue(apply["requires_pairing"])
        self.assertTrue(apply["recovery_requires_fresh_preview"])
        self.assertEqual(len(self.store.artifacts.payloads), 1)
        self.assertNotIn(self.arguments["note_english"], self.store.artifacts.payloads[0])
        planner.assert_called()
        run.assert_called_once()
        reservation.assert_called_once()
        self.assertEqual(snapshot.call_count, 5)

    def test_apply_marks_an_unselected_source_write_as_failed(self) -> None:
        before = self._delta("src/value.txt", digest="a" * 64)
        after = self._delta("src/value.txt", "docs/unselected.md", digest="b" * 64)
        plan = SimpleNamespace(to_dict=lambda: {"check_id": service.D_NOTE_WRITE})
        runner = SimpleNamespace(to_dict=lambda: {"ok": True})
        with (
            patch.object(service, "_snapshot", return_value=self.snapshot),
            patch.object(service, "_delta", side_effect=[before, before, before, before, after]),
            patch.object(service, "resolve_runtime", return_value=object()),
            patch.object(service, "plan_validation", return_value=plan),
            patch.object(service, "run_validation", return_value=runner),
            patch.object(service, "_require_journal_reservation"),
        ):
            preview = service.validate_blocked_node(self.config, self.store, self.arguments)
            applied = service.validate_blocked_node(
                self.config,
                self.store,
                {
                    **self.arguments,
                    "dry_run": False,
                    "validation_id": "d-note-drift",
                    "expected_fingerprint": preview["fingerprint"],
                    "confirm_note_write": True,
                },
            )
        self.assertFalse(applied["ok"])
        self.assertIn("outside its previewed outputs", applied["postflight_error"])


if __name__ == "__main__":
    unittest.main()
