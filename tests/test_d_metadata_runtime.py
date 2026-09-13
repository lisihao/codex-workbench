"""Execute D metadata profiles in isolated native sandboxes without a model."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.controlled_validation import ValidationRuntime, plan_validation, run_validation
from codex_workbench.d_integration_metadata import AgentNoteMetadata, PairingMetadata
from codex_workbench.d_integration_profile import NOTE_WRITE_ID, PAIRING_WRITE_ID, REQUIRED_PACKAGE_MARKERS
from tests.test_controlled_validation_sandbox import (
    CODEX, NODE, DSH_NODE_MODULES, _copy_pairing_sources, _real_launcher_dependencies_available,
)


@unittest.skipUnless(sys.platform == "darwin" and CODEX and NODE, "requires macOS Codex sandbox and Node")
class DMetadataRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wb-d-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        for path, name in REQUIRED_PACKAGE_MARKERS.items():
            marker = self.worktree / path
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"name": name}) + "\n")
        self.notes = self.worktree / ".agents/notes/implemented/feature"
        self.notes.mkdir(parents=True)
        (self.worktree / ".agents/skills").mkdir()
        (self.worktree / ".agents/skills/SKILL.md").write_text("unchanged skill\n")
        (self.worktree / "AGENTS.md").write_text("unchanged policy\n")
        self.protected = {path: (self.worktree / path).read_bytes() for path in (
            ".agents/skills/SKILL.md", "AGENTS.md", ".git/config",
        )}
        assert NODE is not None
        self.runtime = ValidationRuntime(Path(str(CODEX)).resolve(), NODE.resolve(), NODE.resolve())
        self.artifacts = ArtifactStore(self.root / "artifacts")

    def _execute(self, plan):
        result = run_validation(plan, self.artifacts).to_dict()
        logs = "\n".join(self.artifacts.verify(command["stderr_ref"]).read_text()
                         for command in result["commands"])
        self.assertTrue(result["ok"], (result, logs))
        for path, contents in self.protected.items():
            self.assertEqual((self.worktree / path).read_bytes(), contents)
        return result

    def test_real_note_writer_creates_only_reviewed_leaves_and_returns_json(self) -> None:
        anchor = ".agents/notes/implemented/feature/2026-09-13-fixture.md"
        english = b"# Agent Note: Fixture\n\nStatus: implemented\n\nEnglish fixture.\n"
        chinese = "# Agent Note: Fixture\n\nStatus: implemented\n\n中文 fixture。\n".encode()
        request = AgentNoteMetadata(anchor, english, chinese)
        plan = plan_validation(self.worktree, NOTE_WRITE_ID, self.runtime, metadata=request)
        self.assertIsNone(plan.git_common_dir)
        self.assertFalse(plan.allow_unix_socket)
        self.assertEqual(set(plan.grants), {
            self.worktree / anchor,
            self.worktree / request.chinese_path,
        })
        self.assertFalse((self.worktree / anchor).exists())
        result = self._execute(plan)
        self.assertEqual((self.worktree / anchor).read_bytes(), english)
        self.assertEqual((self.worktree / request.chinese_path).read_bytes(), chinese)
        self.assertFalse((self.notes / "2026-09-13-fixture.i18n.yaml").exists())
        self.assertEqual(len(result["commands"]), 1)
        stdout = self.artifacts.verify(result["commands"][0]["stdout_ref"]).read_text()
        receipt = json.loads(stdout)
        self.assertTrue(receipt["ok"])
        self.assertEqual({item["result"] for item in receipt["results"]}, {"created"})
        self.assertNotIn("English fixture.", json.dumps(plan.to_dict()))

    def test_real_note_writer_updates_only_hash_matched_reviewed_leaves(self) -> None:
        anchor = ".agents/notes/implemented/feature/2026-09-13-update.md"
        english_path = self.worktree / anchor
        chinese_path = self.worktree / (anchor.removesuffix(".md") + ".zh.md")
        old_english = b"# Old English\n"
        old_chinese = "# 旧中文\n".encode()
        english_path.write_bytes(old_english)
        chinese_path.write_bytes(old_chinese)
        request = AgentNoteMetadata(
            anchor,
            b"# New English\n",
            "# 新中文\n".encode(),
            sha256(old_english).hexdigest(),
            sha256(old_chinese).hexdigest(),
        )
        plan = plan_validation(self.worktree, NOTE_WRITE_ID, self.runtime, metadata=request)
        result = self._execute(plan)
        self.assertEqual(english_path.read_bytes(), request.english)
        self.assertEqual(chinese_path.read_bytes(), request.chinese)
        stdout = self.artifacts.verify(result["commands"][0]["stdout_ref"]).read_text()
        self.assertEqual(
            {item["result"] for item in json.loads(stdout)["results"]},
            {"updated"},
        )

    @unittest.skipUnless(_real_launcher_dependencies_available(), "requires installed DSH pairing/tsx sources")
    def test_real_pairing_creates_only_selected_missing_doc_and_note_sidecars(self) -> None:
        assert DSH_NODE_MODULES is not None
        (self.worktree / "node_modules").symlink_to(DSH_NODE_MODULES, target_is_directory=True)
        _copy_pairing_sources(self.worktree)
        anchors = ("docs/fixture.md", ".agents/notes/implemented/feature/2026-09-13-fixture.md")
        for anchor in anchors:
            english = self.worktree / anchor
            english.parent.mkdir(parents=True, exist_ok=True)
            stem = english.name.removesuffix(".md")
            english.write_text(f"# Fixture\n\n[中文]({stem}.zh.md)\n")
            english.with_name(stem + ".zh.md").write_text(f"# Fixture\n\n[English]({stem}.md)\n")
        unselected = self.worktree / "docs/unselected.i18n.yaml"
        unselected.write_text("unchanged sidecar\n")
        plan = plan_validation(self.worktree, PAIRING_WRITE_ID, self.runtime,
                               metadata=PairingMetadata(anchors))
        expected = {self.worktree / (anchor.removesuffix(".md") + ".i18n.yaml") for anchor in anchors}
        self.assertTrue(all(not path.exists() for path in expected))
        self.assertTrue(expected.issubset(set(plan.write_paths)))
        result = self._execute(plan)
        self.assertEqual(len(result["commands"]), 2)
        for path in expected:
            self.assertIn(".md:", path.read_text())
        self.assertEqual(unselected.read_text(), "unchanged sidecar\n")
        self.assertNotIn("--all", plan.commands[0].argv)


if __name__ == "__main__":
    unittest.main()
