from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest import mock

from codex_workbench.cli import build_parser, command_capabilities, command_mobile


class FeatureCLITests(unittest.TestCase):
    def _run(self, function, args) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = function(args)
        return code, json.loads(output.getvalue())

    def test_capability_and_mobile_commands_parse_management_options(self) -> None:
        parser = build_parser()
        capabilities = parser.parse_args(
            [
                "--home",
                "/tmp/workbench-cli",
                "capabilities",
                "refresh",
                "--bundled",
                "--activate-safe",
            ]
        )
        self.assertEqual(capabilities.capabilities_action, "refresh")
        self.assertTrue(capabilities.bundled)
        self.assertTrue(capabilities.activate_safe)

        diff = parser.parse_args(
            ["capabilities", "diff", "--from", "catalog-before", "--to", "catalog-after"]
        )
        self.assertEqual(diff.from_generation, "catalog-before")
        self.assertEqual(diff.to_generation, "catalog-after")

        mobile = parser.parse_args(
            [
                "mobile",
                "pair",
                "--codex-binary",
                "/opt/codex",
                "--user-codex-home",
                "/tmp/user-codex",
                "--marketplace-source",
                "owner/repo",
                "--workbench-binary",
                "/opt/workbench",
                "--dry-run",
            ]
        )
        self.assertEqual(mobile.mobile_action, "pair")
        self.assertEqual(mobile.codex_binary, "/opt/codex")
        self.assertEqual(mobile.user_codex_home, "/tmp/user-codex")
        self.assertEqual(mobile.marketplace_source, "owner/repo")
        self.assertEqual(mobile.workbench_binary, "/opt/workbench")
        self.assertTrue(mobile.dry_run)

    def test_capability_refresh_uses_actual_binary_env_and_returns_explicit_ok(self) -> None:
        with tempfile.TemporaryDirectory(prefix="feature-cli-") as directory:
            args = build_parser().parse_args(
                ["--home", directory, "capabilities", "refresh", "--bundled", "--activate-safe"]
            )
            fake_registry = mock.Mock()
            fake_registry.refresh.return_value = {"ok": True, "active_generation_id": "catalog-demo"}
            with mock.patch.dict(
                "os.environ",
                {
                    "CODEX_WORKBENCH_CODEX": "/opt/codex-real",
                    "CODEX_WORKBENCH_CLAUDE": "/opt/claude-real",
                },
                clear=False,
            ), mock.patch("codex_workbench.cli.CapabilityRegistry", return_value=fake_registry) as registry:
                code, payload = self._run(command_capabilities, args)

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            registry.assert_called_once()
            self.assertEqual(registry.call_args.kwargs["codex_binary"], "/opt/codex-real")
            self.assertEqual(registry.call_args.kwargs["claude_binary"], "/opt/claude-real")
            fake_registry.refresh.assert_called_once_with(bundled=True, activate_safe=True)

    def test_mobile_pair_is_explicitly_manual_and_never_reports_confirmed_pairing(self) -> None:
        args = build_parser().parse_args(
            ["mobile", "pair", "--codex-binary", "/opt/codex", "--dry-run"]
        )
        fake_remote = mock.Mock()
        fake_remote.pair.return_value = {
            "ok": True,
            "manual_pairing_required": True,
            "pairing_code_available": True,
        }
        with mock.patch("codex_workbench.cli.MobileRemote", return_value=fake_remote) as remote:
            code, payload = self._run(command_mobile, args)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pairing_state"], "not_confirmed")
        self.assertNotIn("paired", payload)
        self.assertIn("remote-control pair --json", payload["pairing_command"])
        self.assertIn("同一终端", payload["next_step"])
        self.assertNotIn("pairing_code", payload)
        remote.assert_called_once_with(
            codex_binary="/opt/codex",
            user_codex_home=None,
            marketplace_source=None,
            workbench_binary=None,
            dry_run=True,
        )
        fake_remote.pair.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
