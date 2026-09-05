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
    command_ai_frontier,
    command_mobile,
    command_performance,
    command_radar,
    command_serve,
)
from codex_workbench.config import WorkbenchConfig


class FeatureCLITests(unittest.TestCase):
    def test_performance_list_exports_missing_astra_without_task_store_or_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(["--home", directory, "performance", "list", "--format", "json"])
            catalog = {"models": [{"provider": "codex", "model_id": "gpt-6-astra", "routable": True}]}
            with (
                mock.patch("codex_workbench.cli._store", side_effect=AssertionError("list must not initialize task store")),
                mock.patch("codex_workbench.cli._performance_catalog", return_value=(catalog, {})),
                mock.patch("codex_workbench.cli._radar") as radar,
                mock.patch("codex_workbench.cli._ai_frontier") as frontier,
                mock.patch("codex_workbench.cli.PerformanceRegistry") as registry,
            ):
                radar.return_value.status.return_value = {}
                frontier.return_value.status.return_value = {}
                registry.return_value.active.return_value = None
                code, report = self._run(command_performance, args)
                registry.return_value.refresh.assert_not_called()
            self.assertEqual(code, 0)
            astra = next(item for item in report["models"] if item["model_id"] == "gpt-6-astra")
            self.assertFalse(astra["performance_available"])

    def test_performance_evaluate_does_not_activate_snapshot_or_claim_real_savings(self) -> None:
        from tests.test_routing import v3_catalog
        with tempfile.TemporaryDirectory() as directory:
            WorkbenchConfig(Path(directory), deployment_role="authority", authority_host=socket.gethostname(), authority_machine_id=authority_machine_id()).initialize()
            requests = Path(directory) / "requests.json"
            requests.write_text(json.dumps([{
                "request_id": "scenario", "sample_type": "scenario", "role": "worker",
                "task_type": "implementation", "complexity": "standard", "quality_floor": 80,
                "bounded": True, "independent_slice": True,
                "claude_quota": {"auth_ok": False},
            }]))
            args = build_parser().parse_args(["--home", directory, "performance", "evaluate", "--requests", str(requests)])
            with (
                mock.patch("codex_workbench.cli._performance_catalog", return_value=(v3_catalog(), {})),
                mock.patch("codex_workbench.cli._radar") as radar,
                mock.patch("codex_workbench.cli._ai_frontier") as frontier,
            ):
                radar.return_value.status.return_value = {}
                frontier.return_value.status.return_value = {}
                code, result = self._run(command_performance, args)
            self.assertEqual(code, 0)
            self.assertFalse(result["delivery_improvement_proven"])
            self.assertIsNone(result["actual_savings"])
            self.assertFalse((Path(directory) / "performance" / "active.json").exists())

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

        heartbeat = parser.parse_args(
            [
                "client",
                "heartbeat",
                "--client-id",
                "macbook-fixture",
                "--kind",
                "macbook",
                "--route",
                "lan",
                "--reason",
                "home_network_lan_probe_ok",
                "--observed-at",
                "2026-09-03T12:00:00Z",
            ]
        )
        self.assertEqual(heartbeat.route, "lan")
        self.assertEqual(heartbeat.reason, "home_network_lan_probe_ok")

        worktree = parser.parse_args(
            ["worktree", "send", "wta-fixture", "--host", "macmini"]
        )
        self.assertEqual(worktree.worktree_action, "send")
        self.assertEqual(worktree.allocation_id, "wta-fixture")

        radar = parser.parse_args(["radar", "show", "codex-radar-v1-0123456789abcdef"])
        self.assertEqual(radar.radar_action, "show")
        self.assertEqual(radar.snapshot_id, "codex-radar-v1-0123456789abcdef")

        frontier = parser.parse_args(
            ["ai-frontier", "show", "ai-frontier-v1-0123456789abcdef"]
        )
        self.assertEqual(frontier.ai_frontier_action, "show")
        self.assertEqual(frontier.snapshot_id, "ai-frontier-v1-0123456789abcdef")

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

    def test_radar_status_and_unauthorized_refresh_make_no_network_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="radar-cli-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            parser = build_parser()
            status = parser.parse_args(["--home", directory, "radar", "status"])
            refresh = parser.parse_args(["--home", directory, "radar", "refresh"])

            with mock.patch("codex_radar_provider.provider.urlopen") as network:
                status_code, status_payload = self._run(command_radar, status)
                refresh_code, refresh_payload = self._run(command_radar, refresh)

            self.assertEqual(status_code, 1)
            self.assertEqual(status_payload["state"], "unauthorized")
            self.assertEqual(refresh_code, 1)
            self.assertEqual(refresh_payload["state"], "unauthorized")
            self.assertFalse(refresh_payload["network_requested"])
            self.assertEqual(refresh_payload["model_calls"], 0)
            self.assertTrue(refresh_payload["performance"]["ok"])
            self.assertEqual(refresh_payload["performance"]["radar_state"], "unauthorized")
            self.assertFalse(refresh_payload["performance"]["imported_radar_prior"])
            self.assertEqual(refresh_payload["performance"]["model_calls"], 0)
            network.assert_not_called()

    def test_radar_personal_use_consent_is_authority_only(self) -> None:
        parser = build_parser()
        consent = parser.parse_args(
            ["--home", "/tmp/workbench-cli", "radar", "consent-personal-use"]
        )
        self.assertEqual(consent.radar_action, "consent-personal-use")

        with tempfile.TemporaryDirectory(prefix="radar-consent-cli-") as directory:
            root = Path(directory)
            WorkbenchConfig(root).initialize()
            code, payload = self._run(
                command_radar,
                parser.parse_args(
                    ["--home", directory, "radar", "consent-personal-use"]
                ),
            )

            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["state"], "forbidden")
            self.assertFalse(payload["network_requested"])

        with tempfile.TemporaryDirectory(prefix="radar-consent-authority-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            fake_registry = mock.Mock()
            fake_registry.consent_personal_use.return_value = {
                "ok": True,
                "status": "consented",
                "network_requested": False,
                "receipt": {"basis": "local_operator_consent"},
            }
            with mock.patch(
                "codex_workbench.radar.RadarRegistry", return_value=fake_registry
            ):
                code, payload = self._run(
                    command_radar,
                    parser.parse_args(
                        ["--home", directory, "radar", "consent-personal-use"]
                    ),
                )

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "consented")
            self.assertEqual(payload["model_calls"], 0)
            fake_registry.consent_personal_use.assert_called_once_with(
                root / "radar" / "authorization.json"
            )

    def test_ai_frontier_status_is_local_and_refresh_is_client_forbidden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-frontier-cli-client-") as directory:
            root = Path(directory)
            WorkbenchConfig(root).initialize()
            fake_frontier = mock.Mock()
            fake_frontier.status.return_value = {
                "ok": False,
                "state": "unauthorized",
                "network_requested": False,
                "routing_prior_eligible": False,
            }
            with mock.patch(
                "codex_workbench.cli.WorkbenchAIFrontier", return_value=fake_frontier
            ):
                status_code, status_payload = self._run(
                    command_ai_frontier,
                    build_parser().parse_args(
                        ["--home", directory, "ai-frontier", "status"]
                    ),
                )
                refresh_code, refresh_payload = self._run(
                    command_ai_frontier,
                    build_parser().parse_args(
                        ["--home", directory, "ai-frontier", "refresh"]
                    ),
                )

            self.assertEqual(status_code, 1)
            self.assertEqual(status_payload["state"], "unauthorized")
            self.assertFalse(status_payload["network_requested"])
            self.assertEqual(refresh_code, 1)
            self.assertEqual(refresh_payload["state"], "forbidden")
            self.assertFalse(refresh_payload["network_requested"])
            self.assertEqual(refresh_payload["model_calls"], 0)
            fake_frontier.status.assert_called_once()
            fake_frontier.refresh.assert_not_called()

    def test_ai_frontier_refresh_scopes_sources_and_rebuilds_performance_after_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-frontier-cli-authority-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            args = build_parser().parse_args(
                ["--home", directory, "ai-frontier", "refresh"]
            )
            catalog = {
                "catalog_id": "catalog-active",
                "models": [
                    {"provider": "codex", "model_id": "gpt-5.6-luna", "routable": True},
                    {"provider": "claude", "model_id": "claude-opus-4-6", "routable": True},
                    {"provider": "claude", "model_id": "opus", "routable": True},
                    {"provider": "codex", "model_id": "gpt-5.6-luna", "routable": True},
                ],
            }
            fake_frontier = mock.Mock()
            fake_frontier.refresh.return_value = {
                "ok": False,
                "state": "unavailable",
                "network_requested": True,
                "generation_created": False,
            }
            fake_frontier.status.return_value = {
                "ok": False,
                "state": "stale",
                "routing_prior_eligible": True,
                "snapshot_id": "frontier-lkg",
                "network_requested": False,
            }
            fake_performance = mock.Mock()
            fake_performance.refresh.return_value = {
                "active_generation_id": "performance-after-frontier",
                "activated": True,
                "unchanged": False,
                "snapshot": {"source_provenance": {"external_priors": {
                    "ai_frontier": {"reference_record_count": 0, "used_for_prior": False},
                }}},
            }
            with (
                mock.patch(
                    "codex_workbench.cli.WorkbenchAIFrontier", return_value=fake_frontier
                ),
                mock.patch(
                    "codex_workbench.cli.CapabilityRegistry"
                ) as registry_class,
                mock.patch(
                    "codex_workbench.cli.PerformanceRegistry", return_value=fake_performance
                ),
                mock.patch(
                    "codex_workbench.cli._performance_catalog",
                    return_value=(catalog, {"status": "active", "catalog_id": "catalog-active"}),
                ),
            ):
                registry_class.return_value.active.return_value = catalog
                code, payload = self._run(command_ai_frontier, args)

            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["source_ids"],
                ["openai/gpt-5.6-luna", "anthropic/claude-opus-4-6"],
            )
            self.assertEqual(payload["model_calls"], 0)
            self.assertTrue(payload["performance"]["ok"])
            self.assertFalse(payload["performance"]["referenced_ai_frontier_prior"])
            fake_frontier.refresh.assert_called_once_with(
                source_ids=["openai/gpt-5.6-luna", "anthropic/claude-opus-4-6"]
            )
            fake_performance.refresh.assert_called_once()
            self.assertEqual(
                fake_performance.refresh.call_args.kwargs["ai_frontier_status"]["snapshot_id"],
                "frontier-lkg",
            )

    def test_ai_frontier_personal_use_consent_is_authority_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-frontier-consent-client-") as directory:
            WorkbenchConfig(Path(directory)).initialize()
            fake_frontier = mock.Mock()
            with mock.patch(
                "codex_workbench.cli.WorkbenchAIFrontier", return_value=fake_frontier
            ):
                code, payload = self._run(
                    command_ai_frontier,
                    build_parser().parse_args(
                        ["--home", directory, "ai-frontier", "consent-personal-use"]
                    ),
                )
            self.assertEqual(code, 1)
            self.assertEqual(payload["state"], "forbidden")
            self.assertFalse(payload["network_requested"])
            fake_frontier.registry.consent_personal_use.assert_not_called()

        with tempfile.TemporaryDirectory(prefix="ai-frontier-consent-authority-") as directory:
            root = Path(directory)
            WorkbenchConfig(
                root,
                deployment_role="authority",
                authority_host=socket.gethostname(),
                authority_machine_id=authority_machine_id(),
            ).initialize()
            fake_frontier = mock.Mock()
            fake_frontier.registry.consent_personal_use.return_value = {
                "ok": True,
                "state": "consented",
                "network_requested": False,
            }
            with mock.patch(
                "codex_workbench.cli.WorkbenchAIFrontier", return_value=fake_frontier
            ):
                code, payload = self._run(
                    command_ai_frontier,
                    build_parser().parse_args(
                        ["--home", directory, "ai-frontier", "consent-personal-use"]
                    ),
                )
            self.assertEqual(code, 0)
            self.assertEqual(payload["state"], "consented")
            self.assertEqual(payload["model_calls"], 0)
            fake_frontier.registry.consent_personal_use.assert_called_once_with(
                root / "ai-frontier" / "authorization.json"
            )

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
