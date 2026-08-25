from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from codex_workbench.sync import RepositorySynchronizer


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()


class RepositorySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.mini = self.root / "mini"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.source)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.source, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.source, check=True)
        (self.source / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.source, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.source, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.source, check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.mini)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.mini, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.mini, check=True)
        self.sync = RepositorySynchronizer()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_github_primary_fast_forwards_clean_checkout(self) -> None:
        (self.source / "github.txt").write_text("github\n")
        subprocess.run(["git", "add", "github.txt"], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-m", "github"], cwd=self.source, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=self.source, check=True, capture_output=True)
        result = self.sync.sync_github(str(self.mini), "origin", "main")
        self.assertTrue(result["changed"])
        self.assertEqual(result["after"], git(self.source, "rev-parse", "HEAD"))

    def test_tailscale_increment_imports_isolated_ref_without_moving_checkout(self) -> None:
        base = git(self.source, "rev-parse", "HEAD")
        (self.source / "increment.txt").write_text("increment\n")
        subprocess.run(["git", "add", "increment.txt"], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-m", "increment"], cwd=self.source, check=True, capture_output=True)
        head = git(self.source, "rev-parse", "HEAD")
        bundle = self.root / "increment.bundle"
        exported = self.sync.export_increment(str(self.source), base, "HEAD", bundle)
        imported = self.sync.import_increment(str(self.mini), bundle, "macbook/task-1")
        self.assertEqual(exported["head_sha"], head)
        self.assertEqual(imported["commit"], head)
        self.assertEqual(git(self.mini, "rev-parse", "HEAD"), base)

    def test_tailscale_send_streams_bundle_and_verifies_remote_commit(self) -> None:
        base = git(self.source, "rev-parse", "HEAD")
        (self.source / "streamed.txt").write_text("streamed\n")
        subprocess.run(["git", "add", "streamed.txt"], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-m", "streamed"], cwd=self.source, check=True, capture_output=True)
        head = git(self.source, "rev-parse", "HEAD")
        observed = {}

        def runner(command, **kwargs):
            observed["command"] = command
            observed["bundle"] = kwargs["input"]
            response = json.dumps({"ok": True, "commit": head}).encode()
            return subprocess.CompletedProcess(command, 0, response, b"")

        result = self.sync.send_increment(
            str(self.source),
            base,
            "HEAD",
            host="macmini",
            remote_repository="/Users/example/Projects/example repo",
            ref_name="macbook/task-2",
            runner=runner,
        )
        self.assertEqual(result["imported"]["commit"], head)
        self.assertEqual(observed["command"][0], "ssh")
        self.assertEqual(observed["command"][-2], "macmini")
        self.assertTrue(observed["bundle"].startswith(b"# v2 git bundle"))


if __name__ == "__main__":
    unittest.main()
