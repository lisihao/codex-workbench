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
from codex_workbench.model import (
    NodeResult,
    NodeSpec,
    QuotaSnapshot,
    TaskContract,
    codex_model_long_context_overrides,
    codex_model_profile,
    codex_model_reasoning_effort,
    derive_execution_lane,
    now_iso,
    retry_model,
)
from codex_workbench.planner import PLAN_SCHEMA, CodexPlanner


def compatible_provenance() -> dict[str, object]:
    return {
        "source": COMPATIBLE_SOURCE,
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "claude_version": SUPPORTED_USAGE_VERSION,
    }


class ModelTests(unittest.TestCase):
    def test_codex_model_profiles_require_exact_known_ids(self) -> None:
        self.assertEqual(codex_model_profile(" gpt-5.3-codex-spark "), "spark_worker")
        self.assertEqual(codex_model_reasoning_effort("gpt-5.3-codex-spark"), "xhigh")
        self.assertIsNone(codex_model_profile("gpt-5.3-codex-spark-evil"))
        self.assertIsNone(codex_model_reasoning_effort("gpt-5.3-codex-spark-evil"))
        self.assertEqual(
            retry_model("gpt-5.3-codex-spark-evil", 2),
            "gpt-5.3-codex-spark-evil",
        )
        self.assertEqual(
            derive_execution_lane("codex", "gpt-5.3-codex-spark-evil"),
            "general",
        )

    def test_long_context_overrides_require_exact_supported_model_ids(self) -> None:
        expected = (
            "model_context_window=500000",
            "model_auto_compact_token_limit=450000",
        )
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            self.assertEqual(codex_model_long_context_overrides(model), expected)
        self.assertEqual(codex_model_long_context_overrides("gpt-5.3-codex-spark"), ())
        self.assertEqual(codex_model_long_context_overrides("gpt-5.6-luna-evil"), ())

    def test_routing_v3_retry_preserves_the_pinned_worker_capability(self) -> None:
        self.assertEqual(
            retry_model(
                "gpt-5.6-luna",
                3,
                routing_policy_version="model-routing-v3",
            ),
            "gpt-5.6-luna",
        )
        self.assertEqual(
            retry_model(
                "gpt-5.3-codex-spark",
                4,
                routing_policy_version="model-routing-v3",
            ),
            "gpt-5.3-codex-spark",
        )

    def test_capability_snapshot_roundtrips_on_contract_and_node(self) -> None:
        digest = "sha256:" + "a" * 64
        contract = TaskContract(
            task_id="capability-contract",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            capability_snapshot_id="catalog-20260902-001",
            capability_digest=digest,
        )
        contract.validate()
        restored_contract = TaskContract.from_dict(contract.to_dict())
        self.assertEqual(restored_contract, contract)
        self.assertEqual(restored_contract.capability_digest, digest)

        node = NodeSpec(
            node_id="worker",
            task_id=contract.task_id,
            title="worker",
            executor="codex",
            model="gpt-5.6-luna",
            prompt="bounded implementation",
            capability_snapshot_id=contract.capability_snapshot_id,
            capability_digest=contract.capability_digest,
            model_capability_id="codex.gpt-5.6-luna",
            agent_capability_id="codex.exec",
            agent_name="codex",
            agent_version="0.149.1",
            routing_policy_version="model-routing-v3",
        )
        node.validate()
        restored_node = NodeSpec.from_dict(node.to_dict())
        self.assertEqual(restored_node, node)

    def test_capability_snapshot_changes_contract_digest(self) -> None:
        values = {
            "task_id": "capability-digest",
            "repository": "/tmp/example",
            "base_sha": "abc123",
            "objective": "bounded work",
            "allowed_scope": ("src",),
        }
        without_snapshot = TaskContract(**values)
        with_snapshot = TaskContract(
            **values,
            capability_snapshot_id="catalog-1",
            capability_digest="b" * 64,
        )
        self.assertNotEqual(without_snapshot.digest, with_snapshot.digest)

    def test_performance_snapshot_roundtrips_and_changes_contract_digest(self) -> None:
        values = {
            "task_id": "performance-digest",
            "repository": "/tmp/example",
            "base_sha": "abc123",
            "objective": "bounded work",
            "allowed_scope": ("src",),
        }
        without_snapshot = TaskContract(**values)
        with_snapshot = TaskContract(
            **values,
            performance_snapshot_id="performance-" + "a" * 16,
            performance_digest="b" * 64,
            performance_policy="quality-first-v1",
            performance_status="cold-start",
        )
        with_snapshot.validate()
        self.assertNotEqual(without_snapshot.digest, with_snapshot.digest)
        self.assertEqual(TaskContract.from_dict(with_snapshot.to_dict()), with_snapshot)

        node = NodeSpec(
            "worker",
            with_snapshot.task_id,
            "worker",
            "codex",
            "gpt-5.6-luna",
            "bounded work",
            performance_snapshot_id=with_snapshot.performance_snapshot_id,
            performance_digest=with_snapshot.performance_digest,
            performance_policy=with_snapshot.performance_policy,
            performance_status=with_snapshot.performance_status,
            performance_quality_source="declared",
        )
        node.validate()
        self.assertEqual(NodeSpec.from_dict(node.to_dict()), node)

    def test_performance_snapshot_requires_a_complete_pair(self) -> None:
        contract = TaskContract(
            task_id="half-performance-contract",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            performance_snapshot_id="performance-" + "a" * 16,
        )
        with self.assertRaisesRegex(ValueError, "performance_snapshot_id"):
            contract.validate()

        node = NodeSpec(
            "half-performance-node",
            "half-performance-contract",
            "worker",
            "fixture",
            "fixture",
            "ok",
            performance_digest="c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "performance_snapshot_id"):
            node.validate()

    def test_capability_snapshot_requires_a_complete_pair(self) -> None:
        contract = TaskContract(
            task_id="half-contract",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            capability_snapshot_id="catalog-1",
        )
        with self.assertRaisesRegex(ValueError, "supplied together"):
            contract.validate()

        node = NodeSpec(
            node_id="half-node",
            task_id="half-contract",
            title="worker",
            executor="fixture",
            model="fixture",
            prompt="ok",
            capability_digest="c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "supplied together"):
            node.validate()

    def test_capability_snapshot_rejects_empty_or_unsafe_digest(self) -> None:
        empty_id = TaskContract(
            task_id="empty-id",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            capability_snapshot_id=" ",
            capability_digest="d" * 64,
        )
        with self.assertRaisesRegex(ValueError, "snapshot_id"):
            empty_id.validate()

        unsafe_digest = TaskContract(
            task_id="unsafe-digest",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
            capability_snapshot_id="catalog-1",
            capability_digest="not-a-digest",
        )
        with self.assertRaisesRegex(ValueError, "capability_digest"):
            unsafe_digest.validate()

        invalid_agent_version = NodeSpec(
            node_id="invalid-agent-version",
            task_id="unsafe-digest",
            title="worker",
            executor="fixture",
            model="fixture",
            agent_version=149,  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(ValueError, "agent_version"):
            invalid_agent_version.validate()

    def test_legacy_contract_and_node_input_remain_valid(self) -> None:
        legacy_contract = {
            "task_id": "legacy-contract",
            "repository": "/tmp/example",
            "base_sha": "abc123",
            "objective": "bounded work",
            "allowed_scope": ["src"],
        }
        restored_contract = TaskContract.from_dict(legacy_contract)
        self.assertIsNone(restored_contract.capability_snapshot_id)
        self.assertIsNone(restored_contract.capability_digest)

        legacy_node = {
            "node_id": "legacy-node",
            "task_id": "legacy-contract",
            "title": "worker",
            "executor": "fixture",
            "model": "fixture",
            "prompt": "ok",
        }
        restored_node = NodeSpec.from_dict(legacy_node)
        self.assertIsNone(restored_node.capability_snapshot_id)
        self.assertIsNone(restored_node.capability_digest)

    def test_node_derives_and_validates_execution_lane_and_quota_pool(self) -> None:
        spark = NodeSpec(
            "spark",
            "lane-contract",
            "mechanical worker",
            "codex",
            "gpt-5.3-codex-spark",
            "run the bounded command",
        )
        self.assertEqual(spark.execution_lane, "spark")
        self.assertEqual(spark.quota_pool_id, "codex-spark")

        verifier = NodeSpec(
            "verify",
            "lane-contract",
            "verifier",
            "codex",
            "gpt-5.6-sol",
            "inspect the result",
            verifier=True,
        )
        self.assertEqual(verifier.execution_lane, "control")
        self.assertEqual(verifier.quota_pool_id, "codex-control")

        with self.assertRaisesRegex(ValueError, "execution_lane"):
            NodeSpec(
                "forged-lane",
                "lane-contract",
                "worker",
                "codex",
                "gpt-5.6-luna",
                "bounded work",
                execution_lane="spark",
                quota_pool_id="codex-spark",
            ).validate()

        with self.assertRaisesRegex(ValueError, "quota_pool_id"):
            NodeSpec(
                "forged-pool",
                "lane-contract",
                "worker",
                "codex",
                "gpt-5.6-luna",
                "bounded work",
                execution_lane="general",
                quota_pool_id="codex-spark",
            ).validate()

    def test_node_result_capability_provenance_roundtrips(self) -> None:
        result = NodeResult(
            status="succeeded",
            summary="bounded work complete",
            actual_model="gpt-5.6-luna",
            requested_model="gpt-5.6-luna",
            provider="codex",
            agent_name="codex.exec",
            agent_version="0.149.1",
            capability_snapshot_id="catalog-20260902-001",
            model_capability_id="codex.gpt-5.6-luna",
            agent_capability_id="codex.exec",
        )
        restored = NodeResult.from_dict(result.to_dict())
        self.assertEqual(restored, result)

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
        configs = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--config"
        ]
        self.assertEqual(
            configs,
            [
                "model_reasoning_effort=max",
                "model_context_window=500000",
                "model_auto_compact_token_limit=450000",
            ],
        )
        self.assertIn("Execution profile: luna_worker", CodexExecutor._prompt(request))
        self.assertIn("Model reasoning effort: max", CodexExecutor._prompt(request))

    def test_spark_command_does_not_emit_long_context_overrides(self) -> None:
        request = ExecutionRequest(
            task_id="task-spark",
            node_id="worker",
            attempt=1,
            contract={"objective": "bounded work", "allowed_scope": ["src"], "forbidden_scope": [], "acceptance_commands": []},
            spec={
                "title": "worker",
                "prompt": "implement",
                "model": "gpt-5.3-codex-spark",
                "model_profile": "spark_worker",
                "model_reasoning_effort": "xhigh",
                "verifier": False,
            },
            worktree=Path("/tmp/worktree"),
        )
        command = CodexExecutor._command("codex", request, Path("schema.json"), Path("result.json"))
        configs = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--config"
        ]
        self.assertEqual(configs, ["model_reasoning_effort=xhigh"])

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
