from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.executors import (
    CodexExecutor,
    ExecutionRequest,
    codex_subscription_environment,
)
from codex_workbench.governance import CODE_AS_HARNESS_PROFILE
from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.planner import PLAN_SCHEMA, CodexPlanner


def compatible_provenance() -> dict[str, object]:
    return {
        "source": COMPATIBLE_SOURCE,
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "claude_version": SUPPORTED_USAGE_VERSION,
    }


class ModelTests(unittest.TestCase):
    def test_context_reference_and_source_thread_are_a_paired_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = TaskContract(
                task_id="context-contract",
                repository=str(Path(directory).resolve()),
                base_sha="fixture",
                objective="continue imported work",
                allowed_scope=("src",),
                source_thread_id="thread-1",
            )
            with self.assertRaisesRegex(ValueError, "supplied together"):
                contract.validate()

    def test_artifact_refs_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            with self.assertRaises(ValueError):
                store.path_for("sha256:" + "a" * 64 + ":../secret")

    def test_runtime_steering_is_injected_without_changing_scope(self) -> None:
        request = ExecutionRequest(
            task_id="steered",
            node_id="work",
            attempt=1,
            contract={
                "objective": "update parser",
                "allowed_scope": ["src/parser"],
                "forbidden_scope": ["src/private"],
                "acceptance_commands": ["python -m unittest"],
                "timeout_seconds": 30,
            },
            spec={"title": "parser", "prompt": "implement", "verifier": False},
            worktree=None,
            steering=("保留公开接口",),
        )
        prompt = CodexExecutor._prompt(request)
        self.assertIn("Runtime steering: [\"保留公开接口\"]", prompt)
        self.assertIn('Allowed scope: ["src/parser"]', prompt)
        self.assertIn("Governance profile: code-as-harness/v1", prompt)
        self.assertIn("Verification tier: L2", prompt)

    def test_codex_qualification_requires_companion_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "codex"
            binary.write_text("#!/bin/sh\necho 'Logged in using ChatGPT'\n")
            binary.chmod(0o755)
            executor = CodexExecutor(ArtifactStore(root / "artifacts"), str(binary))

            qualified, reason = executor.qualification()

            self.assertFalse(qualified)
            self.assertIn("codex-code-mode-host", reason)

    def test_codex_worker_enables_code_mode_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            executor = CodexExecutor(ArtifactStore(root / "artifacts"), "/tmp/codex")
            request = ExecutionRequest(
                task_id="task-1",
                node_id="worker",
                attempt=1,
                contract={
                    "objective": "write a bounded file",
                    "allowed_scope": ["result.txt"],
                    "forbidden_scope": [],
                    "acceptance_commands": [],
                    "timeout_seconds": 30,
                },
                spec={
                    "title": "write result",
                    "prompt": "write result.txt",
                    "model": "gpt-5.6-luna",
                    "verifier": False,
                },
                worktree=worktree,
            )

            def fake_run(command: list[str], **_: object):
                enabled = [
                    command[index + 1]
                    for index, argument in enumerate(command[:-1])
                    if argument == "--enable"
                ]
                disabled = [
                    command[index + 1]
                    for index, argument in enumerate(command[:-1])
                    if argument == "--disable"
                ]
                self.assertIn("code_mode_host", enabled)
                self.assertNotIn("code_mode_host", disabled)
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    '{"status":"succeeded","summary":"ok","changed_paths":[],"checks":[]}'
                )
                return subprocess.CompletedProcess(command, 0, "", ""), {}

            with (
                patch.object(executor, "qualification", return_value=(True, "native-subscription")),
                patch.object(executor, "_run", side_effect=fake_run),
            ):
                result = executor.execute(request)
            self.assertEqual(result.governance_profile, CODE_AS_HARNESS_PROFILE)
            self.assertEqual(result.verification_tier, "L2")

    def test_planner_schema_requires_every_declared_property(self) -> None:
        item = PLAN_SCHEMA["properties"]["nodes"]["items"]
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertEqual(
            set(item["required"])
            & {"routing_strategy", "task_type", "complexity", "parallelizable", "claude_allowed"},
            {"routing_strategy", "task_type", "complexity", "parallelizable", "claude_allowed"},
        )

    def test_luna_command_emits_explicit_max_effort_and_profile_metadata(self) -> None:
        request = ExecutionRequest(
            task_id="task-1",
            node_id="worker",
            attempt=1,
            contract={"objective": "bounded work", "allowed_scope": ["src"], "forbidden_scope": [], "acceptance_commands": []},
            spec={
                "title": "worker",
                "prompt": "implement",
                "model": "gpt-5.6-luna",
                "model_profile": "luna_worker",
                "model_reasoning_effort": "max",
                "verifier": False,
            },
            worktree=Path("/tmp/worktree"),
        )
        command = CodexExecutor._command("codex", request, Path("schema.json"), Path("result.json"))
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(
            command[command.index("--config") + 1],
            "model_reasoning_effort=max",
        )
        self.assertIn("Execution profile: luna_worker", CodexExecutor._prompt(request))
        self.assertIn("Model reasoning effort: max", CodexExecutor._prompt(request))

    def test_planner_receives_only_quota_admitted_claude_models(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        prompt = CodexPlanner._prompt(
            contract,
            claude_models_available=("sonnet",),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )
        self.assertIn('["sonnet"]', prompt)
        self.assertIn("only when its exact model family appears", prompt)
        self.assertIn("model-routing-v2", prompt)
        self.assertIn("Sonnet costs one unit", prompt)
        self.assertIn("Governance profile: code-as-harness/v1", prompt)
        self.assertIn("Research routing policy: research-skill/v2", prompt)
        self.assertIn("Research route: mode=none", prompt)
        self.assertIn("Only when the user explicitly requests deep, extensive, or parallel research", prompt)
        self.assertIn("distinguish source evidence from engineering inference", prompt)

    def test_codex_environment_isolates_home_and_removes_api_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/Users/example",
                "CODEX_WORKBENCH_PROCESS_HOME": "/private/workbench-home",
                "OPENAI_API_KEY": "must-not-forward",
                "ANTHROPIC_API_KEY": "must-not-forward",
            },
            clear=False,
        ):
            environment = codex_subscription_environment()
        self.assertEqual(environment["HOME"], "/private/workbench-home")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_contract_hash_is_deterministic(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        self.assertEqual(contract.digest, TaskContract.from_dict(contract.to_dict()).digest)
        self.assertEqual(contract.governance_profile, CODE_AS_HARNESS_PROFILE)
        self.assertEqual(contract.verification_tier, "L2")

    def test_contract_rejects_unsupported_governance(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            governance_profile="other/v1",
        )
        with self.assertRaisesRegex(ValueError, "governance profile"):
            contract.validate()

    def test_contract_rejects_relative_repository(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="relative",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        with self.assertRaisesRegex(ValueError, "absolute"):
            contract.validate()

    def test_claude_quota_fails_closed(self) -> None:
        unknown = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=None,
            weekly_all_remaining=90,
            weekly_sonnet_remaining=90,
            **compatible_provenance(),
        )
        self.assertEqual(unknown.permits("sonnet"), (False, "Claude quota is unknown"))

        protected = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=25,
            weekly_all_remaining=90,
            weekly_sonnet_remaining=90,
            **compatible_provenance(),
        )
        allowed, reason = protected.permits("sonnet")
        self.assertFalse(allowed)
        self.assertIn("protection active", reason)

        healthy = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=70,
            weekly_sonnet_remaining=80,
            **compatible_provenance(),
        )
        self.assertTrue(healthy.permits("sonnet")[0])

    def test_manual_quota_snapshot_cannot_admit_formal_claude_dispatch(self) -> None:
        manual = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=80,
            weekly_all_remaining=80,
            weekly_sonnet_remaining=80,
            source="settings-usage",
        )

        decision = manual.dispatch_decision("sonnet")
        self.assertEqual((decision.action, decision.zone), ("codex", "unknown"))
        self.assertIn("provenance", decision.reason)

    def test_claude_quota_zones_enforce_model_and_concurrency_policy(self) -> None:
        def snapshot(remaining: float) -> QuotaSnapshot:
            return QuotaSnapshot(
                observed_at="2026-08-26T00:00:00+00:00",
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=remaining,
                weekly_all_remaining=remaining,
                weekly_sonnet_remaining=remaining,
                weekly_fable_remaining=remaining,
                **compatible_provenance(),
            )

        red = snapshot(27)
        self.assertEqual(red.dispatch_decision("sonnet").action, "codex")
        self.assertEqual(red.dispatch_decision("sonnet").zone, "red")

        yellow = snapshot(35)
        self.assertEqual(yellow.dispatch_decision("opus").action, "codex")
        self.assertEqual(yellow.dispatch_decision("sonnet").action, "claude")
        deferred = yellow.dispatch_decision("sonnet", ("sonnet",))
        self.assertEqual(deferred.action, "defer")
        self.assertEqual(deferred.max_concurrency, 1)

        green = snapshot(60)
        self.assertEqual(green.dispatch_decision("sonnet", ("sonnet",)).action, "claude")
        self.assertEqual(
            green.dispatch_decision("sonnet", ("sonnet", "sonnet")).action,
            "defer",
        )
        self.assertEqual(green.dispatch_decision("opus", ("opus",)).action, "defer")
        self.assertEqual(green.dispatch_decision("fable", ("opus",)).action, "defer")

        mixed = QuotaSnapshot(
            observed_at="2026-08-26T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=60,
            weekly_sonnet_remaining=60,
            weekly_fable_remaining=None,
            **compatible_provenance(),
        )
        self.assertEqual(mixed.policy_summary()["zone"], "green")
        self.assertEqual(mixed.policy_summary()["zones"]["fable"], "green")
        self.assertEqual(mixed.dispatch_decision("fable").action, "claude")

        fable_specific_limit = QuotaSnapshot(
            observed_at="2026-08-26T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=60,
            weekly_sonnet_remaining=60,
            weekly_fable_remaining=20,
            **compatible_provenance(),
        )
        self.assertEqual(fable_specific_limit.dispatch_decision("fable").zone, "protected")

    def test_green_quota_uses_one_shared_weighted_capacity_pool(self) -> None:
        green = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=60,
            weekly_sonnet_remaining=60,
            weekly_fable_remaining=60,
            **compatible_provenance(),
        )

        first_sonnet = green.dispatch_decision("sonnet", ("sonnet",))
        self.assertEqual(first_sonnet.action, "claude")
        self.assertEqual(first_sonnet.max_concurrency, 2)
        self.assertEqual(
            (first_sonnet.capacity_units, first_sonnet.active_units, first_sonnet.requested_units),
            (2, 1, 1),
        )

        two_sonnet_then_opus = green.dispatch_decision("opus", ("sonnet", "sonnet"))
        self.assertEqual(two_sonnet_then_opus.action, "defer")
        self.assertEqual(two_sonnet_then_opus.max_concurrency, 1)
        self.assertEqual(
            (two_sonnet_then_opus.capacity_units, two_sonnet_then_opus.active_units,
             two_sonnet_then_opus.requested_units),
            (2, 2, 2),
        )

        sonnet_then_opus = green.dispatch_decision("opus", ("sonnet",))
        opus_then_sonnet = green.dispatch_decision("sonnet", ("opus",))
        self.assertEqual(sonnet_then_opus.action, "defer")
        self.assertEqual(opus_then_sonnet.action, "defer")

    def test_claude_quota_zone_boundaries_are_exact(self) -> None:
        def zone(remaining: float) -> str:
            snapshot = QuotaSnapshot(
                observed_at="2026-08-26T00:00:00+00:00",
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=remaining,
                weekly_all_remaining=remaining,
                weekly_sonnet_remaining=remaining,
                source="settings-usage",
            )
            return snapshot.quota_zone("sonnet")[0]

        self.assertEqual(zone(25), "protected")
        self.assertEqual(zone(25.1), "red")
        self.assertEqual(zone(29.9), "red")
        self.assertEqual(zone(30), "yellow")
        self.assertEqual(zone(40), "yellow")
        self.assertEqual(zone(40.1), "green")

    def test_quota_snapshot_rejects_invalid_percentages(self) -> None:
        snapshot = QuotaSnapshot(
            observed_at="2026-08-26T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=101,
            weekly_all_remaining=50,
            weekly_sonnet_remaining=50,
            source="settings-usage",
        )
        with self.assertRaisesRegex(ValueError, "five_hour_remaining"):
            snapshot.validate()


if __name__ == "__main__":
    unittest.main()
