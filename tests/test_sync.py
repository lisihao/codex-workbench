from __future__ import annotations

from pathlib import Path
import json
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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

    def test_github_primary_refreshes_tracking_ref_with_restricted_fetch_config(self) -> None:
        subprocess.run(
            ["git", "config", "--unset-all", "remote.origin.fetch"],
            cwd=self.mini,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "--add",
                "remote.origin.fetch",
                "+refs/tags/v0.1.0:refs/tags/v0.1.0",
            ],
            cwd=self.mini,
            check=True,
        )
        stale_tracking_ref = git(self.mini, "rev-parse", "origin/main")
        (self.source / "restricted-fetch.txt").write_text("github\n")
        subprocess.run(["git", "add", "restricted-fetch.txt"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "commit", "-m", "restricted fetch"],
            cwd=self.source,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "push"], cwd=self.source, check=True, capture_output=True)

        result = self.sync.sync_github(str(self.mini), "origin", "main")

        expected = git(self.source, "rev-parse", "HEAD")
        self.assertNotEqual(stale_tracking_ref, expected)
        self.assertTrue(result["changed"])
        self.assertEqual(result["after"], expected)
        self.assertEqual(git(self.mini, "rev-parse", "origin/main"), expected)

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

        # Isolate this fallback-path test from a real MacBook client profile in
        # the developer's home directory.  Location-aware routing has its own
        # explicit test below.
        with patch("pathlib.Path.home", return_value=self.root / "empty-home"):
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
        self.assertEqual(result["transport_profile"], "ssh-config")
        self.assertEqual(observed["command"][0], "ssh")
        self.assertEqual(observed["command"][-2], "macmini")
        self.assertNotIn("ProxyCommand=", observed["command"])
        self.assertNotIn("HostKeyAlias=codex-workbench-authority", observed["command"])
        self.assertTrue(observed["bundle"].startswith(b"# v2 git bundle"))

    def test_send_uses_location_aware_proxy_and_shell_quotes_paths(self) -> None:
        base = git(self.source, "rev-parse", "HEAD")
        (self.source / "location-aware.txt").write_text("location-aware\n")
        subprocess.run(["git", "add", "location-aware.txt"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "commit", "-m", "location aware"],
            cwd=self.source,
            check=True,
            capture_output=True,
        )
        head = git(self.source, "rev-parse", "HEAD")
        home = self.root / "MacBook Home"
        client_root = home / "Library" / "Application Support" / "Codex Workbench Client"
        proxy = client_root / "bin" / "workbench-location-proxy"
        config = client_root / "transport.json"
        proxy.parent.mkdir(parents=True)
        proxy.write_text("#!/bin/sh\n")
        config.write_text('{"mode":"auto"}\n')
        observed = {}

        def runner(command, **kwargs):
            observed["command"] = command
            response = json.dumps({"ok": True, "commit": head}).encode()
            return subprocess.CompletedProcess(command, 0, response, b"")

        with patch("pathlib.Path.home", return_value=home):
            result = self.sync.send_increment(
                str(self.source),
                base,
                "HEAD",
                host="macmini",
                remote_repository="/Users/example/Projects/example repo",
                ref_name="macbook/task-location-aware",
                runner=runner,
            )

        proxy_option = next(
            value for value in observed["command"] if value.startswith("ProxyCommand=")
        )
        expected_command = f"{shlex.quote(str(proxy))} --config {shlex.quote(str(config))}"
        self.assertEqual(proxy_option, f"ProxyCommand={expected_command}")
        self.assertEqual(
            shlex.split(proxy_option.removeprefix("ProxyCommand=")),
            [str(proxy), "--config", str(config)],
        )
        self.assertIn("HostKeyAlias=codex-workbench-authority", observed["command"])
        self.assertEqual(result["transport_profile"], "location-aware")

    def test_send_requires_both_location_profile_files(self) -> None:
        base = git(self.source, "rev-parse", "HEAD")
        (self.source / "partial-profile.txt").write_text("partial\n")
        subprocess.run(["git", "add", "partial-profile.txt"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "commit", "-m", "partial profile"],
            cwd=self.source,
            check=True,
            capture_output=True,
        )
        head = git(self.source, "rev-parse", "HEAD")
        home = self.root / "MacBook Home"
        client_root = home / "Library" / "Application Support" / "Codex Workbench Client"
        proxy = client_root / "bin" / "workbench-location-proxy"
        proxy.parent.mkdir(parents=True)
        proxy.write_text("#!/bin/sh\n")
        observed = {}

        def runner(command, **kwargs):
            observed["command"] = command
            response = json.dumps({"ok": True, "commit": head}).encode()
            return subprocess.CompletedProcess(command, 0, response, b"")

        with patch("pathlib.Path.home", return_value=home):
            result = self.sync.send_increment(
                str(self.source),
                base,
                "HEAD",
                host="macmini",
                remote_repository="/Users/example/Projects/example repo",
                ref_name="macbook/task-partial-profile",
                runner=runner,
            )

        self.assertEqual(result["transport_profile"], "ssh-config")
        self.assertNotIn("ProxyCommand=", observed["command"])
        self.assertNotIn("HostKeyAlias=codex-workbench-authority", observed["command"])


if __name__ == "__main__":
    unittest.main()
