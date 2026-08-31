from __future__ import annotations

import unittest

from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import LEGACY_ROUTING_STRATEGY_VERSION, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.routing import (
    ROUTING_STRATEGY_VERSION,
    codex_fallback_model,
    route_task,
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


class RoutingTests(unittest.TestCase):
    def test_old_contract_input_gets_versioned_default_strategy(self) -> None:
        contract = make_contract()

        self.assertEqual(contract.strategy.version, ROUTING_STRATEGY_VERSION)
        self.assertEqual(contract.strategy.task_type, "implementation")
        self.assertEqual(contract.strategy.complexity, "standard")
        self.assertTrue(contract.strategy.parallelizable)

    def test_low_risk_split_implementation_uses_the_independent_codex_spark_pool(self) -> None:
        decision = route_task(
            make_contract(complexity="low", parallelizable=True),
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
        low = make_contract(task_type="tests", complexity="low")
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


if __name__ == "__main__":
    unittest.main()
