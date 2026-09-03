from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from typing import get_args
import unittest
from unittest.mock import MagicMock, patch

from codex_workbench.config import WorkbenchConfig
from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import (
    NodeSpec,
    QuotaSnapshot,
    RoutingComplexity,
    RoutingTaskType,
    now_iso,
)
from codex_workbench.store import WorkbenchStore
from codex_workbench.submission import submit_natural_language_request


def compatible_provenance() -> dict[str, object]:
    return {
        "source": COMPATIBLE_SOURCE,
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "claude_version": SUPPORTED_USAGE_VERSION,
    }


def unavailable_registry() -> MagicMock:
    registry = MagicMock()
    registry.active.return_value = None
    registry.refresh.return_value = {
        "ok": False,
        "error": "passive capability probe unavailable",
        "catalog": None,
    }
    return registry


def active_catalog() -> dict[str, object]:
    return {
        "catalog_id": "catalog-submission-v3",
        "digest": "c" * 64,
        "probe_errors": [],
        "models": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-sol",
                "status": "available",
                "routable": True,
            }
        ],
    }


class SubmissionTests(unittest.TestCase):
    def test_imported_context_is_persisted_in_contract_and_bound_to_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            context_ref = "sha256:" + "d" * 64 + ":tar.gz"
            store.record_session_context(
                command_id="context-import",
                request_hash="context-hash",
                source_thread_id="thread-submission",
                context_ref=context_ref,
                archive_ref=context_ref,
                manifest={"schema_version": 1},
                repository=str(repository),
                base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
                allowed_scopes=("README.md",),
                context_excerpt="prior context",
            )
            planned = [
                NodeSpec(
                    "verify",
                    "task-context",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            with patch(
                "codex_workbench.submission.CodexPlanner.compile",
                return_value=planned,
            ) as compile_plan, patch(
                "codex_workbench.submission.CapabilityRegistry",
                return_value=unavailable_registry(),
            ):
                submit_natural_language_request(
                    config,
                    store,
                    objective="continue",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-context",
                    queue=False,
                    source_thread_id="thread-submission",
                    context_bundle_ref=context_ref,
                    context_excerpt="prior context",
                )
            contract = store.get_task("task-context")["contract"]
            self.assertEqual(contract["source_thread_id"], "thread-submission")
            self.assertEqual(contract["context_bundle_ref"], context_ref)
            self.assertEqual(
                store.get_session_binding("thread-submission")["active_task_id"],
                "task-context",
            )
            self.assertEqual(compile_plan.call_args.kwargs["context_excerpt"], "prior context")

    def test_yellow_zone_exposes_only_authenticated_sonnet_to_planner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)

            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=35,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    weekly_fable_remaining=60,
                    **compatible_provenance(),
                )
            )
            planned = [
                NodeSpec(
                    "verify",
                    "task-yellow",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            with (
                patch("codex_workbench.submission.ClaudeExecutor.authentication", return_value=(True, "native-subscription")),
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned) as compile_plan,
                patch(
                    "codex_workbench.submission.CapabilityRegistry",
                    return_value=unavailable_registry(),
                ),
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="bounded work",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-yellow",
                    queue=False,
                )

            self.assertEqual(result["claude_models_available"], ("sonnet",))
            self.assertEqual(result["governance"]["profile"], "code-as-harness/v1")
            self.assertEqual(result["governance"]["verification_tier"], "L2")
            self.assertEqual(compile_plan.call_args.kwargs["claude_models_available"], ("sonnet",))

    def test_structured_routing_strategy_is_forwarded_without_claude_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)

            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            planned = [
                NodeSpec(
                    "verify",
                    "task-architecture",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            with (
                patch("codex_workbench.submission.ClaudeExecutor.authentication") as authenticate,
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned) as compile_plan,
                patch(
                    "codex_workbench.submission.CapabilityRegistry",
                    return_value=unavailable_registry(),
                ),
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="review architecture",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-architecture",
                    task_type="architecture",
                    complexity="high",
                    task_points=3,
                    queue=False,
                )

            self.assertEqual(result["routing_strategy"]["task_type"], "architecture")
            self.assertEqual(result["routing_strategy"]["complexity"], "high")
            self.assertEqual(compile_plan.call_args.kwargs["strategy"].task_type, "architecture")
            self.assertEqual(store.get_task("task-architecture")["contract"]["task_points"], 3)
            authenticate.assert_not_called()

    def test_active_capability_catalog_is_pinned_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            planned = [
                NodeSpec(
                    "verify",
                    "task-catalog",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            registry = MagicMock()
            registry.active.return_value = active_catalog()
            with (
                patch("codex_workbench.submission.CapabilityRegistry", return_value=registry),
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned) as compile_plan,
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="bounded work",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-catalog",
                    queue=False,
                )

            contract = store.get_task("task-catalog")["contract"]
            self.assertEqual(contract["capability_snapshot_id"], "catalog-submission-v3")
            self.assertEqual(contract["capability_digest"], "c" * 64)
            self.assertEqual(result["routing_policy"]["version"], "model-routing-v3")
            self.assertEqual(result["capability_registry"]["status"], "active")
            routing_catalog = compile_plan.call_args.kwargs["capability_snapshot"]
            self.assertEqual(routing_catalog["catalog_id"], active_catalog()["catalog_id"])
            self.assertIn("performance_calibration", routing_catalog)

    def test_active_catalog_pins_the_performance_snapshot_and_passes_advisory_calibration_to_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            planned = [
                NodeSpec(
                    "verify",
                    "task-performance",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            registry = MagicMock()
            registry.active.return_value = active_catalog()
            with (
                patch("codex_workbench.submission.CapabilityRegistry", return_value=registry),
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned) as compile_plan,
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="bounded work",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-performance",
                    queue=False,
                )

            contract = store.get_task("task-performance")["contract"]
            self.assertTrue(contract["performance_snapshot_id"].startswith("performance-"))
            self.assertEqual(len(contract["performance_digest"]), 64)
            self.assertEqual(contract["performance_policy"], "benchmark-prior-plus-runtime-ledger-v1")
            self.assertEqual(contract["performance_status"], "ok")
            self.assertEqual(
                result["routing_policy"]["performance_snapshot_id"],
                contract["performance_snapshot_id"],
            )
            self.assertTrue(result["performance"]["advisory_only"])
            self.assertEqual(
                result["performance"]["calibration"]["matrix_context_count"],
                len(get_args(RoutingTaskType)) * len(get_args(RoutingComplexity)),
            )
            self.assertNotIn("contexts", result["performance"]["calibration"])
            routing_catalog = compile_plan.call_args.kwargs["capability_snapshot"]
            self.assertEqual(routing_catalog["catalog_id"], "catalog-submission-v3")
            calibration = routing_catalog["performance_calibration"]
            self.assertEqual(calibration["task_type"], "implementation")
            self.assertEqual(calibration["complexity"], "standard")
            self.assertEqual(calibration["snapshot_id"], contract["performance_snapshot_id"])
            self.assertEqual(calibration["digest"], contract["performance_digest"])
            expected_contexts = {
                (task_type, complexity)
                for task_type in get_args(RoutingTaskType)
                for complexity in get_args(RoutingComplexity)
            }
            contexts = calibration["contexts"]
            self.assertEqual(
                {(context["task_type"], context["complexity"]) for context in contexts},
                expected_contexts,
            )
            self.assertTrue(
                all(
                    context["snapshot_id"] == contract["performance_snapshot_id"]
                    and context["digest"] == contract["performance_digest"]
                    for context in contexts
                )
            )
            self.assertEqual(
                compile_plan.call_args.kwargs["performance_calibration"],
                calibration,
            )

    def test_failed_first_capability_refresh_reports_legacy_v2_without_pinning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            planned = [
                NodeSpec(
                    "verify",
                    "task-legacy-catalog",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            with (
                patch(
                    "codex_workbench.submission.CapabilityRegistry",
                    return_value=unavailable_registry(),
                ),
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned),
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="bounded work",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-legacy-catalog",
                    queue=False,
                )

            contract = store.get_task("task-legacy-catalog")["contract"]
            self.assertIsNone(contract["capability_snapshot_id"])
            self.assertEqual(result["routing_policy"]["version"], "model-routing-v2")
            self.assertEqual(result["capability_registry"]["mode"], "legacy-v2")
            self.assertIn("probe unavailable", result["capability_registry"]["reason"])


if __name__ == "__main__":
    unittest.main()
