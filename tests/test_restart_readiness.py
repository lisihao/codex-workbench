from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest

from codex_workbench.restart_readiness import assess_restart_readiness


class RestartReadinessTests(unittest.TestCase):
    def test_launchd_restarts_workbench_after_nonzero_coordinator_exit(self) -> None:
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

        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(payload["ThrottleInterval"], 10)

    def test_filevault_and_missing_auto_login_block_unattended_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_agent = Path(directory) / "workbench.plist"
            launch_agent.write_text("plist")

            def runner(command: list[str]) -> tuple[int, str]:
                if command[:2] == ["/usr/bin/fdesetup", "status"]:
                    return 0, "FileVault is On."
                if command[-1] == "autoLoginUser":
                    return 1, "not found"
                if command[:2] == ["/usr/bin/pmset", "-g"]:
                    return 0, " sleep 0\n autorestart 1"
                if command[:2] == ["/opt/homebrew/bin/tailscale", "status"]:
                    return 0, "100.1.2.3 host online"
                raise AssertionError(command)

            report = assess_restart_readiness(
                platform_name="darwin",
                current_user="example",
                launch_agent=launch_agent,
                runner=runner,
            )

        self.assertFalse(report["ready"])
        self.assertTrue(report["filevault_enabled"])
        self.assertIsNone(report["auto_login_user"])
        self.assertIn("FileVault", " ".join(report["blockers"]))
        self.assertIn("automatic login", " ".join(report["blockers"]))

    def test_ready_requires_power_tailscale_and_user_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_agent = Path(directory) / "workbench.plist"
            launch_agent.write_text("plist")

            def runner(command: list[str]) -> tuple[int, str]:
                if command[:2] == ["/usr/bin/fdesetup", "status"]:
                    return 0, "FileVault is Off."
                if command[-1] == "autoLoginUser":
                    return 0, "example"
                if command[:2] == ["/usr/bin/pmset", "-g"]:
                    return 0, " sleep 0\n autorestart 1"
                if command[:2] == ["/opt/homebrew/bin/tailscale", "status"]:
                    return 0, "100.1.2.3 host online"
                raise AssertionError(command)

            report = assess_restart_readiness(
                platform_name="darwin",
                current_user="example",
                launch_agent=launch_agent,
                runner=runner,
            )

        self.assertTrue(report["ready"])
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["power"]["restart_after_power_failure"])
        self.assertTrue(report["power"]["sleep_disabled"])

    def test_userspace_tailscale_socket_is_accepted_when_default_socket_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_agent = Path(directory) / "workbench.plist"
            launch_agent.write_text("plist")
            commands: list[list[str]] = []

            def runner(command: list[str]) -> tuple[int, str]:
                commands.append(command)
                if command[:2] == ["/usr/bin/fdesetup", "status"]:
                    return 0, "FileVault is Off."
                if command[-1] == "autoLoginUser":
                    return 0, "example"
                if command[:2] == ["/usr/bin/pmset", "-g"]:
                    return 0, " sleep 0\n autorestart 1"
                if command == ["/opt/homebrew/bin/tailscale", "status"]:
                    return 1, "dial unix /var/run/tailscaled.socket: no such file"
                if command == [
                    "/opt/homebrew/bin/tailscale",
                    "--socket=/var/run/tailscale/tailscaled.sock",
                    "status",
                ]:
                    return 0, "100.64.0.42 mac-mini online"
                raise AssertionError(command)

            report = assess_restart_readiness(
                platform_name="darwin",
                current_user="example",
                launch_agent=launch_agent,
                runner=runner,
            )

        self.assertTrue(report["ready"])
        self.assertTrue(report["tailscale_ready"])
        self.assertIn(
            [
                "/opt/homebrew/bin/tailscale",
                "--socket=/var/run/tailscale/tailscaled.sock",
                "status",
            ],
            commands,
        )


if __name__ == "__main__":
    unittest.main()
