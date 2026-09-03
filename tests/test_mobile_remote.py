from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.mobile import CommandResult, MobileRemote, MobileRemoteError


class MobileRemoteTests(unittest.TestCase):
    def test_enable_uses_user_codex_home_and_builds_native_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            root = Path(directory)
            user_home = root / "user" / ".codex"
            isolated_home = root / "workbench" / "codex-home"
            process_home = root / "workbench" / "codex-process-home"
            source = root / "source"
            source.mkdir()
            calls: list[tuple[list[str], dict[str, str]]] = []

            def runner(command: list[str], environment: dict[str, str]) -> CommandResult:
                calls.append((command, environment))
                if command[1:4] == ["plugin", "list", "--available"]:
                    return CommandResult(0, '{"installed":[],"available":[]}')
                if command[1:3] == ["mcp", "get"]:
                    return CommandResult(1)
                return CommandResult(0)

            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(isolated_home),
                    "CODEX_WORKBENCH_PROCESS_HOME": str(process_home),
                },
            ):
                result = MobileRemote(
                    codex_binary="/usr/local/bin/codex",
                    user_codex_home=user_home,
                    marketplace_source=source,
                    workbench_binary="/Applications/Codex Workbench.app/bin/codex-workbench",
                    runner=runner,
                ).enable()

            self.assertTrue(result["ok"])
            self.assertTrue(result["idempotent"])
            self.assertEqual(len(calls), 5)
            self.assertEqual(
                calls[1][0],
                [
                    "/usr/local/bin/codex",
                    "plugin",
                    "marketplace",
                    "add",
                    str(source),
                    "--json",
                ],
            )
            self.assertEqual(
                calls[2][0],
                [
                    "/usr/local/bin/codex",
                    "plugin",
                    "add",
                    "codex-workbench@codex-workbench",
                    "--json",
                ],
            )
            self.assertEqual(
                calls[4][0],
                [
                    "/usr/local/bin/codex",
                    "mcp",
                    "add",
                    "codex-workbench",
                    "--",
                    "/Applications/Codex Workbench.app/bin/codex-workbench",
                    "mcp",
                ],
            )
            self.assertEqual(
                result["remote_control"]["owner"],
                "desktop_app",
            )
            self.assertEqual(result["pairing_state"], "not_attested")
            self.assertFalse(any("app-server" in command for command, _environment in calls))
            for _command, environment in calls:
                self.assertEqual(environment["CODEX_HOME"], str(user_home.resolve()))
                self.assertNotEqual(environment["CODEX_HOME"], str(isolated_home))

    def test_enable_dry_run_never_calls_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
            calls.append(command)
            return CommandResult(0)

        result = MobileRemote(
            user_codex_home=Path("/tmp/mobile-user-codex"),
            marketplace_source="lisihao/codex-workbench",
            runner=runner,
            dry_run=True,
        ).enable()

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(calls, [])
        self.assertEqual(result["commands"][-1][1:3], ["mcp", "add"])
        self.assertFalse(any("app-server" in command for command in result["commands"]))

    def test_enable_failure_stops_without_claiming_success(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
            calls.append(command)
            if len(calls) == 1:
                return CommandResult(0, '{"installed":[],"available":[]}')
            return CommandResult(1) if len(calls) == 3 else CommandResult(0)

        with self.assertRaisesRegex(MobileRemoteError, "plugin installation failed"):
            MobileRemote(
                user_codex_home=Path("/tmp/mobile-user-codex"),
                marketplace_source="source",
                runner=runner,
            ).enable()
        self.assertEqual(len(calls), 3)

    def test_enable_reuses_matching_plugin_and_mcp_without_overwrite(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
            calls.append(command)
            if command[1:4] == ["plugin", "list", "--available"]:
                return CommandResult(
                    0,
                    '{"installed":[{"pluginId":"codex-workbench@personal",'
                    '"name":"codex-workbench","installed":true,"enabled":true}],'
                    '"available":[]}',
                )
            if command[1:3] == ["mcp", "get"]:
                return CommandResult(
                    0,
                    '{"name":"codex-workbench","enabled":true,"transport":'
                    '{"type":"stdio","command":"/opt/workbench","args":["mcp"]}}',
                )
            return CommandResult(0)

        result = MobileRemote(
            user_codex_home=Path("/tmp/mobile-user-codex"),
            marketplace_source="source",
            workbench_binary="/opt/workbench",
            runner=runner,
        ).enable()

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(any("app-server" in command for command in calls))
        self.assertEqual(
            result["skipped"],
            [
                "Codex Workbench plugin already installed",
                "Codex Workbench MCP already configured",
            ],
        )

    def test_enable_refuses_to_overwrite_a_different_workbench_mcp(self) -> None:
        def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
            if command[1:4] == ["plugin", "list", "--available"]:
                return CommandResult(
                    0,
                    '{"installed":[{"pluginId":"codex-workbench@codex-workbench",'
                    '"name":"codex-workbench","installed":true,"enabled":true}]}',
                )
            if command[1:3] == ["mcp", "get"]:
                return CommandResult(
                    0,
                    '{"enabled":true,"transport":{"type":"stdio",'
                    '"command":"ssh","args":["authority"]}}',
                )
            return CommandResult(0)

        with self.assertRaisesRegex(MobileRemoteError, "refusing to overwrite"):
            MobileRemote(
                user_codex_home=Path("/tmp/mobile-user-codex"),
                marketplace_source="source",
                workbench_binary="/opt/workbench",
                runner=runner,
            ).enable()

    def test_isolated_codex_home_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            root = Path(directory)
            process_home = root / "state" / "codex-process-home"
            isolated_home = root / "state" / "codex-home"
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(isolated_home),
                    "CODEX_WORKBENCH_PROCESS_HOME": str(process_home),
                },
            ):
                with self.assertRaisesRegex(MobileRemoteError, "isolated CODEX_HOME"):
                    MobileRemote(user_codex_home=isolated_home)

    def test_pair_points_to_desktop_app_without_generating_a_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            home = Path(directory) / ".codex"

            calls: list[list[str]] = []

            def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
                calls.append(command)
                return CommandResult(0, '{"pairing_code":"short-lived-secret"}')

            result = MobileRemote(user_codex_home=home, runner=runner).pair()

            self.assertTrue(result["ok"])
            self.assertTrue(result["manual_pairing_required"])
            self.assertFalse(result["pairing_code_available"])
            self.assertFalse(result["persisted"])
            self.assertEqual(result["commands"], [])
            self.assertEqual(result["pairing_surface"], "desktop_app")
            self.assertIn("Control this Mac or PC", result["desktop_setup_path"])
            self.assertEqual(calls, [])
            self.assertNotIn("short-lived-secret", json.dumps(result))
            self.assertFalse(home.exists())

    def test_status_reports_only_reduced_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            home = Path(directory) / ".codex"
            outputs = {
                "mcp": '{"name":"codex-workbench","enabled":true,"transport":'
                '{"type":"stdio","command":"/opt/workbench","args":["mcp"]}}',
                "plugin": '{"installed":[{"pluginId":"codex-workbench@codex-workbench","installed":true}],"available":[]}',
            }

            def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
                if command[1:3] == ["mcp", "get"]:
                    return CommandResult(0, outputs["mcp"])
                return CommandResult(0, outputs["plugin"])

            result = MobileRemote(
                user_codex_home=home,
                workbench_binary="/opt/workbench",
                runner=runner,
            ).status()

            self.assertTrue(result["ok"])
            self.assertTrue(result["integration_ready"])
            self.assertEqual(result["pairing_state"], "not_attested")
            self.assertEqual(result["remote_control"]["owner"], "desktop_app")
            self.assertTrue(result["mcp"]["configured"])
            self.assertTrue(result["plugin"]["installed"])
            self.assertNotIn("socketPath", json.dumps(result))

    def test_status_rejects_a_different_mcp_transport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            home = Path(directory) / ".codex"
            def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
                if command[1:3] == ["mcp", "get"]:
                    return CommandResult(
                        0,
                        '{"enabled":true,"transport":{"type":"stdio",'
                        '"command":"ssh","args":["authority"]}}',
                    )
                return CommandResult(
                    0,
                    '{"installed":[{"pluginId":"codex-workbench@codex-workbench",'
                    '"installed":true}],"available":[]}',
                )

            result = MobileRemote(
                user_codex_home=home,
                workbench_binary="/opt/workbench",
                runner=runner,
            ).status()

            self.assertFalse(result["ok"])
            self.assertFalse(result["mcp"]["configured"])

    def test_disable_defers_to_desktop_app_and_preserves_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mobile-remote-") as directory:
            home = Path(directory) / ".codex"
            home.mkdir(parents=True)
            config = home / "config.toml"
            config.write_text("model = \"keep-me\"\n")
            calls: list[list[str]] = []

            def runner(command: list[str], _environment: dict[str, str]) -> CommandResult:
                calls.append(command)
                return CommandResult(0)

            result = MobileRemote(user_codex_home=home, runner=runner).disable()

            self.assertTrue(result["ok"])
            self.assertEqual(calls, [])
            self.assertTrue(result["manual_action_required"])
            self.assertEqual(result["remote_control"]["owner"], "desktop_app")
            self.assertEqual(config.read_text(), "model = \"keep-me\"\n")
            self.assertTrue(result["preserved"]["mcp"])
            self.assertTrue(result["preserved"]["plugin"])


if __name__ == "__main__":
    unittest.main()
