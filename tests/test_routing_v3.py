from __future__ import annotations

import json
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
    def _calibration(self, *candidates: dict[str, object], **metadata: object) -> dict[str, object]:
        values: dict[str, object] = {
            "status": "ok",
            "snapshot_id": "performance-" + "a" * 16,
            "digest": "b" * 64,
            "task_type": "implementation",
            "complexity": "standard",
            "candidates": list(candidates),
        }
        values.update(metadata)
        return values

    @staticmethod
    def _calibrated_candidate(
        provider: str,
        model_id: str,
        lower_bound: float,
        *,
        runtime_samples: int = 5,
        agent_version: str = "unattested",
        first_pass: float | None = None,
        rework_rate: float | None = None,
        latency_ms: float | None = None,
        reasoning_effort: str | None = "max",
    ) -> dict[str, object]:
        quality: dict[str, object] = {
            "prior": {"evidence_status": "available"},
            "posterior": {
                "lower_bound_95": lower_bound,
                "runtime_sample_count": runtime_samples,
            },
        }
        result: dict[str, object] = {
            "provider": provider,
            "model_id": model_id,
            "agent_version": agent_version,
            "quality": quality,
        }
        if reasoning_effort is not None:
            result["reasoning_effort"] = reasoning_effort
        if first_pass is not None:
            result["first_pass_rate"] = first_pass
        if rework_rate is not None:
            result["rework_rate"] = rework_rate
        if latency_ms is not None:
            result["latency_ms"] = latency_ms
        return result

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

    def test_control_plane_model_selects_exact_astra_and_rejects_unavailable_or_worker_use(self) -> None:
        sol = capability(
            "codex",
            "gpt-5.6-sol",
            roles=("planner", "verifier", "control"),
            task_types=("*",),
            complexities=("*",),
            quality=100,
        )
        astra = capability(
            "codex",
            "gpt-6-astra",
            roles=("planner", "verifier", "control"),
            task_types=("*",),
            complexities=("*",),
            quality=100,
        )
        control_request = request(
            role="planner",
            task_type="architecture",
            complexity="high",
            control_plane_model="gpt-6-astra",
        )

        selected = route_capability_snapshot(snapshot(sol, astra), control_request)
        self.assertTrue(selected.accepted)
        self.assertEqual(selected.model, "gpt-6-astra")
        sol_rejection = next(item for item in selected.rejected_candidates if item.model == "gpt-5.6-sol")
        self.assertIn("control_plane_model", sol_rejection.reasons[0])

        unavailable = route_capability_snapshot(snapshot(sol), control_request)
        self.assertFalse(unavailable.accepted)
        self.assertIn("control_plane_model", unavailable.rejected_candidates[0].reasons[0])

        future_astra = capability(
            "codex",
            "gpt-6.1-astra",
            roles=("planner", "verifier", "control"),
            task_types=("*",),
            complexities=("*",),
            quality=100,
        )
        forged = route_capability_snapshot(
            snapshot(future_astra),
            request(
                role="planner",
                task_type="architecture",
                complexity="high",
                control_plane_model="gpt-6.1-astra",
            ),
        )
        self.assertFalse(forged.accepted)
        self.assertIn(
            "exact admitted Codex control-plane ID",
            " ".join(forged.rejected_candidates[0].reasons),
        )

        worker = route_capability_snapshot(snapshot(astra), request())
        self.assertFalse(worker.accepted)
        self.assertIn(
            "Astra is reserved for planner, cross-module control, and final verifier roles",
            worker.rejected_candidates[0].reasons,
        )
        with self.assertRaisesRegex(ValueError, "control_plane_model"):
            route_capability_snapshot(snapshot(astra), request(control_plane_model="gpt-6-astra"))

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

    def test_standard_quality_equivalence_band_allows_lower_cost_candidate(self) -> None:
        expensive_best = capability("codex", "gpt-5.6-terra", quality=90, cost=100)
        cheap_equivalent = capability("codex", "gpt-5.6-luna", quality=89, cost=1)

        decision = route_capability_snapshot(
            snapshot(expensive_best, cheap_equivalent),
            request(acceptance_risk="standard", independent_slice=True),
        )

        self.assertEqual(decision.model, "gpt-5.6-luna")
        self.assertEqual(decision.ranking_algorithm_version, "quality-equivalence-efficiency-v1")
        self.assertTrue(decision.selected.quality_equivalence_band)  # type: ignore[union-attr]
        self.assertEqual(decision.selected.quality_gap, 1)  # type: ignore[union-attr]
        self.assertEqual(decision.selected.quality_equivalence_tolerance, 2)  # type: ignore[union-attr]
        self.assertEqual(
            decision.selected.ranking_algorithm_version,  # type: ignore[union-attr]
            "quality-equivalence-efficiency-v1",
        )

    def test_quality_outside_standard_band_cannot_win_on_cost(self) -> None:
        expensive_best = capability("codex", "gpt-5.6-terra", quality=90, cost=100)
        cheap_outside = capability("codex", "gpt-5.6-luna", quality=85, cost=1)

        decision = route_capability_snapshot(
            snapshot(expensive_best, cheap_outside),
            request(allow_parallel_providers=True, independent_slice=True),
        )

        self.assertEqual(decision.model, "gpt-5.6-terra")
        self.assertTrue(decision.ranked_candidates[0].quality_equivalence_band)
        self.assertFalse(decision.ranked_candidates[1].quality_equivalence_band)
        self.assertEqual([item.model for item in decision.parallel_candidates], ["gpt-5.6-terra"])

    def test_critical_risk_requires_exact_quality_frontier(self) -> None:
        best = capability("codex", "gpt-5.6-terra", quality=90, cost=100)
        cheaper = capability("codex", "gpt-5.6-luna", quality=89, cost=1)

        decision = route_capability_snapshot(
            snapshot(best, cheaper),
            request(acceptance_risk="critical", independent_slice=True),
        )

        self.assertEqual(decision.model, "gpt-5.6-terra")
        self.assertEqual(decision.ranked_candidates[0].quality_equivalence_tolerance, 0)
        self.assertFalse(decision.ranked_candidates[1].quality_equivalence_band)

    def test_external_quality_and_consistency_are_not_local_success_rates(self) -> None:
        first = self._calibrated_candidate("codex", "gpt-5.6-luna", 0.70)
        first["quality"]["prior"]["external_signals"] = {  # type: ignore[index]
            "quality_mean": 0.99,
            "consistency_mean": 0.99,
            "consistency_std_mean": 0.01,
            "observed_cost_usd_mean": 0.01,
            "cost_surprise_mean": 0.02,
            "source_count": 3,
        }
        second = self._calibrated_candidate("codex", "gpt-5.6-terra", 0.70)
        second["quality"]["prior"]["external_signals"] = {  # type: ignore[index]
            "quality_mean": 0.10,
            "consistency_mean": 0.10,
            "consistency_std_mean": 0.50,
            "observed_cost_usd_mean": 0.90,
            "cost_surprise_mean": 0.80,
            "source_count": 1,
        }
        catalog = snapshot(
            capability("codex", "gpt-5.6-luna", quality=80, cost=1),
            capability("codex", "gpt-5.6-terra", quality=80, cost=1),
        )
        catalog["performance_calibration"] = self._calibration(first, second)

        decision = route_capability_snapshot(catalog, request(independent_slice=True))

        self.assertEqual(decision.ranked_candidates[0].ranking_quality_score, 70)
        self.assertEqual(decision.ranked_candidates[1].ranking_quality_score, 70)
        self.assertEqual(decision.ranked_candidates[0].external_quality_mean, 0.99)
        self.assertEqual(decision.ranked_candidates[1].external_quality_mean, 0.10)
        self.assertEqual(decision.ranked_candidates[0].external_observed_cost_mean, 0.01)
        self.assertNotIn(
            "external_observed_cost_usd_mean",
            decision.ranked_candidates[0].to_dict(),
        )
        self.assertNotEqual(
            decision.ranked_candidates[0].performance_consistency_risk,
            decision.ranked_candidates[1].performance_consistency_risk,
        )
        self.assertEqual(decision.ranked_candidates[0].performance_p_first_rate, 0.70)

    def test_efficiency_receipt_is_serializable_and_deterministic(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=80, cost=2)
        catalog = snapshot(luna)
        candidate = self._calibrated_candidate(
            "codex",
            "gpt-5.6-luna",
            0.70,
            first_pass=0.80,
            latency_ms=250,
        )
        catalog["performance_calibration"] = self._calibration(candidate)

        first = route_capability_snapshot(catalog, request())
        second = route_capability_snapshot(catalog, request())

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(json.loads(json.dumps(first.to_dict())), first.to_dict())
        self.assertEqual(first.selected.expected_attempts, 1.25)  # type: ignore[union-attr]
        self.assertEqual(first.selected.expected_completion_units, 2.5)  # type: ignore[union-attr]
        self.assertEqual(first.selected.efficiency_score, 0.4)  # type: ignore[union-attr]

    def test_external_signals_cannot_bypass_claude_quota_gate(self) -> None:
        sonnet = capability("claude", "sonnet", quality=80, cost=1)
        luna = capability("codex", "gpt-5.6-luna", quality=79, cost=100)
        catalog = snapshot(sonnet, luna, quota=healthy_quota(remaining_percent=20))
        external = self._calibrated_candidate("claude", "sonnet", 0.99)
        external["quality"]["prior"]["external_signals"] = {  # type: ignore[index]
            "quality_mean": 0.99,
            "consistency_mean": 0.99,
            "observed_cost_usd_mean": 0.001,
            "source_count": 5,
        }
        catalog["performance_calibration"] = self._calibration(external)

        decision = route_capability_snapshot(catalog, request())

        self.assertEqual(decision.model, "gpt-5.6-luna")
        claude_rejection = next(
            item for item in decision.rejected_candidates if item.provider == "claude"
        )
        self.assertIn("20% hard reserve", " ".join(claude_rejection.reasons))

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

    def test_exact_calibrated_lower_bound_changes_rank_after_hard_gates(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=80, cost=1)
        terra = capability("codex", "gpt-5.6-terra", quality=80, cost=100)
        catalog = snapshot(luna, terra)
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.45),
            self._calibrated_candidate("codex", "gpt-5.6-terra", 0.75),
        )

        first = route_capability_snapshot(catalog, request(independent_slice=True))
        self.assertEqual(first.model, "gpt-5.6-terra")
        self.assertEqual(first.selected.quality_source, "calibrated")  # type: ignore[union-attr]
        self.assertEqual(first.selected.quality_score, 80)  # type: ignore[union-attr]
        self.assertEqual(first.selected.ranking_quality_score, 75)  # type: ignore[union-attr]
        self.assertEqual(first.selected.runtime_sample_count, 5)  # type: ignore[union-attr]

        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.85),
            self._calibrated_candidate("codex", "gpt-5.6-terra", 0.35),
        )
        second = route_capability_snapshot(catalog, request(independent_slice=True))
        self.assertEqual(second.model, "gpt-5.6-luna")
        self.assertEqual(second.performance_snapshot_id, "performance-" + "a" * 16)
        self.assertEqual(second.performance_digest, "b" * 64)

    def test_performance_candidate_requires_exact_reasoning_effort(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=80, cost=1, efforts=("high", "max"))
        catalog = snapshot(luna)
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate(
                "codex",
                "gpt-5.6-luna",
                0.99,
                reasoning_effort="high",
            ),
        )

        decision = route_capability_snapshot(catalog, request(reasoning_effort="max"))

        self.assertEqual(decision.model, "gpt-5.6-luna")
        luna_candidate = decision.selected
        self.assertIsNotNone(luna_candidate)
        self.assertEqual(luna_candidate.quality_source, "declared")
        self.assertIsNone(luna_candidate.performance_lower_bound_95)

    def test_runtime_first_pass_rework_and_latency_break_equal_quality_bounds(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", quality=80, cost=1)
        terra = capability("codex", "gpt-5.6-terra", quality=80, cost=100)
        catalog = snapshot(luna, terra)
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate(
                "codex",
                "gpt-5.6-luna",
                0.70,
                first_pass=0.80,
                rework_rate=0.20,
                latency_ms=300,
            ),
            self._calibrated_candidate(
                "codex",
                "gpt-5.6-terra",
                0.70,
                first_pass=0.80,
                rework_rate=0.10,
                latency_ms=100,
            ),
        )

        decision = route_capability_snapshot(catalog, request(independent_slice=True))

        self.assertEqual(decision.model, "gpt-5.6-terra")
        self.assertEqual(decision.selected.performance_rework_rate, 0.10)  # type: ignore[union-attr]
        self.assertEqual(decision.selected.performance_latency_ms, 100)  # type: ignore[union-attr]

    def test_performance_cannot_bypass_quality_or_capability_hard_gates(self) -> None:
        low_quality = capability("codex", "gpt-5.6-luna", quality=60)
        good = capability("codex", "gpt-5.6-terra", quality=80)
        catalog = snapshot(low_quality, good)
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.99),
            self._calibrated_candidate("codex", "gpt-5.6-terra", 0.20),
        )

        decision = route_capability_snapshot(
            catalog,
            request(quality_floor=70, independent_slice=True),
        )

        self.assertEqual(decision.model, "gpt-5.6-terra")
        rejection = next(
            item for item in decision.rejected_candidates if item.model == "gpt-5.6-luna"
        )
        self.assertIn("below required quality floor", " ".join(rejection.reasons))

    def test_spark_without_public_pass_rate_uses_declared_quality_only(self) -> None:
        spark = capability(
            "codex",
            "gpt-5.3-codex-spark",
            complexities=("low",),
            quality=60,
        )
        catalog = snapshot(spark)
        catalog["performance_calibration"] = self._calibration(
            {
                "provider": "codex",
                "model_id": "gpt-5.3-codex-spark",
                "agent_version": "unattested",
                "quality": {
                    "prior": {"evidence_status": "unavailable"},
                    "posterior": {
                        "lower_bound_95": 0.99,
                        "runtime_sample_count": 0,
                    },
                },
            },
            complexity="low",
        )
        decision = route_capability_snapshot(
            catalog,
            request(
                complexity="low",
                quality_floor=60,
                low_risk=True,
                short_task=True,
                mechanically_verifiable=True,
            ),
        )

        self.assertEqual(decision.model, "gpt-5.3-codex-spark")
        self.assertEqual(decision.selected.quality_source, "declared")  # type: ignore[union-attr]
        self.assertIsNone(decision.selected.performance_lower_bound_95)  # type: ignore[union-attr]

    def test_performance_contexts_select_exact_dag_bucket(self) -> None:
        # The task-level calibration is implementation/standard, but the
        # planner may specialize one node into docs/low.  The exact context
        # must win over both the task-level candidates and declared cost.
        luna = capability(
            "codex",
            "gpt-5.6-luna",
            task_types=("docs",),
            complexities=("low",),
            quality=80,
            cost=1,
        )
        sonnet = capability(
            "claude",
            "sonnet",
            task_types=("docs",),
            complexities=("low",),
            quality=80,
            cost=100,
        )
        calibration = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.95),
            self._calibrated_candidate("claude", "sonnet", 0.10),
            contexts=[
                {
                    "task_type": "implementation",
                    "complexity": "standard",
                    "candidates": [
                        self._calibrated_candidate("codex", "gpt-5.6-luna", 0.95),
                        self._calibrated_candidate("claude", "sonnet", 0.10),
                    ],
                },
                {
                    "task_type": "docs",
                    "complexity": "low",
                    "candidates": [
                        self._calibrated_candidate("codex", "gpt-5.6-luna", 0.20),
                        self._calibrated_candidate("claude", "sonnet", 0.90),
                    ],
                },
            ],
        )
        catalog = snapshot(luna, sonnet)
        catalog["performance_calibration"] = calibration

        decision = route_capability_snapshot(
            catalog,
            request(task_type="docs", complexity="low"),
        )

        self.assertEqual(decision.model, "sonnet")
        self.assertEqual(decision.selected.quality_source, "calibrated")  # type: ignore[union-attr]
        self.assertEqual(decision.selected.performance_lower_bound_95, 0.90)  # type: ignore[union-attr]
        self.assertEqual(decision.performance_snapshot_id, "performance-" + "a" * 16)
        self.assertEqual(decision.performance_digest, "b" * 64)

    def test_performance_contexts_without_exact_bucket_fall_back_to_declared(self) -> None:
        luna = capability(
            "codex",
            "gpt-5.6-luna",
            task_types=("tests",),
            complexities=("standard",),
            quality=80,
            cost=1,
        )
        sonnet = capability(
            "claude",
            "sonnet",
            task_types=("tests",),
            complexities=("standard",),
            quality=85,
            cost=100,
        )
        catalog = snapshot(luna, sonnet)
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.99),
            contexts=[
                {
                    "task_type": "docs",
                    "complexity": "low",
                    "candidates": [
                        self._calibrated_candidate("codex", "gpt-5.6-luna", 0.99),
                        self._calibrated_candidate("claude", "sonnet", 0.01),
                    ],
                }
            ],
        )

        decision = route_capability_snapshot(
            catalog,
            request(task_type="tests", complexity="standard"),
        )

        self.assertEqual(decision.model, "sonnet")
        self.assertEqual(decision.selected.quality_source, "declared")  # type: ignore[union-attr]
        self.assertIsNone(decision.selected.performance_lower_bound_95)  # type: ignore[union-attr]
        self.assertEqual(decision.performance_snapshot_id, "performance-" + "a" * 16)
        self.assertEqual(decision.performance_digest, "b" * 64)

    def test_performance_context_identity_conflict_is_rejected(self) -> None:
        luna = capability(
            "codex",
            "gpt-5.6-luna",
            task_types=("docs",),
            complexities=("low",),
        )
        candidate = self._calibrated_candidate("codex", "gpt-5.6-luna", 0.8)
        for field, value, message in (
            (
                "snapshot_id",
                "performance-" + "c" * 16,
                "performance calibration context snapshot",
            ),
            ("digest", "c" * 64, "performance calibration context digest"),
        ):
            with self.subTest(field=field):
                context = {
                    "task_type": "docs",
                    "complexity": "low",
                    "candidates": [candidate],
                    field: value,
                }
                catalog = snapshot(luna)
                catalog["performance_calibration"] = self._calibration(
                    contexts=[context]
                )
                with self.assertRaisesRegex(ValueError, message):
                    route_capability_snapshot(
                        catalog,
                        request(task_type="docs", complexity="low"),
                    )

    def test_performance_snapshot_mismatch_is_rejected(self) -> None:
        catalog = snapshot(capability("codex", "gpt-5.6-luna"))
        catalog["performance_calibration"] = self._calibration(
            self._calibrated_candidate("codex", "gpt-5.6-luna", 0.8),
        )
        with self.assertRaisesRegex(ValueError, "performance snapshot"):
            route_capability_snapshot(
                catalog,
                request(
                    performance_snapshot_id="performance-" + "c" * 16,
                    performance_digest="b" * 64,
                ),
            )


if __name__ == "__main__":
    unittest.main()
