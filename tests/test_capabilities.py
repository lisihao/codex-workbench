from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.capabilities import (
    CAPABILITY_CATALOG_PRODUCER,
    CapabilityCatalogError,
    CapabilityRegistry,
    build_catalog,
    is_routable_model,
    routable_models,
    validate_catalog,
)


def completed(command: list[str], stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.calls: list[tuple[str, ...]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _models(*slugs: str, deprecated: tuple[str, ...] = ()) -> str:
        return json.dumps({
            "models": [
                {
                    "slug": slug,
                    "display_name": slug,
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "max",
                    "supported_reasoning_levels": [{"effort": "high"}, {"effort": "max"}],
                    "shell_type": "shell_command",
                    "tool_mode": "tools",
                    "supports_search_tool": True,
                    "experimental_supported_tools": ["shell"],
                    "input_modalities": ["text"],
                    "multi_agent_version": "v1",
                    "context_window": 272000,
                    "max_context_window": 872000,
                    "upgrade": {"model": "replacement"} if slug in deprecated else None,
                }
                for slug in slugs
            ]
        })

    def _registry(
        self,
        *,
        models: tuple[str, ...] = ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"),
        claude_help: str = "--model <model> (sonnet, opus, fable)",
        fail_codex_models: bool = False,
        deprecated: tuple[str, ...] = (),
    ) -> CapabilityRegistry:
        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(tuple(command))
            self.assertEqual(kwargs.get("check"), False)
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            environment = kwargs.get("env")
            self.assertIsInstance(environment, dict)
            assert isinstance(environment, dict)
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            if "-p" in command or "auth" in command or "login" in command:
                self.fail(f"capability refresh must not invoke a model or login: {command}")
            if command == ["codex", "--version"]:
                return completed(command, "codex-cli 0.149.1\n")
            if command == ["codex", "debug", "models"]:
                if fail_codex_models:
                    return completed(command, "not-json", 1)
                return completed(command, self._models(*models, deprecated=deprecated))
            if command in (
                ["codex", "agents", "--help"],
                ["codex", "remote-control", "--help"],
                ["codex", "app-server", "--help"],
            ):
                return completed(command, "help")
            if command == ["claude", "--version"]:
                return completed(command, "2.1.239 (Claude Code)\n")
            if command == ["claude", "--help"]:
                return completed(command, claude_help)
            if command in (["claude", "agents", "--help"], ["claude", "remote-control", "--help"]):
                return completed(command, "help")
            self.fail(f"unexpected passive capability command: {command}")
            raise AssertionError("unreachable")

        return CapabilityRegistry(self.root, runner=runner)

    def test_passive_refresh_persists_versioned_active_catalog_without_model_or_login(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "must-not-leak"
        try:
            registry = self._registry()
            refreshed = registry.refresh()
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertTrue(refreshed["ok"])
        self.assertTrue(refreshed["activated"])
        catalog = refreshed["catalog"]
        self.assertEqual(catalog["producer"], CAPABILITY_CATALOG_PRODUCER)
        self.assertEqual(catalog["agents"]["codex"]["cli_version"], "0.149.1")
        self.assertEqual(catalog["agents"]["claude"]["cli_version"], "2.1.239")
        sol = next(item for item in catalog["models"] if item["model_id"] == "gpt-5.6-sol")
        self.assertTrue(sol["control_plane_eligible"])
        self.assertTrue(is_routable_model(sol, role="planner"))
        self.assertEqual(sol["source"]["kind"], "codex-debug-models")
        self.assertEqual(sol["provenance"]["cli_version"], "0.149.1")
        self.assertIn("quality", sol)
        self.assertIn("cost", sol)
        self.assertIn("latency", sol)
        self.assertIn("concurrency", sol)
        self.assertIn("reasoning", sol)
        self.assertIn("tools", sol)
        self.assertIn("features", sol)
        self.assertTrue((self.root / "capabilities" / "active.json").exists())
        generation = self.root / "capabilities" / "generations" / f"{catalog['catalog_id']}.json"
        self.assertTrue(generation.exists())
        self.assertEqual(os.stat(generation).st_mode & 0o777, 0o600)
        self.assertFalse(any("-p" in command or "auth" in command for command in self.calls))

    def test_unchanged_refresh_reuses_the_active_generation(self) -> None:
        registry = self._registry()
        first = registry.refresh(activate_safe=True)
        second = registry.refresh(activate_safe=True)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["unchanged"])
        self.assertFalse(second["activated"])
        self.assertEqual(second["catalog"]["catalog_id"], first["catalog"]["catalog_id"])
        self.assertEqual(registry.status()["generation_count"], 1)

    def test_unknown_and_deprecated_models_are_observed_but_not_routable(self) -> None:
        registry = self._registry(
            models=("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-9.9-unknown"),
            deprecated=("gpt-5.6-terra",),
        )
        catalog = registry.refresh()["catalog"]
        unknown = next(item for item in catalog["models"] if item["model_id"] == "gpt-9.9-unknown")
        deprecated_runtime = next(item for item in catalog["models"] if item["model_id"] == "gpt-5.6-terra")
        self.assertEqual(unknown["model_family"], "unknown")
        self.assertFalse(unknown["routable"])
        self.assertEqual(unknown["policy_origin"], "observed-only")
        self.assertEqual(deprecated_runtime["status"], "deprecated")
        self.assertFalse(deprecated_runtime["routable"])
        self.assertNotIn(unknown, routable_models(catalog))
        deprecated = json.loads(json.dumps(catalog))
        deprecated["models"][0]["status"] = "deprecated"
        deprecated["models"][0]["routable"] = True
        with self.assertRaisesRegex(CapabilityCatalogError, "not available but is routable"):
            validate_catalog(deprecated)

    def test_new_worker_family_inherits_policy_but_new_sol_never_receives_control_plane(self) -> None:
        registry = self._registry(models=("gpt-5.6-sol", "gpt-5.7-luna", "gpt-5.7-sol"))
        catalog = registry.refresh()["catalog"]
        luna = next(item for item in catalog["models"] if item["model_id"] == "gpt-5.7-luna")
        future_sol = next(item for item in catalog["models"] if item["model_id"] == "gpt-5.7-sol")
        self.assertTrue(luna["routable"])
        self.assertEqual(luna["policy_origin"], "family-inherited")
        self.assertTrue(is_routable_model(luna, role="worker"))
        self.assertFalse(future_sol["routable"])
        self.assertFalse(future_sol["control_plane_eligible"])
        self.assertNotIn("planner", future_sol["roles"])
        self.assertEqual(future_sol["policy_origin"], "family-inherited-control-plane-pending")

    def test_failed_refresh_reuses_immutable_active_catalog_and_records_error(self) -> None:
        good = self._registry()
        first = good.refresh()
        active_id = first["catalog"]["catalog_id"]
        failed = self._registry(fail_codex_models=True).refresh()
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["reused_active"])
        self.assertEqual(failed["active_generation_id"], active_id)
        persisted = good.active()
        assert persisted is not None
        self.assertEqual(persisted["catalog_id"], active_id)
        receipt = json.loads((self.root / "capabilities" / "last-refresh.json").read_text())
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reused_active_generation_id"], active_id)

    def test_safe_activate_rollback_and_functional_diff(self) -> None:
        first_registry = self._registry(models=("gpt-5.6-sol", "gpt-5.6-luna"))
        first = first_registry.refresh()
        first_id = first["catalog"]["catalog_id"]
        second_registry = self._registry(models=("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"))
        second = second_registry.refresh(activate_safe=True)
        second_id = second["catalog"]["catalog_id"]
        self.assertNotEqual(first_id, second_id)
        diff = second_registry.diff(first_id, second_id)
        self.assertIn("codex:gpt-5.6-terra", diff["added"])
        rolled_back = second_registry.rollback()
        self.assertEqual(rolled_back["active_generation_id"], first_id)
        self.assertEqual(second_registry.active()["catalog_id"], first_id)  # type: ignore[index]

    def test_malformed_catalog_and_unsafe_activate_fail_closed_without_pointer_change(self) -> None:
        registry = self._registry()
        first = registry.refresh()
        active_id = first["catalog"]["catalog_id"]
        malformed = json.loads(json.dumps(first["catalog"]))
        malformed["models"] = []
        with self.assertRaisesRegex(CapabilityCatalogError, "at least one model"):
            validate_catalog(malformed)

        unsafe = build_catalog(
            observed_at="2026-09-02T00:00:00+00:00",
            agents={
                "codex": {
                    "status": "available", "cli_version": "0.149.1", "features": {},
                    "source": {}, "provenance": {}, "observed_at": "2026-09-02T00:00:00+00:00",
                },
                "claude": {
                    "status": "unavailable", "cli_version": "unavailable", "features": {},
                    "source": {}, "provenance": {}, "observed_at": "2026-09-02T00:00:00+00:00",
                },
            },
            models=[
                {
                    **next(item for item in first["catalog"]["models"] if item["model_id"] == "gpt-5.6-luna"),
                    "observed_at": "2026-09-02T00:00:00+00:00",
                }
            ],
        )
        registry._write_generation(unsafe)
        with self.assertRaisesRegex(CapabilityCatalogError, "no exact-policy Codex Sol"):
            registry.activate(unsafe["catalog_id"], safe=True)
        self.assertEqual(registry.active()["catalog_id"], active_id)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
