from __future__ import annotations

import unittest

from codex_workbench.public_evidence_ranking import canonical_cohort_key
from codex_workbench.routing_v3 import route_capability_snapshot
from codex_workbench.performance import PerformanceRegistry, build_performance_snapshot, load_benchmark_baseline


def capability(
    model: str,
    *,
    quality: float = 80,
    cost: float = 10,
    efforts: tuple[str, ...] = ("max",),
) -> dict[str, object]:
    return {
        "capability_id": f"codex:{model}",
        "provider": "codex",
        "model": model,
        "status": "active",
        "routable": True,
        "runtime_available": True,
        "roles": ("worker",),
        "task_types": ("implementation",),
        "complexities": ("standard",),
        "features": ("structured_output",),
        "reasoning_efforts": efforts,
        "quality_score": quality,
        "estimated_cost_units": cost,
        "estimated_latency_ms": 100,
        "estimated_throughput": 10,
        "concurrency_capacity": 2,
        "active_count": 0,
    }


def request(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "role": "worker",
        "task_type": "implementation",
        "complexity": "standard",
        "quality_floor": 70,
        "acceptance_risk": "standard",
        "reasoning_effort": "max",
        "independent_slice": True,
        "bounded": True,
    }
    result.update(changes)
    return result


def public_record(
    model: str,
    *,
    source: str,
    value: float,
    sample_count: int | None,
    effort: str = "max",
    comparability: str = "comparable",
    benchmark: str = "coding-bench",
    harness: str = "exact-harness-v1",
    unit: str = "proportion",
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": source,
        "provider": "codex",
        "model_id": model,
        "canonical_model_id": model,
        "reasoning_effort": effort,
        "metric_kind": "pass_rate",
        "score_kind": "resolved_rate",
        "value": value,
        "unit": unit,
        "sample_count": sample_count,
        "observed_at": "2026-09-04T00:00:00Z",
        "benchmark": benchmark,
        "benchmark_version": "2026-09",
        "harness": harness,
        "domain": "coding",
        "task_type": "implementation",
        "lineage_id": f"{source}:{model}:{benchmark}",
        "correlation_group": f"{source}:{benchmark}",
        "comparability": {"status": comparability, "missing_conditions": []},
    }
    record["cohort_key"] = canonical_cohort_key(record)
    return record


def calibration_candidate(
    model: str,
    *,
    lower_bound: float = 0.99,
    runtime_samples: int = 0,
    effort: str = "max",
    public_evidence: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "provider": "codex",
        "model_id": model,
        "canonical_model_id": model,
        "agent_version": "unattested",
        "reasoning_effort": effort,
        "quality": {
            "prior": {"kind": "conservative-policy-prior", "evidence_status": "unavailable"},
            "posterior": {
                "lower_bound_95": lower_bound,
                "runtime_sample_count": runtime_samples,
                "local_success_count": runtime_samples,
                "local_failure_count": 0,
            },
        },
        "public_evidence": public_evidence or [],
    }


def catalog(*models: dict[str, object], calibration: dict[str, object]) -> dict[str, object]:
    return {
        "snapshot_id": "catalog-public-evidence",
        "digest": "catalog-public-evidence-digest",
        "provider_runtime": {"codex": {"available": True}},
        "capabilities": list(models),
        "performance_calibration": calibration,
    }


def v2_calibration(*candidates: dict[str, object]) -> dict[str, object]:
    return {
        "status": "cold-start",
        "snapshot_id": "performance-" + "a" * 16,
        "digest": "b" * 64,
        "semantic_version": "local-outcomes-only-v2",
        "calibration_policy": {
            "local_outcomes_only": True,
            "external_evidence_updates_beta": False,
        },
        "task_type": "implementation",
        "complexity": "standard",
        "candidates": list(candidates),
    }


class RoutingV3PublicEvidenceTests(unittest.TestCase):
    def test_same_cohort_comparable_reference_breaks_a_declared_policy_tie(self) -> None:
        luna = "gpt-5.6-luna"
        terra = "gpt-5.6-terra"
        decision = route_capability_snapshot(
            catalog(
                capability(luna),
                capability(terra),
                calibration=v2_calibration(
                    calibration_candidate(luna, public_evidence=[public_record(luna, source="codex-radar", value=0.70, sample_count=200)]),
                    calibration_candidate(terra, public_evidence=[public_record(terra, source="codex-radar", value=0.95, sample_count=200)]),
                ),
            ),
            request(),
        )

        self.assertEqual(decision.model, terra)
        self.assertEqual(decision.selected.quality_source, "declared-policy")  # type: ignore[union-attr]
        self.assertIsNone(decision.selected.performance_lower_bound_95)  # type: ignore[union-attr]
        self.assertEqual(decision.public_evidence_summary["status"], "used")
        self.assertEqual(decision.selected.public_evidence_summary["status"], "used")  # type: ignore[union-attr]
        self.assertEqual(
            decision.selected.public_evidence_summary["supporting_sources"][0]["source"],  # type: ignore[union-attr]
            "codex-radar",
        )

    def test_incomplete_public_group_keeps_unmeasured_peer_from_free_top_tier(self) -> None:
        luna = "gpt-5.6-luna"
        terra = "gpt-5.6-terra"
        sonnet = "sonnet"
        decision = route_capability_snapshot(
            catalog(
                capability(luna, cost=100),
                capability(terra, cost=1),
                capability(sonnet, cost=10),
                calibration=v2_calibration(
                    calibration_candidate(
                        luna,
                        public_evidence=[
                            public_record(
                                luna,
                                source="codex-radar",
                                value=0.95,
                                sample_count=200,
                            )
                        ],
                    ),
                    calibration_candidate(
                        terra,
                        public_evidence=[
                            public_record(
                                terra,
                                source="codex-radar",
                                value=0.70,
                                sample_count=200,
                            )
                        ],
                    ),
                    calibration_candidate(sonnet),
                ),
            ),
            request(),
        )

        self.assertEqual(decision.model, terra)
        self.assertEqual(decision.public_evidence_summary["status"], "abstained")
        self.assertIn("incomplete-comparison", decision.public_evidence_summary["reason"])
        self.assertEqual(
            decision.public_evidence_summary["preference_ranks"],
            {f"codex:{luna}": 0, f"codex:{terra}": 0, f"codex:{sonnet}": 0},
        )
        self.assertTrue(decision.public_evidence_summary["incomplete_comparisons"])

    def test_gpt55_effort_mismatch_and_ungraded_frontier_reference_do_not_mix(self) -> None:
        gpt55 = "gpt-5.5"
        terra = "gpt-5.6-terra"
        frontier = public_record(
            gpt55,
            source="ai-frontier",
            value=0.99,
            sample_count=None,
            effort="xhigh",
            comparability="reference_only",
            harness="frontier-ungraded",
        )
        decision = route_capability_snapshot(
            catalog(
                capability(gpt55, efforts=("high", "xhigh")),
                capability(terra, efforts=("xhigh",)),
                calibration=v2_calibration(
                    calibration_candidate(
                        gpt55,
                        effort="xhigh",
                        public_evidence=[
                            public_record(gpt55, source="codex-radar", value=0.99, sample_count=200, effort="high"),
                            frontier,
                        ],
                    ),
                    calibration_candidate(terra, effort="xhigh"),
                ),
            ),
            request(reasoning_effort="xhigh"),
        )

        self.assertEqual(decision.model, terra)
        rejection = next(item for item in decision.rejected_candidates if item.model == gpt55)
        self.assertIn("unknown model family", " ".join(rejection.reasons))
        self.assertNotEqual(decision.model, gpt55)
        self.assertEqual(decision.selected.quality_source, "declared-policy")  # type: ignore[union-attr]
        self.assertEqual(decision.selected.public_evidence_summary["status"], "abstained")  # type: ignore[union-attr]
        self.assertEqual(decision.public_evidence_summary["status"], "abstained")

    def test_producer_comparable_public_pair_reaches_routing_consumer(self) -> None:
        luna = "gpt-5.6-luna"
        terra = "gpt-5.6-terra"
        baseline = load_benchmark_baseline()
        records: list[dict[str, object]] = []
        for model, score in ((luna, 0.65), (terra, 0.75)):
            records.append(
                {
                    **baseline["records"][0],
                    "record_id": f"producer-{model}",
                    "provider": "codex",
                    "model_id": model,
                    "model_family": model.rsplit("-", 1)[-1],
                    "task_types": ["implementation"],
                    "task_type": "implementation",
                    "reasoning_effort": "max",
                    "harness": "fixture-harness",
                    "metric_kind": "pass_rate",
                    "score_kind": "resolved_rate",
                    "score": score,
                    "value": score,
                    "unit": "proportion",
                    "sample_count": 400,
                    "source_id": "fixture-source",
                    "lineage_id": f"fixture-{model}",
                    "correlation_group": "fixture-source-v1",
                }
            )
        producer_baseline = {**baseline, "records": records}
        producer_catalog = {
            "catalog_id": "producer-catalog",
            "digest": "c" * 64,
            "agents": {"codex": {"cli_version": "0.153.0", "agent_name": "codex"}},
            "models": [
                {
                    "provider": "codex",
                    "model_id": model,
                    "model_family": model.rsplit("-", 1)[-1],
                    "agent_cli_version": "0.153.0",
                    "agent_name": "codex",
                    "routable": True,
                    "reasoning": {"supported_efforts": ["max"], "preferred_effort": "max"},
                }
                for model in (luna, terra)
            ],
        }
        performance_snapshot = build_performance_snapshot(
            [], [], producer_catalog, baseline=producer_baseline
        )
        calibration = PerformanceRegistry._calibrate_context(
            performance_snapshot,
            producer_catalog,
            producer_baseline,
            task_type="implementation",
            complexity="standard",
        )
        calibration["digest"] = performance_snapshot["digest"]
        terra_evidence = next(
            item
            for item in next(
                candidate for candidate in calibration["candidates"] if candidate["model_id"] == terra
            )["public_evidence"]
            if item["metric_kind"] == "pass_rate"
        )
        self.assertEqual(terra_evidence["comparability"]["status"], "comparable")
        self.assertEqual(
            terra_evidence["cohort_key"],
            '{"benchmark":"SWE-Bench Pro","benchmark_version":"reported-on-gpt-5-6-page","harness":"fixture-harness","metric_kind":"pass_rate","reasoning_effort":"max","task_type":"implementation"}',
        )

        routing_catalog = catalog(
            capability(luna),
            capability(terra),
            calibration=calibration,
        )
        routing_catalog["agents"] = {"codex": {"cli_version": "0.153.0", "agent_name": "codex"}}
        decision = route_capability_snapshot(routing_catalog, request())

        self.assertEqual(decision.model, terra)
        self.assertEqual(decision.public_evidence_summary["status"], "used")
        self.assertEqual(decision.selected.quality_source, "declared-policy")  # type: ignore[union-attr]
        self.assertEqual(
            decision.selected.public_evidence_summary["supporting_sources"][0]["source"],  # type: ignore[union-attr]
            "benchmark-baseline",
        )

    def test_legacy_calibration_is_audit_only_not_a_runtime_quality_signal(self) -> None:
        luna = "gpt-5.6-luna"
        terra = "gpt-5.6-terra"
        legacy = {
            "snapshot_id": "performance-" + "a" * 16,
            "digest": "b" * 64,
            "task_type": "implementation",
            "complexity": "standard",
            "candidates": [
                calibration_candidate(luna, lower_bound=0.10, runtime_samples=5),
                calibration_candidate(terra, lower_bound=0.99, runtime_samples=5),
            ],
        }
        decision = route_capability_snapshot(
            catalog(capability(luna, cost=1), capability(terra, cost=100), calibration=legacy),
            request(),
        )

        self.assertEqual(decision.model, luna)
        self.assertEqual(decision.selected.quality_source, "declared-policy")  # type: ignore[union-attr]
        self.assertEqual(decision.selected.performance_semantic_status, "legacy-audit-only")  # type: ignore[union-attr]
        self.assertEqual(decision.selected.runtime_sample_count, 0)  # type: ignore[union-attr]
        self.assertIsNone(decision.selected.performance_lower_bound_95)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
