"""Focused immutable-plan checks for D's bounded metadata write inputs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench import controlled_validation as validation
from codex_workbench.d_integration_metadata import (
    AgentNoteMetadata,
    DIntegrationMetadataError,
    PairingMetadata,
    metadata_request_from_arguments,
)
from codex_workbench.d_integration_profile import NOTE_WRITE_ID, PAIRING_WRITE_ID


class DIntegrationMetadataPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wb-d-metadata-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.worktree)], check=True)
        self._write_profile_markers()
        self._write_pair("docs/d-guide.md", sidecar=False)
        self._write_pair("packages/d-package/README.md", sidecar=True)
        (self.worktree / ".agents/notes/implemented/architecture").mkdir(parents=True)
        loader = self.worktree / "node_modules/tsx/dist/esm/index.mjs"
        loader.parent.mkdir(parents=True)
        loader.write_text("export {}\n", encoding="utf-8")
        script = self.worktree / "scripts/verify-translation-pairing.ts"
        script.parent.mkdir(parents=True)
        script.write_text("export {}\n", encoding="utf-8")
        self.runtime = validation.ValidationRuntime(
            self._executable("codex"), self._executable("pnpm"), self._executable("node")
        )

    def _executable(self, name: str) -> Path:
        target = self.root / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o700)
        return target

    def _write_profile_markers(self) -> None:
        for relative, package_name in validation.REQUIRED_PACKAGE_MARKERS.items():
            target = self.worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"name": package_name}) + "\n", encoding="utf-8")

    def _write_pair(self, anchor: str, *, sidecar: bool) -> None:
        english = self.worktree / anchor
        english.parent.mkdir(parents=True, exist_ok=True)
        english.write_text("# English\n", encoding="utf-8")
        english.with_name(english.stem + ".zh.md").write_text("# 中文\n", encoding="utf-8")
        if sidecar:
            english.with_name(english.stem + ".i18n.yaml").write_text("paired: true\n", encoding="utf-8")

    def test_pairing_plan_binds_selected_bytes_and_missing_sidecars_without_writing(self) -> None:
        selected = ("docs/d-guide.md", "packages/d-package/README.md")
        missing_sidecar = (self.worktree / "docs/d-guide.i18n.yaml").resolve(strict=False)
        self.assertFalse(missing_sidecar.exists())

        plan = validation.plan_validation(
            self.worktree,
            PAIRING_WRITE_ID,
            self.runtime,
            metadata=PairingMetadata(selected),
        )

        self.assertFalse(missing_sidecar.exists())
        self.assertFalse(plan.allow_unix_socket)
        self.assertEqual(plan.check_id, PAIRING_WRITE_ID)
        self.assertEqual(plan.commands[0].argv[-2:], selected)
        self.assertEqual(plan.commands[1].argv[-2:], selected)
        self.assertEqual(plan.commands[0].argv[-3], "--write")
        assert plan.metadata is not None
        bindings = plan.to_dict()["metadata"]["pairing_bindings"]
        self.assertFalse(bindings[0]["sidecar_exists"])
        self.assertIsNone(bindings[0]["sidecar_sha256"])
        self.assertTrue(bindings[1]["sidecar_exists"])
        self.assertIn(missing_sidecar, plan.grants)
        assert plan.git_common_dir is not None
        self.assertNotIn(plan.git_common_dir, plan.grants)
        self.assertNotIn(plan.git_common_dir / "objects", plan.grants)
        self.assertNotIn(plan.git_common_dir / "refs", plan.grants)
        self.assertNotIn(plan.git_common_dir / "index", plan.grants)
        self.assertNotIn(plan.git_common_dir / "config", plan.grants)
        for binding in plan.metadata.pairing_bindings:
            for blob in (binding.source_blob_sha1, binding.translated_blob_sha1):
                self.assertIn(plan.git_common_dir / "objects" / blob[:2], plan.grants)
                reference = plan.git_common_dir / "refs/dsh/translation-pairing/snapshots" / blob
                self.assertIn(reference, plan.grants)
                self.assertIn(reference.with_name(reference.name + ".lock"), plan.grants)

        replan = validation.plan_validation(
            self.worktree,
            PAIRING_WRITE_ID,
            self.runtime,
            metadata=plan.metadata.request,
        )
        self.assertEqual(replan.to_dict(), plan.to_dict())

    def test_note_plan_binds_private_hashes_exact_two_leaves_and_replans(self) -> None:
        request = AgentNoteMetadata(
            ".agents/notes/implemented/architecture/2026-09-13-d-metadata.md",
            b"# Agent Note\n\nEnglish\n",
            "# Agent Note\n\n中文\n".encode(),
        )
        plan = validation.plan_validation(
            self.worktree, NOTE_WRITE_ID, self.runtime, metadata=request
        )
        assert plan.metadata is not None
        assert plan.metadata.agent_note is not None
        note = plan.metadata.agent_note
        english = (self.worktree / note.anchor).resolve(strict=False)
        chinese = (self.worktree / note.translated_path).resolve(strict=False)
        self.assertEqual(plan.write_paths, (english, chinese))
        self.assertEqual(plan.grants, (english, chinese))
        self.assertIsNone(plan.git_common_dir)
        self.assertFalse(plan.allow_unix_socket)
        self.assertEqual(len(plan.commands), 1)
        self.assertEqual(plan.commands[0].argv[1:], (
            "<private-scratch>/d-agent-note-write.cjs",
            "<private-scratch>/d-agent-note-write.json",
        ))
        record = json.dumps(plan.to_dict(), ensure_ascii=False)
        self.assertNotIn("English", record)
        self.assertNotIn("中文", record)
        self.assertRegex(note.private_input_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            validation.plan_validation(
                self.worktree, NOTE_WRITE_ID, self.runtime, metadata=plan.metadata.request
            ).to_dict(),
            plan.to_dict(),
        )

    def test_note_existing_matching_bytes_are_idempotent_but_different_or_missing_parent_rejects(self) -> None:
        request = AgentNoteMetadata(
            ".agents/notes/implemented/architecture/2026-09-13-idempotent.md",
            b"English\n",
            b"Chinese\n",
        )
        english = self.worktree / request.anchor
        chinese = self.worktree / request.chinese_path
        english.write_bytes(request.english)
        chinese.write_bytes(request.chinese)
        plan = validation.plan_validation(self.worktree, NOTE_WRITE_ID, self.runtime, metadata=request)
        assert plan.metadata is not None and plan.metadata.agent_note is not None
        self.assertTrue(plan.metadata.agent_note.english_exists)
        self.assertTrue(plan.metadata.agent_note.chinese_exists)

        chinese.write_bytes(b"different\n")
        with self.assertRaisesRegex(validation.ControlledValidationError, "different bytes"):
            validation.plan_validation(self.worktree, NOTE_WRITE_ID, self.runtime, metadata=request)

        missing_parent = AgentNoteMetadata(
            ".agents/notes/implemented/testing/2026-09-13-missing-parent.md",
            b"English\n",
            b"Chinese\n",
        )
        with self.assertRaisesRegex(validation.ControlledValidationError, "parent directory is absent"):
            validation.plan_validation(
                self.worktree, NOTE_WRITE_ID, self.runtime, metadata=missing_parent
            )

    def test_source_symlinks_and_profile_marker_drift_cannot_reuse_a_plan(self) -> None:
        request = PairingMetadata(("docs/d-guide.md",))
        plan = validation.plan_validation(self.worktree, PAIRING_WRITE_ID, self.runtime, metadata=request)
        marker = self.worktree / "packages/prompt/task-template/package.json"
        marker.write_text('{"name":"@deepseek-ai/dsh-task-template","changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(validation.ControlledValidationError, "plan changed"):
            validation._assert_immutable_plan(plan)

        translated = self.worktree / "docs/d-guide.zh.md"
        translated.unlink()
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        translated.symlink_to(outside)
        with self.assertRaisesRegex(validation.ControlledValidationError, "symlink"):
            validation.plan_validation(self.worktree, PAIRING_WRITE_ID, self.runtime, metadata=request)

    def test_metadata_grammar_rejects_globs_instruction_files_and_unapproved_note_kinds(self) -> None:
        for anchor in (
            "docs/*.md",
            "docs/AGENTS.md",
            "packages/skills/README.md",
            ".agents/notes/implemented/ARCHIVED/README.md",
            "docs/d-guide.zh.md",
        ):
            with self.subTest(anchor=anchor), self.assertRaises(DIntegrationMetadataError):
                metadata_request_from_arguments(PAIRING_WRITE_ID, {"pair_anchors": [anchor]})
        with self.assertRaises(DIntegrationMetadataError):
            metadata_request_from_arguments(NOTE_WRITE_ID, {
                "note_anchor": ".agents/notes/implemented/bugfix/2026-09-13-invalid.md",
                "note_english": "English\n",
                "note_chinese": "中文\n",
            })


if __name__ == "__main__":
    unittest.main()
