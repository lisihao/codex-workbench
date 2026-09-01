from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.config import WorkbenchConfig
from codex_workbench.session_context import import_session_context
from codex_workbench.store import WorkbenchStore


class SessionContextTests(unittest.TestCase):
    def test_import_materializes_isolated_worktree_and_durable_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=origin, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=origin, check=True)
            (origin / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=origin, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=origin, check=True, capture_output=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin, text=True).strip()

            manifest = {
                "schema_version": 1,
                "source_thread_id": "thread-fixture",
                "repository": {
                    "name": "imported-project",
                    "origin": str(origin),
                    "head": head,
                },
                "suggested_scopes": ["README.md", "notes.txt"],
                "files": [
                    {
                        "archive_path": "files/notes.txt",
                        "logical_path": "notes.txt",
                        "kind": "repository",
                    }
                ],
            }
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                for name, data in (
                    ("manifest.json", json.dumps(manifest).encode()),
                    ("transcript.jsonl", b'{"role":"user","text":"continue"}\n'),
                    ("files/notes.txt", b"synced\n"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
            archive.seek(0)

            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            with patch.dict(
                os.environ,
                {"CODEX_WORKBENCH_PROJECTS_ROOT": str(root / "projects")},
            ):
                result = import_session_context(
                    config,
                    store,
                    archive,
                    command_id="import-fixture",
                )

            self.assertEqual(result["state"], "active")
            worktree = Path(result["repository"])
            self.assertEqual((worktree / "notes.txt").read_text(), "synced\n")
            self.assertTrue((worktree / ".workbench-context" / "transcript.jsonl").is_file())
            self.assertEqual(
                store.get_session_binding("thread-fixture")["base_sha"],
                result["base_sha"],
            )

    def test_import_rejects_path_traversal(self) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            data = b"bad"
            info = tarfile.TarInfo("../escape")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        archive.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            config = WorkbenchConfig(Path(directory) / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            with self.assertRaisesRegex(ValueError, "unsafe context bundle path"):
                import_session_context(config, store, archive, command_id="bad")


if __name__ == "__main__":
    unittest.main()
