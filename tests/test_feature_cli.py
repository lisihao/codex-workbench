from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from codex_workbench.authority import authority_machine_id
from codex_workbench.cli import (
    build_parser,
    command_capabilities,
    command_mobile,
    command_performance,
    command_serve,
)
from codex_workbench.config import WorkbenchConfig


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
            "pairing_code_available": False,
            "pairing_state": "not_confirmed",
            "desktop_setup_path": "Settings > Connections > Control this Mac or PC > Set up or Add",
            "next_step": "在桌面 App 中显示二维码后用手机扫描。",
        }
        with mock.patch("codex_workbench.cli.MobileRemote", return_value=fake_remote) as remote:
            code, payload = self._run(command_mobile, args)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pairing_state"], "not_confirmed")
        self.assertNotIn("paired", payload)
        self.assertNotIn("pairing_command", payload)
        self.assertIn("Control this Mac or PC", payload["desktop_setup_path"])
        self.assertIn("桌面 App", payload["next_step"])
        self.assertNotIn("pairing_code", payload)
        remote.assert_called_once_with(
            codex_binary="/opt/codex",
            user_codex_home=None,
            marketplace_source=None,
            workbench_binary=None,
            dry_run=True,
        )
        fake_remote.pair.assert_called_once_with()

    def test_performance_commands_materialize_and_show_the_local_ledger_without_models(self) -> None:
        with tempfile.TemporaryDirectory(prefix="performance-cli-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            parser = build_parser()
            refresh = parser.parse_args(["--home", directory, "performance", "refresh"])
            code, payload = self._run(command_performance, refresh)

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["model_calls"], 0)
            self.assertEqual(payload["catalog"]["status"], "unavailable")
            snapshot_id = payload["active_generation_id"]

            status = parser.parse_args(["--home", directory, "performance", "status"])
            status_code, status_payload = self._run(command_performance, status)
            self.assertEqual(status_code, 0)
            self.assertEqual(status_payload["active_generation_id"], snapshot_id)

            show = parser.parse_args(["--home", directory, "performance", "show", snapshot_id])
            show_code, shown = self._run(command_performance, show)
            self.assertEqual(show_code, 0)
            self.assertEqual(shown["snapshot_id"], snapshot_id)
            self.assertEqual(shown["snapshot"]["pools"]["spark"]["remaining_display"], "N/A")

    def test_capability_refresh_on_authority_refreshes_the_matching_performance_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capability-performance-cli-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            args = build_parser().parse_args(
                ["--home", directory, "capabilities", "refresh", "--activate-safe"]
            )
            catalog = {"catalog_id": "catalog-active", "digest": "c" * 64}
            fake_registry = mock.Mock()
            fake_registry.refresh.return_value = {
                "ok": True,
                "catalog": catalog,
                "active_generation_id": "catalog-active",
            }
            fake_performance = mock.Mock()
            fake_performance.refresh.return_value = {
                "active_generation_id": "performance-active",
                "activated": True,
                "unchanged": False,
            }
            with (
                mock.patch("codex_workbench.cli.CapabilityRegistry", return_value=fake_registry),
                mock.patch("codex_workbench.cli.PerformanceRegistry", return_value=fake_performance),
            ):
                code, payload = self._run(command_capabilities, args)

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["performance"], {
                "ok": True,
                "status": "active",
                "snapshot_id": "performance-active",
                "activated": True,
                "unchanged": False,
                "model_calls": 0,
            })
            fake_performance.refresh.assert_called_once()

    def test_serve_forwards_the_resolved_spark_lane_cap_to_the_coordinator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spark-serve-cli-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                max_workers=3,
                spark_workers=2,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            args = build_parser().parse_args(
                ["--home", directory, "serve", "--spark-workers", "1"]
            )
            coordinator = mock.Mock()
            coordinator.recover.return_value = 0
            server = mock.Mock()
            with (
                mock.patch("codex_workbench.cli.Coordinator", return_value=coordinator) as coordinator_class,
                mock.patch("codex_workbench.cli.WorkbenchHTTPServer", return_value=server),
                mock.patch("codex_workbench.cli.signal.signal"),
            ):
                code, _ = self._run(command_serve, args)

            self.assertEqual(code, 0)
            self.assertEqual(coordinator_class.call_args.kwargs["max_workers"], 3)
            self.assertEqual(coordinator_class.call_args.kwargs["spark_workers"], 1)


if __name__ == "__main__":
    unittest.main()
