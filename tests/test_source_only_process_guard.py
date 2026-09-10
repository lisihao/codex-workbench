from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.process_probe_fixture import isolated_process_catalog

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.dirty_worktree_recovery import (
    DirtyWorktreeRecoveryError,
    observed_indeterminate_recovery_paths,
)


class SourceOnlyProcessGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process_ids = []
        self.enterContext(isolated_process_catalog(self.process_ids))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "local process probe")
    def test_observer_rejects_live_executor_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            subprocess.run(["git", "init", "--quiet", str(source)], check=True, capture_output=True)
            (source / ".gitignore").write_text("lib/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", ".gitignore"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                check=True, capture_output=True,
            )
            base = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            (source / "lib").mkdir()
            ignored = source / "lib" / "unknown.txt"
            ignored.write_bytes(b"preserve unknown bytes")
            candidate = {
                "task": {"task_id": "fixture", "base_sha": base, "allowed_scope": ["src"], "forbidden_scope": []},
                "node": {"node_id": "worker", "worktree": str(source), "depends_on": (), "write_scopes": ["src"]},
            }
            artifacts = ArtifactStore(source / "artifacts")
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                cwd=source, stdout=subprocess.PIPE, text=True,
            )
            self.process_ids.append(child.pid)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(DirtyWorktreeRecoveryError, "active process"):
                    observed_indeterminate_recovery_paths(
                        candidate, dependency_input_ref=None, artifacts=artifacts, source_only=True,
                    )
                self.assertIsNone(child.poll())
                self.assertEqual(ignored.read_bytes(), b"preserve unknown bytes")
                self.assertFalse(artifacts.root.exists())
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()
            self.assertEqual(
                observed_indeterminate_recovery_paths(
                    candidate, dependency_input_ref=None, artifacts=artifacts, source_only=True,
                ),
                ((), ()),
            )
            self.assertEqual(ignored.read_bytes(), b"preserve unknown bytes")
            self.assertFalse(artifacts.root.exists())
