from __future__ import annotations

import unittest

from codex_workbench.model import QuotaSnapshot, TaskContract, now_iso
from codex_workbench.routing import (
    ROUTING_STRATEGY_VERSION,
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


def healthy_quota(*, auth_ok: bool = True) -> QuotaSnapshot:
    return QuotaSnapshot(
        observed_at=now_iso(),
        auth_ok=auth_ok,
        auth_method="native-subscription" if auth_ok else "unavailable",
        five_hour_remaining=80,
        weekly_all_remaining=80,
        weekly_sonnet_remaining=80,
        weekly_fable_remaining=80,
        source="offline-fixture",
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
        source="offline-fixture",
    )


class RoutingTests(unittest.TestCase):
    def test_old_contract_input_gets_versioned_default_strategy(self) -> None:
        contract = make_contract()

        self.assertEqual(contract.strategy.version, ROUTING_STRATEGY_VERSION)
        self.assertEqual(contract.strategy.task_type, "implementation")
        self.assertEqual(contract.strategy.complexity, "standard")
        self.assertTrue(contract.strategy.parallelizable)

    def test_low_risk_split_implementation_is_codex_luna(self) -> None:
        decision = route_task(
            make_contract(complexity="low", parallelizable=True),
            claude_models_available=("opus", "sonnet"),
            quota_snapshot=healthy_quota(),
        )

        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-luna"))
        self.assertEqual(decision.strategy_version, ROUTING_STRATEGY_VERSION)

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


if __name__ == "__main__":
    unittest.main()
