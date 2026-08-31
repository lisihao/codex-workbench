from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock


class InstallerTests(unittest.TestCase):
    @staticmethod
    def _macbook_installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_macbook_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fake_runtime(self, directory: Path, name: str, probe_exit: int) -> Path:
        runtime = directory / name
        runtime.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            f"  exit {probe_exit}\n"
            "fi\n"
            "printf 'selected-runtime=%s args=%s\\n' \"$0\" \"$*\"\n"
        )
        runtime.chmod(0o755)
        return runtime

    def _run_selector(self, runtime: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["CODEX_WORKBENCH_PYTHON"] = str(runtime)
        selector = Path(__file__).resolve().parents[1] / "scripts" / "python-runtime"
        return subprocess.run(
            [str(selector), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_python39_runtime_is_rejected_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._fake_runtime(Path(directory), "python3.9", 1)
            result = self._run_selector(runtime)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires Python 3.11 or newer", result.stderr)
        self.assertIn("CODEX_WORKBENCH_PYTHON", result.stderr)

    def test_python311_runtime_is_accepted_with_spaces_in_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex workbench ") as directory:
            runtime = self._fake_runtime(Path(directory), "python 3.11", 0)
            result = self._run_selector(runtime, "-m", "codex_workbench", "marker")

        self.assertEqual(result.returncode, 0)
        self.assertIn("python 3.11", result.stdout)
        self.assertIn("marker", result.stdout)

    def test_python314_runtime_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._fake_runtime(Path(directory), "python3.14", 0)
            result = self._run_selector(runtime, "--version")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--version", result.stdout)

    def test_macos_installer_generated_wrapper_uses_runtime_selector(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scripts" / "install-macos.py").read_text()
        self.assertIn('app_root / "scripts" / "python-runtime"', source)
        self.assertNotIn("exec /opt/homebrew/bin/python3 -m codex_workbench", source)
        self.assertIn("CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE", source)
        self.assertIn('"authority_machine_id": macos_machine_id()', source)

    def test_authority_launch_agent_persists_quota_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / "com.lisihao.codex-workbench.plist.in").read_text()
        rendered = (
            template.replace("__APP_ROOT__", "/tmp/app")
            .replace("__STATE_ROOT__", "/tmp/state")
            .replace("__CODEX_BINARY__", "/tmp/codex")
            .replace("__CODEX_HOME__", "/tmp/codex-home")
            .replace("__PROCESS_HOME__", "/tmp/process-home")
            .replace("__QUOTA_SNAPSHOT_FILE__", "/tmp/state/claude-quota.json")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertEqual(
            payload["EnvironmentVariables"]["CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE"],
            "/tmp/state/claude-quota.json",
        )

    def test_macbook_tunnel_reconnect_is_bounded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "launchd" / "com.lisihao.codex-workbench-tunnel.plist.in"
        ).read_text()
        rendered = (
            template.replace("__LOG_ROOT__", "/tmp/logs")
            .replace("__CLIENT_ID__", "macbook-fixture")
            .replace("__AUTHORITY_SSH_ALIAS__", "authority-fixture")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 300)
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("ThrottleInterval", payload)
        self.assertIn("127.0.0.1:18766:127.0.0.1:8766", payload["ProgramArguments"])
        self.assertEqual(payload["ProgramArguments"][-2], "authority-fixture")
        self.assertIn("client heartbeat", payload["ProgramArguments"][-1])
        self.assertIn("macbook-fixture", payload["ProgramArguments"][-1])

    def test_macbook_installer_retires_the_second_ssh_heartbeat_agent(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        ).read_text()
        self.assertIn('LEGACY_HEARTBEAT_LABEL = "com.lisihao.codex-workbench-heartbeat"', source)
        self.assertIn("legacy_heartbeat_path.unlink(missing_ok=True)", source)
        self.assertNotIn("for label in (TUNNEL_LABEL, HEARTBEAT_LABEL)", source)

    def test_macbook_installer_supports_configurable_authority_alias(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        ).read_text()
        self.assertIn('"--authority-ssh-alias"', source)
        self.assertIn('default="macmini"', source)
        self.assertIn('__AUTHORITY_SSH_ALIAS__', source)

    def test_macbook_installer_auto_uses_userspace_tailscale_for_cgnat_host(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="100.64.0.42",
        ), mock.patch.object(
            module.shutil,
            "which",
            return_value="/opt/homebrew/bin/tailscale",
        ):
            arguments = module.ssh_transport_arguments("macmini", "auto")

        self.assertEqual(
            arguments,
            (
                "-o",
                "ProxyCommand=/opt/homebrew/bin/tailscale nc %h %p",
            ),
        )

    def test_macbook_installer_keeps_system_ssh_for_non_tailscale_host(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="git.example.test",
        ):
            arguments = module.ssh_transport_arguments("build-server", "auto")

        self.assertEqual(arguments, ())


if __name__ == "__main__":
    unittest.main()
