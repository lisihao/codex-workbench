from __future__ import annotations

import unittest

from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import (
    LEGACY_ROUTING_STRATEGY_VERSION,
    NodeSpec,
    QuotaSnapshot,
    TaskContract,
    now_iso,
)
from codex_workbench.routing import (
    ROUTING_STRATEGY_VERSION,
    codex_fallback_model,
    route_node,
    route_task,
    strategy_for_node,
)


def make_contract(**changes: object) -> TaskContract:
    values: dict[str, object] = {
        "task_id": "routing-task",
        "repository": "/tmp/example",
        "base_sha": "abc123",
        "objective": "bounded implementation",
        "allowed_scope": ("src", "tests"),
    }
    values.update(changes)
    return TaskContract(**values)  # type: ignore[arg-type]


def compatible_provenance() -> dict[str, object]:
    return {
        "source": COMPATIBLE_SOURCE,
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "claude_version": SUPPORTED_USAGE_VERSION,
    }


def healthy_quota(*, auth_ok: bool = True) -> QuotaSnapshot:
    return QuotaSnapshot(
        observed_at=now_iso(),
        auth_ok=auth_ok,
        auth_method="native-subscription" if auth_ok else "unavailable",
        five_hour_remaining=80,
        weekly_all_remaining=80,
        weekly_sonnet_remaining=80,
        weekly_fable_remaining=80,
        **compatible_provenance(),
    )


def yellow_quota() -> QuotaSnapshot:
    return QuotaSnapshot(
        observed_at=now_iso(),
        auth_ok=True,
        auth_method="native-subscription",
        five_hour_remaining=35,
        weekly_all_remaining=60,
        weekly_sonnet_remaining=60,
        weekly_fable_remaining=60,
        **compatible_provenance(),
    )


def v3_catalog() -> dict[str, object]:
    def record(
        provider: str,
        model_id: str,
        *,
        roles: tuple[str, ...],
        task_types: tuple[str, ...],
        quality: str,
        cost: str,
        latency: str,
        weight: int,
    ) -> dict[str, object]:
        return {
            "provider": provider,
            "model_id": model_id,
            "capability_id": f"{provider}:{model_id}",
            "status": "available",
            "routable": True,
            "roles": list(roles),
            "task_types": list(task_types),
            "quality": {"floor": quality},
            "cost": {"relative": cost},
            "latency": {"class": latency},
            "concurrency": {"weight": weight, "class": "high"},
            "reasoning": {"preferred_effort": "xhigh" if "spark" in model_id else "max"},
            "features": {"structured_output": True},
        }

    return {
        "catalog_id": "catalog-routing-v3",
        "digest": "a" * 64,
        "agents": {
            "codex": {"status": "available", "cli_version": "0.149.1"},
            "claude": {"status": "available", "cli_version": "2.1.239"},
        },
        "models": [
            record(
                "codex", "gpt-5.6-sol",
                roles=("planner", "verifier", "architecture", "research"),
                task_types=("architecture", "review", "exploration"),
                quality="frontier", cost="highest", latency="deliberate", weight=3,
            ),
            record(
                "codex", "gpt-5.3-codex-spark",
                roles=("worker",),
                task_types=("implementation", "debugging", "tests", "docs", "exploration"),
                quality="focused-mechanical", cost="lowest", latency="fastest", weight=1,
            ),
            record(
                "codex", "gpt-5.6-luna",
                roles=("worker",),
                task_types=("implementation", "debugging", "tests", "docs", "exploration"),
                quality="production", cost="efficient", latency="fast", weight=1,
            ),
            record(
                "codex", "gpt-5.6-terra",
                roles=("worker",),
                task_types=("implementation", "debugging", "tests", "docs", "exploration"),
                quality="production", cost="balanced", latency="balanced", weight=2,
            ),
            record(
                "claude", "sonnet",
                roles=("worker", "reviewer"),
                task_types=("implementation", "debugging", "tests", "docs", "exploration", "review"),
                quality="production", cost="balanced", latency="fast", weight=1,
            ),
            record(
                "claude", "opus",
                roles=("architecture_challenge", "reviewer", "research"),
                task_types=("architecture", "review", "exploration"),
                quality="frontier", cost="high", latency="deliberate", weight=2,
            ),
            record(
                "claude", "fable",
                roles=("architecture_challenge", "reviewer", "research", "creative"),
                task_types=("architecture", "review", "creative", "exploration"),
                quality="frontier", cost="high", latency="deliberate", weight=2,
            ),
        ],
    }


def v3_contract(**changes: object) -> TaskContract:
    catalog = v3_catalog()
    values: dict[str, object] = {
        "capability_snapshot_id": catalog["catalog_id"],
        "capability_digest": catalog["digest"],
    }
    values.update(changes)
    return make_contract(**values)


class RoutingTests(unittest.TestCase):
    def test_old_contract_input_gets_versioned_default_strategy(self) -> None:
        contract = make_contract()

        self.assertEqual(contract.strategy.version, ROUTING_STRATEGY_VERSION)
        self.assertEqual(contract.strategy.task_type, "implementation")
        self.assertEqual(contract.strategy.complexity, "standard")
        self.assertTrue(contract.strategy.parallelizable)

    def test_legacy_route_receipt_preserves_pinned_performance_identity(self) -> None:
        contract = make_contract(
            performance_snapshot_id="performance-" + "a" * 16,
            performance_digest="b" * 64,
            performance_status="cold-start",
        )
        decision = route_task(
            contract,
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )
        self.assertEqual(decision.performance_snapshot_id, contract.performance_snapshot_id)
        self.assertEqual(decision.performance_digest, contract.performance_digest)
        self.assertEqual(decision.performance_status, "cold-start")
        self.assertEqual(decision.quality_source, "declared-policy")
        self.assertIsNone(decision.performance_lower_bound_95)

    def test_low_risk_split_implementation_uses_the_independent_codex_spark_pool(self) -> None:
        decision = route_task(
            make_contract(
                complexity="low",
                parallelizable=True,
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            claude_models_available=("opus", "sonnet"),
            quota_snapshot=healthy_quota(),
        )

        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.3-codex-spark"))
        self.assertEqual(decision.strategy_version, ROUTING_STRATEGY_VERSION)

    def test_explicit_v1_contract_preserves_legacy_standard_implementation_route(self) -> None:
        decision = route_task(
            make_contract(routing_strategy=LEGACY_ROUTING_STRATEGY_VERSION),
            claude_models_available=("sonnet",),
            quota_snapshot=healthy_quota(),
        )

        self.assertEqual(decision.strategy_version, LEGACY_ROUTING_STRATEGY_VERSION)
        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-luna"))

    def test_v2_standard_productive_work_prefers_admitted_sonnet(self) -> None:
        for task_type in ("implementation", "debugging", "tests", "docs", "exploration"):
            with self.subTest(task_type=task_type):
                decision = route_task(
                    make_contract(task_type=task_type, complexity="standard"),
                    claude_models_available=("opus", "sonnet"),
                    quota_snapshot=healthy_quota(),
                )
                self.assertEqual((decision.executor, decision.model), ("claude", "sonnet"))

    def test_route_decision_exposes_explicit_codex_profile_and_effort(self) -> None:
        for complexity, expected_model, expected_profile, expected_effort in (
            ("low", "gpt-5.3-codex-spark", "spark_worker", "xhigh"),
            ("standard", "gpt-5.6-luna", "luna_worker", "max"),
            ("high", "gpt-5.6-terra", "terra_worker", "max"),
        ):
            with self.subTest(complexity=complexity):
                decision = route_task(
                    make_contract(
                        complexity=complexity,
                        acceptance_commands=("scripts/python-runtime -m unittest",),
                    ),
                    claude_models_available=(),
                    quota_snapshot=healthy_quota(auth_ok=False),
                )
                self.assertEqual(decision.model, expected_model)
                self.assertEqual(decision.model_profile, expected_profile)
                self.assertEqual(decision.model_reasoning_effort, expected_effort)

    def test_node_metadata_overrides_contract_for_mixed_dag_routing(self) -> None:
        contract = make_contract(complexity="standard")
        cases = (
            ("micro", "low", True, "gpt-5.3-codex-spark"),
            ("feature", "standard", True, "gpt-5.6-luna"),
            ("slice", "high", True, "gpt-5.6-terra"),
        )
        for node_id, complexity, parallelizable, expected_model in cases:
            with self.subTest(node_id=node_id):
                node = NodeSpec(
                    node_id,
                    contract.task_id,
                    node_id,
                    "codex",
                    "gpt-5.6-luna",
                    "bounded work",
                    complexity=complexity,
                    parallelizable=parallelizable,
                    task_type="implementation",
                    command=("true",) if complexity == "low" else (),
                )
                decision = route_node(
                    contract,
                    node,
                    claude_models_available=(),
                    quota_snapshot=healthy_quota(auth_ok=False),
                )
                self.assertEqual(decision.model, expected_model)
                self.assertEqual(
                    strategy_for_node(contract, node).complexity,
                    complexity,
                )

    def test_node_cannot_downgrade_the_task_routing_strategy_version(self) -> None:
        contract = make_contract(routing_strategy=ROUTING_STRATEGY_VERSION)
        node = {
            "routing_strategy": LEGACY_ROUTING_STRATEGY_VERSION,
            "task_type": "implementation",
            "complexity": "standard",
            "parallelizable": True,
            "claude_allowed": True,
        }
        with self.assertRaisesRegex(ValueError, "must match task strategy"):
            strategy_for_node(contract, node)

    def test_node_cannot_reopen_claude_disabled_by_task_contract(self) -> None:
        contract = make_contract(claude_allowed=False)
        node = {
            "routing_strategy": ROUTING_STRATEGY_VERSION,
            "task_type": "implementation",
            "complexity": "standard",
            "parallelizable": True,
            "claude_allowed": True,
        }

        with self.assertRaisesRegex(ValueError, "cannot widen task contract"):
            strategy_for_node(contract, node)

    def test_v2_claude_family_priorities_are_contractual(self) -> None:
        cases = (
            ("architecture", "opus"),
            ("review", "opus"),
            ("creative", "fable"),
        )
        for task_type, expected in cases:
            with self.subTest(task_type=task_type):
                decision = route_task(
                    make_contract(task_type=task_type, complexity="high"),
                    claude_models_available=("opus", "sonnet", "fable"),
                    quota_snapshot=healthy_quota(),
                )
                self.assertEqual((decision.executor, decision.model), ("claude", expected))

    def test_complex_implementation_upgrades_to_codex_terra_without_claude(self) -> None:
        decision = route_task(
            make_contract(complexity="high"),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )

        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-terra"))

    def test_declared_architecture_can_use_admitted_claude_opus(self) -> None:
        decision = route_task(
            make_contract(task_type="architecture", complexity="high"),
            claude_models_available=("opus",),
            quota_snapshot=healthy_quota(),
        )

        self.assertEqual((decision.executor, decision.model), ("claude", "opus"))

    def test_claude_requires_native_auth_and_quota_permission(self) -> None:
        decision = route_task(
            make_contract(task_type="review", complexity="high"),
            claude_models_available=("sonnet",),
            quota_snapshot=healthy_quota(auth_ok=False),
        )

        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-terra"))

    def test_yellow_quota_can_downgrade_architecture_from_opus_to_sonnet(self) -> None:
        decision = route_task(
            make_contract(task_type="architecture", complexity="high"),
            claude_models_available=("opus", "sonnet"),
            quota_snapshot=yellow_quota(),
        )

        self.assertEqual((decision.executor, decision.model), ("claude", "sonnet"))

    def test_sol_is_immutable_planner_and_verifier(self) -> None:
        for role in ("planner", "verifier"):
            decision = route_task(
                make_contract(planner_model="gpt-5.6-luna", verifier_model="gpt-5.6-terra"),
                role=role,
            )
            self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-sol"))

    def test_high_complexity_claude_route_is_deterministic(self) -> None:
        contract = make_contract(task_type="review", complexity="high")
        first = route_task(contract, claude_models_available=("sonnet",), quota_snapshot=healthy_quota())
        second = route_task(contract, claude_models_available=("sonnet",), quota_snapshot=healthy_quota())

        self.assertEqual(first.to_dict(), second.to_dict())

    def test_codex_fallback_escalates_without_downgrading_high_complexity_work(self) -> None:
        low = make_contract(
            task_type="tests",
            complexity="low",
            acceptance_commands=("scripts/python-runtime -m unittest",),
        )
        self.assertEqual(codex_fallback_model(low, attempt=1), "gpt-5.3-codex-spark")
        self.assertEqual(codex_fallback_model(low, attempt=2), "gpt-5.6-luna")
        self.assertEqual(codex_fallback_model(low, attempt=3), "gpt-5.6-terra")
        self.assertEqual(codex_fallback_model(low, attempt=4), "gpt-5.6-sol")

        standard = make_contract(task_type="debugging", complexity="standard")
        self.assertEqual(codex_fallback_model(standard, attempt=1), "gpt-5.6-luna")
        self.assertEqual(codex_fallback_model(standard, attempt=2), "gpt-5.6-terra")
        self.assertEqual(codex_fallback_model(standard, attempt=3), "gpt-5.6-sol")

        high = make_contract(task_type="architecture", complexity="high")
        self.assertEqual(codex_fallback_model(high, attempt=1), "gpt-5.6-terra")
        self.assertEqual(codex_fallback_model(high, attempt=2), "gpt-5.6-sol")

    def test_spark_requires_mechanical_acceptance_and_ignores_dependency_count(self) -> None:
        no_command = route_task(
            make_contract(complexity="low"),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )
        self.assertEqual((no_command.executor, no_command.model), ("codex", "gpt-5.6-luna"))

        many_dependencies = route_task(
            make_contract(complexity="low"),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
            node_context={
                "command": ("scripts/python-runtime -m unittest",),
                "depends_on": ("a", "b", "c"),
                "write_scopes": ("tests/one",),
            },
        )
        self.assertEqual((many_dependencies.executor, many_dependencies.model), ("codex", "gpt-5.3-codex-spark"))

    def test_spark_rejects_multiple_writes_and_control_or_architecture_roles(self) -> None:
        multiple_writes = route_task(
            make_contract(
                complexity="low",
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
            node_context={"write_scopes": ("src/a", "src/b")},
        )
        self.assertEqual((multiple_writes.executor, multiple_writes.model), ("codex", "gpt-5.6-luna"))

        architecture = route_task(
            make_contract(
                task_type="architecture",
                complexity="low",
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )
        self.assertEqual((architecture.executor, architecture.model), ("codex", "gpt-5.6-terra"))

        security = route_task(
            make_contract(
                objective="security patch across the authentication boundary",
                complexity="low",
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )
        self.assertEqual((security.executor, security.model), ("codex", "gpt-5.6-luna"))

        verifier = route_task(
            make_contract(
                complexity="low",
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            role="verifier",
            claude_models_available=(),
            quota_snapshot=healthy_quota(auth_ok=False),
        )
        self.assertEqual((verifier.executor, verifier.model), ("codex", "gpt-5.6-sol"))

    def test_public_worker_route_fails_closed_without_a_quota_snapshot(self) -> None:
        decision = route_task(
            make_contract(task_type="review", complexity="high"),
            claude_models_available=("sonnet",),
            quota_snapshot=None,
        )
        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-terra"))
        self.assertIn("provenance", decision.reason)

    def test_explicit_v1_keeps_separate_sonnet_and_high_capacity_pools(self) -> None:
        decision = route_task(
            make_contract(
                routing_strategy=LEGACY_ROUTING_STRATEGY_VERSION,
                task_type="architecture",
                complexity="high",
            ),
            claude_models_available=("opus", "sonnet"),
            quota_snapshot=healthy_quota(),
            active_models=("opus",),
        )
        self.assertEqual((decision.executor, decision.model), ("claude", "sonnet"))

    def test_v3_pinned_catalog_keeps_sol_for_planner_and_verifier(self) -> None:
        catalog = v3_catalog()
        contract = v3_contract()

        for role in ("planner", "verifier"):
            with self.subTest(role=role):
                decision = route_task(
                    contract,
                    role=role,
                    capability_snapshot=catalog,
                )
                self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-sol"))
                self.assertEqual(decision.capability_snapshot_id, catalog["catalog_id"])
                self.assertEqual(decision.capability_digest, catalog["digest"])
                self.assertEqual(decision.model_capability_id, "codex:gpt-5.6-sol")
                self.assertEqual(decision.agent_capability_id, "codex-cli:0.149.1")
                self.assertEqual(decision.routing_policy_version, "model-routing-v3")

    def test_v3_standard_worker_prefers_sonnet_then_luna_fallback(self) -> None:
        catalog = v3_catalog()
        standard = v3_contract(task_type="implementation", complexity="standard")
        sonnet = route_task(
            standard,
            claude_models_available=("sonnet",),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((sonnet.executor, sonnet.model), ("claude", "sonnet"))

        tests = v3_contract(task_type="tests", complexity="standard")
        luna = route_task(
            tests,
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((luna.executor, luna.model), ("codex", "gpt-5.6-luna"))
        self.assertEqual(luna.model_profile, "luna_worker")
        self.assertEqual(luna.model_reasoning_effort, "max")

    def test_v3_uses_terra_for_high_independent_slice_and_spark_only_for_mechanical_lane(self) -> None:
        catalog = v3_catalog()
        terra = route_task(
            v3_contract(task_type="implementation", complexity="high"),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((terra.executor, terra.model), ("codex", "gpt-5.6-terra"))

        spark = route_task(
            v3_contract(
                task_type="tests",
                complexity="low",
                acceptance_commands=("scripts/python-runtime -m unittest",),
            ),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((spark.executor, spark.model), ("codex", "gpt-5.3-codex-spark"))
        self.assertEqual(spark.model_profile, "spark_worker")

    def test_v3_challenge_prefers_fable_or_opus_and_quota_gate_uses_sol_control(self) -> None:
        catalog = v3_catalog()
        architecture = route_task(
            v3_contract(task_type="architecture", complexity="high"),
            claude_models_available=("fable", "opus"),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((architecture.role, architecture.executor, architecture.model), ("challenge", "claude", "fable"))

        review = route_task(
            v3_contract(task_type="review", complexity="high"),
            claude_models_available=("fable", "opus", "sonnet"),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((review.role, review.executor, review.model), ("challenge", "claude", "opus"))

        protected = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=20,
            weekly_all_remaining=80,
            weekly_sonnet_remaining=80,
            weekly_fable_remaining=80,
            **compatible_provenance(),
        )
        control = route_task(
            v3_contract(task_type="architecture", complexity="high"),
            claude_models_available=("fable", "opus"),
            quota_snapshot=protected,
            capability_snapshot=catalog,
        )
        self.assertEqual((control.role, control.executor, control.model), ("control", "codex", "gpt-5.6-sol"))
        self.assertIn("quota admission", control.reason)

        codex_only = route_task(
            v3_contract(
                task_type="review",
                complexity="high",
                claude_allowed=False,
            ),
            quota_snapshot=healthy_quota(),
            capability_snapshot=catalog,
        )
        self.assertEqual((codex_only.role, codex_only.executor, codex_only.model), ("control", "codex", "gpt-5.6-sol"))
        self.assertIn("disabled by the immutable task contract", codex_only.reason)

    def test_v3_fails_loud_when_no_legal_worker_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "no legal worker"):
            route_task(
                v3_contract(task_type="creative", complexity="high"),
                quota_snapshot=healthy_quota(),
                capability_snapshot=v3_catalog(),
            )


if __name__ == "__main__":
    unittest.main()
