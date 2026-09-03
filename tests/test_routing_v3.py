from __future__ import annotations

import unittest

from codex_workbench.routing_v3 import ROUTING_V3_POLICY_VERSION, route_capability_snapshot


def capability(
    provider: str,
    model: str,
    *,
    capability_id: str | None = None,
    roles: tuple[str, ...] = ("worker",),
    task_types: tuple[str, ...] = ("implementation",),
    complexities: tuple[str, ...] = ("standard",),
    features: tuple[str, ...] = ("structured_output",),
    efforts: tuple[str, ...] = ("max",),
    quality: float = 80,
    cost: float = 10,
    latency: float = 100,
    throughput: float = 10,
    capacity: float = 2,
    active: float = 0,
    status: str = "active",
    routable: bool = True,
    runtime_available: bool = True,
    deprecated: bool = False,
) -> dict[str, object]:
    return {
        "capability_id": capability_id or f"{provider}:{model}",
        "provider": provider,
        "model": model,
        "status": status,
        "routable": routable,
        "runtime_available": runtime_available,
        "deprecated": deprecated,
        "roles": roles,
        "task_types": task_types,
        "complexities": complexities,
        "features": features,
        "reasoning_efforts": efforts,
        "quality_score": quality,
        "estimated_cost_units": cost,
        "estimated_latency_ms": latency,
        "estimated_throughput": throughput,
        "concurrency_capacity": capacity,
        "active_count": active,
    }


def healthy_quota(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "auth_ok": True,
        "remaining_percent": 80,
        "five_hour_remaining": 80,
        "weekly_all_remaining": 80,
        "weekly_sonnet_remaining": 80,
        "weekly_fable_remaining": 80,
    }
    values.update(changes)
    return values


def snapshot(*records: dict[str, object], quota: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "snapshot_id": "catalog-2026-09-02-a",
        "digest": "catalog-digest-a",
        "provider_runtime": {
            "codex": {"available": True},
            "claude": {"available": True},
        },
        "claude_quota": quota or healthy_quota(),
        "capabilities": list(records),
    }


def request(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "role": "worker",
        "task_type": "implementation",
        "complexity": "standard",
        "quality_floor": 70,
        "acceptance_risk": "standard",
        "bounded": True,
        "independent_slice": False,
        "low_risk": False,
        "short_task": False,
        "mechanically_verifiable": False,
        "reasoning_effort": "max",
    }
    values.update(changes)
    return values


class RoutingV3Tests(unittest.TestCase):
    def test_known_model_role_profiles_cover_sol_spark_luna_terra_and_claude_families(self) -> None:
        cases = (
            (
                "Sol planner",
                capability(
                    "codex",
                    "gpt-5.6-sol",
                    roles=("planner", "verifier", "control"),
                    task_types=("*",),
                    complexities=("*",),
                    quality=100,
                ),
                request(role="planner", task_type="architecture", complexity="high"),
                "gpt-5.6-sol",
            ),
            (
                "Spark bounded mechanical work",
                capability(
                    "codex",
                    "gpt-5.3-codex-spark",
                    task_types=("implementation",),
                    complexities=("low",),
                ),
                request(
                    complexity="low",
                    low_risk=True,
                    short_task=True,
                    mechanically_verifiable=True,
                ),
                "gpt-5.3-codex-spark",
            ),
            (
                "Luna standard bounded execution",
                capability("codex", "gpt-5.6-luna", task_types=("implementation",), complexities=("standard",)),
                request(),
                "gpt-5.6-luna",
            ),
            (
                "Terra independent large slice",
                capability("codex", "gpt-5.6-terra", task_types=("implementation",), complexities=("high",)),
                request(complexity="high", independent_slice=True),
                "gpt-5.6-terra",
            ),
            (
                "Sonnet daily development",
                capability("claude", "sonnet", task_types=("implementation",), complexities=("standard",)),
                request(),
                "sonnet",
            ),
            (
                "Opus architecture review",
                capability("claude", "opus", task_types=("architecture",), complexities=("high",), quality=92),
                request(task_type="architecture", complexity="high", bounded=False),
                "opus",
            ),
            (
                "Fable challenge research",
                capability("claude", "fable", roles=("challenge",), task_types=("research",), complexities=("high",), quality=98),
                request(role="challenge", task_type="research", complexity="high", bounded=False),
                "fable",
            ),
        )

        for label, record, task, expected_model in cases:
            with self.subTest(label=label):
                decision = route_capability_snapshot(snapshot(record), task)
                self.assertTrue(decision.accepted)
                self.assertEqual(decision.model, expected_model)
                self.assertEqual(decision.policy_version, ROUTING_V3_POLICY_VERSION)
                self.assertEqual(decision.catalog_snapshot_id, "catalog-2026-09-02-a")
                self.assertEqual(decision.catalog_digest, "catalog-digest-a")

    def test_parallel_provider_candidates_are_opt_in_and_keep_one_per_provider(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=88, cost=3)
        sonnet = capability("claude", "sonnet", quality=90, cost=4)

        decision = route_capability_snapshot(
            snapshot(luna, sonnet),
            request(allow_parallel_providers=True, parallel_provider_limit=2),
        )

        self.assertEqual(decision.model, "sonnet")
        self.assertEqual([candidate.provider for candidate in decision.parallel_candidates], ["claude", "codex"])
        self.assertTrue(all(candidate.quality_score >= 70 for candidate in decision.parallel_candidates))

    def test_quality_is_ranked_before_cost(self) -> None:
        high_quality_luna = capability("codex", "gpt-5.6-luna", quality=95, cost=100)
        cheap_sonnet = capability("claude", "sonnet", quality=80, cost=1)

        decision = route_capability_snapshot(snapshot(high_quality_luna, cheap_sonnet), request())

        self.assertEqual(decision.model, "gpt-5.6-luna")
        self.assertEqual(decision.ranked_candidates[1].model, "sonnet")

    def test_cost_is_a_deterministic_tie_break_after_equal_quality(self) -> None:
        low_cost_luna = capability("codex", "gpt-5.6-luna", quality=90, cost=2)
        higher_cost_sonnet = capability("claude", "sonnet", quality=90, cost=3)

        decision = route_capability_snapshot(snapshot(higher_cost_sonnet, low_cost_luna), request())

        self.assertEqual(decision.model, "gpt-5.6-luna")
        self.assertEqual(decision.ranked_candidates[0].estimated_cost_units, 2)

    def test_declared_role_fit_breaks_equal_quality_before_cost(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=90, cost=1)
        sonnet = capability("claude", "sonnet", quality=90, cost=3)

        decision = route_capability_snapshot(
            snapshot(luna, sonnet),
            request(preferred_families=("sonnet", "luna")),
        )

        self.assertEqual(decision.model, "sonnet")
        self.assertEqual(decision.selected.preference_rank, 0)  # type: ignore[union-attr]

    def test_unknown_and_deprecated_capabilities_are_rejected(self) -> None:
        unknown = capability("codex", "gpt-9-future", quality=99)
        deprecated = capability("claude", "sonnet", status="deprecated", quality=99)
        luna = capability("codex", "gpt-5.6-luna", quality=80)

        decision = route_capability_snapshot(snapshot(unknown, deprecated, luna), request())

        self.assertEqual(decision.model, "gpt-5.6-luna")
        reasons = {candidate.model: " ".join(candidate.reasons) for candidate in decision.rejected_candidates}
        self.assertIn("unknown model family", reasons["gpt-9-future"])
        self.assertIn("status 'deprecated'", reasons["sonnet"])

    def test_claude_reserve_and_stop_line_fail_closed(self) -> None:
        sonnet = capability("claude", "sonnet")

        reserve = route_capability_snapshot(snapshot(sonnet, quota=healthy_quota(remaining_percent=20)), request())
        stop_line = route_capability_snapshot(snapshot(sonnet, quota=healthy_quota(remaining_percent=25)), request())

        self.assertFalse(reserve.accepted)
        self.assertIn("20% hard reserve", reserve.rejected_candidates[0].reasons[0])
        self.assertFalse(stop_line.accepted)
        self.assertIn("25% stop line", stop_line.rejected_candidates[0].reasons[0])

    def test_sol_is_rejected_for_ordinary_worker_execution(self) -> None:
        sol = capability(
            "codex",
            "gpt-5.6-sol",
            roles=("worker",),
            task_types=("implementation",),
            complexities=("standard",),
        )

        decision = route_capability_snapshot(snapshot(sol), request())

        self.assertFalse(decision.accepted)
        self.assertIn("Sol is reserved", " ".join(decision.rejected_candidates[0].reasons))

    def test_quality_floor_required_features_effort_and_capacity_are_hard_gates(self) -> None:
        inadequate = capability(
            "codex",
            "gpt-5.6-luna",
            quality=79,
            features=("structured_output",),
            efforts=("high",),
            capacity=1,
            active=1,
        )

        decision = route_capability_snapshot(
            snapshot(inadequate),
            request(
                quality_floor=80,
                required_features=("research",),
                reasoning_effort="max",
            ),
        )

        self.assertFalse(decision.accepted)
        rejection = " ".join(decision.rejected_candidates[0].reasons)
        self.assertIn("below required quality floor", rejection)
        self.assertIn("lacks required features", rejection)
        self.assertIn("reasoning effort", rejection)
        self.assertIn("concurrency capacity reached", rejection)

    def test_result_is_deterministic_and_serializable(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=90, cost=2)
        sonnet = capability("claude", "sonnet", quality=90, cost=2)
        catalog = snapshot(sonnet, luna)
        task = request(allow_parallel_providers=True)

        first = route_capability_snapshot(catalog, task)
        second = route_capability_snapshot(catalog, task)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.ranked_candidates[0].model, "sonnet")
        self.assertEqual(first.ranked_candidates[0].capability_digest, first.ranked_candidates[0].capability_digest)

    def test_passive_capability_registry_shape_is_normalized_without_live_model_calls(self) -> None:
        # This is the public shape emitted by capabilities.py: policy classes
        # rather than invented benchmark numbers, nested reasoning/features,
        # and provider runtime under agents.  Service/request supplies the
        # current usable capacity at dispatch time.
        catalog = {
            "catalog_id": "catalog-passive-observation",
            "digest": "catalog-passive-digest",
            "agents": {"codex": {"status": "available"}, "claude": {"status": "available"}},
            "models": [
                {
                    "provider": "codex",
                    "model_id": "gpt-5.6-luna",
                    "status": "available",
                    "routable": True,
                    "roles": ["worker"],
                    "task_types": ["implementation"],
                    "quality": {"floor": "production"},
                    "cost": {"relative": "efficient"},
                    "latency": {"class": "fast"},
                    "concurrency": {"weight": 1, "class": "high"},
                    "reasoning": {"supported_efforts": ["max"]},
                    "features": {"structured_output": True},
                }
            ],
        }

        decision = route_capability_snapshot(
            catalog,
            request(provider_capacity={"codex": {"capacity": 4, "active": 0}}),
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.model, "gpt-5.6-luna")
        self.assertEqual(decision.catalog_snapshot_id, "catalog-passive-observation")
        self.assertEqual(decision.selected.quality_score, 80)  # type: ignore[union-attr]
        self.assertEqual(decision.selected.estimated_cost_units, 2)  # type: ignore[union-attr]
        self.assertEqual(decision.selected.concurrency_utilization, 0.25)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
