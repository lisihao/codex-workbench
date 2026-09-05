from __future__ import annotations

import copy
import unittest

from codex_workbench.performance import load_benchmark_baseline
from codex_workbench.routing_evaluation import (
    calibration_for_requests,
    evaluate_routes,
)


def capability(
    provider: str,
    model: str,
    *,
    quality: float = 80,
    cost: float = 10,
    quota_pool: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "capability_id": f"{provider}:{model}",
        "provider": provider,
        "model": model,
        "status": "active",
        "routable": True,
        "runtime_available": True,
        "roles": ["worker"],
        "task_types": ["implementation"],
        "complexities": ["standard"],
        "reasoning_efforts": ["max"],
        "quality_score": quality,
        "estimated_cost_units": cost,
        "estimated_latency_ms": 100,
        "estimated_throughput": 10,
        "concurrency_capacity": 4,
        "active_count": 0,
    }
    if quota_pool is not None:
        record["quota_pool"] = quota_pool
    return record


def catalog(*records: dict[str, object], quota: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "snapshot_id": "catalog-evaluation-a",
        "digest": "catalog-digest-a",
        "provider_runtime": {
            "codex": {"available": True},
            "claude": {"available": True},
        },
        "claude_quota": quota or {
            "auth_ok": True,
            "remaining_percent": 80,
            "five_hour_remaining": 80,
            "weekly_all_remaining": 80,
            "weekly_sonnet_remaining": 80,
        },
        "capabilities": list(records),
    }


def request(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "request_id": "request-a",
        "sample_type": "synthetic",
        "role": "worker",
        "task_type": "implementation",
        "complexity": "standard",
        "quality_floor": 70,
        "acceptance_risk": "standard",
        "bounded": True,
        "independent_slice": True,
        "low_risk": False,
        "short_task": False,
        "mechanically_verifiable": False,
        "reasoning_effort": "max",
        "allowed_scope": ("src", "tests"),
        "claude_quota": {
            "auth_ok": True,
            "remaining_percent": 80,
            "five_hour_remaining": 80,
            "weekly_all_remaining": 80,
            "weekly_sonnet_remaining": 80,
        },
        "provider_capacity": {
            "codex": {"capacity": 4, "active": 0},
            "claude": {"capacity": 4, "active": 0},
        },
    }
    value.update(changes)
    return value


def calibration(
    *candidates: dict[str, object],
    snapshot_id: str = "performance-aaaaaaaaaaaaaaaa",
    digest: str = "b" * 64,
    contexts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "digest": digest,
        "task_type": "implementation",
        "complexity": "standard",
        "candidates": list(candidates),
    }
    if contexts is not None:
        value["contexts"] = contexts
    return value


def calibrated_candidate(provider: str, model: str, lower_bound: float) -> dict[str, object]:
    return {
        "provider": provider,
        "model_id": model,
        "agent_version": "unattested",
        "reasoning_effort": "max",
        "quality": {
            "prior": {"evidence_status": "available"},
            "posterior": {
                "lower_bound_95": lower_bound,
                "runtime_sample_count": 5,
            },
        },
    }


class RoutingEvaluationTests(unittest.TestCase):
    def test_input_calibration_changes_route_inside_quality_band(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", cost=1)
        terra = capability("codex", "gpt-5.6-terra", cost=100)
        task = request()
        before = copy.deepcopy(task)
        result = evaluate_routes(
            catalog(luna, terra),
            [task],
            calibrations={
                "without_ai_frontier": calibration(
                    calibrated_candidate("codex", "gpt-5.6-luna", 0.45),
                    calibrated_candidate("codex", "gpt-5.6-terra", 0.75),
                ),
            },
        )

        baseline_row = result["strategies"]["declared_baseline"]["rows"][0]
        comparison_row = result["strategies"]["without_ai_frontier"]["rows"][0]
        self.assertEqual(task, before)
        self.assertEqual(result["variant_order"], ["declared_baseline", "without_ai_frontier"])
        self.assertEqual(baseline_row["route"], "gpt-5.6-luna")
        self.assertEqual(comparison_row["route"], "gpt-5.6-terra")
        self.assertTrue(baseline_row["quality_band"])
        self.assertTrue(comparison_row["quality_band"])
        self.assertEqual(comparison_row["candidate_rank"], 1)
        self.assertEqual(comparison_row["source_snapshot"]["performance_snapshot_id"], "performance-aaaaaaaaaaaaaaaa")
        self.assertEqual(
            result["strategies"]["without_ai_frontier"]["decision_change"],
            {
                "baseline_strategy": "declared_baseline",
                "numerator": 1,
                "denominator": 1,
                "comparable_rows": 1,
                "rate": 1.0,
            },
        )
        self.assertFalse(result["delivery_improvement_proven"])
        self.assertIsNone(result["actual_savings"])

    def test_hard_quality_and_quota_rejections_remain_rejections(self) -> None:
        low_quality = capability("codex", "gpt-5.6-luna", quality=60)
        sonnet = capability("claude", "sonnet", quality=95)
        result = evaluate_routes(
            catalog(
                low_quality,
                sonnet,
                quota={
                    "auth_ok": True,
                    "remaining_percent": 20,
                    "five_hour_remaining": 20,
                    "weekly_all_remaining": 20,
                    "weekly_sonnet_remaining": 20,
                },
            ),
            [
                request(
                    independent_slice=False,
                    claude_quota={
                        "auth_ok": True,
                        "remaining_percent": 20,
                        "five_hour_remaining": 20,
                        "weekly_all_remaining": 20,
                        "weekly_sonnet_remaining": 20,
                    },
                )
            ],
            calibrations={
                "current": calibration(
                    calibrated_candidate("codex", "gpt-5.6-luna", 0.99),
                    calibrated_candidate("claude", "sonnet", 0.99),
                ),
            },
        )

        for label in result["variant_order"]:
            row = result["strategies"][label]["rows"][0]
            self.assertFalse(row["accepted"])
            self.assertIsNone(row["route"])
            reasons = " ".join(row["excluded_reasons"])
            self.assertIn("below required quality floor", reasons)
            self.assertIn("20% hard reserve", reasons)
        self.assertEqual(result["strategies"]["current"]["coverage"]["denominator"], 1)

    def test_unchanged_route_has_no_fabricated_improvement(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", cost=1)
        terra = capability("codex", "gpt-5.6-terra", cost=100)
        result = evaluate_routes(
            catalog(luna, terra),
            [request()],
            calibrations={
                "current": calibration(
                    calibrated_candidate("codex", "gpt-5.6-luna", 0.75),
                    calibrated_candidate("codex", "gpt-5.6-terra", 0.75),
                ),
            },
        )

        current = result["strategies"]["current"]
        self.assertEqual(current["rows"][0]["route"], "gpt-5.6-luna")
        self.assertEqual(current["decision_change"]["numerator"], 0)
        self.assertEqual(current["decision_change"]["denominator"], 1)
        self.assertEqual(current["decision_change"]["rate"], 0.0)
        self.assertFalse(current["delivery_improvement_proven"])
        self.assertIsNone(current["actual_savings"])

    def test_ai_frontier_incremental_change_is_separate_from_declared_baseline(self) -> None:
        luna = capability("codex", "gpt-5.6-luna", cost=1)
        terra = capability("codex", "gpt-5.6-terra", cost=100)
        without_frontier = calibration(
            calibrated_candidate("codex", "gpt-5.6-luna", 0.45),
            calibrated_candidate("codex", "gpt-5.6-terra", 0.75),
        )

        equal_result = evaluate_routes(
            catalog(luna, terra),
            [request()],
            calibrations={
                "without_ai_frontier": without_frontier,
                "current": without_frontier,
            },
        )
        equal_incremental = equal_result["ai_frontier_incremental"]
        self.assertEqual(equal_result["strategies"]["current"]["decision_change"]["numerator"], 1)
        self.assertEqual(equal_incremental["numerator"], 0)
        self.assertEqual(equal_incremental["denominator"], 1)
        self.assertEqual(equal_incremental["comparable_rows"], 1)
        self.assertEqual(equal_incremental["rate"], 0.0)
        self.assertFalse(equal_incremental["delivery_improvement_proven"])
        self.assertIsNone(equal_incremental["actual_savings"])

        changed_result = evaluate_routes(
            catalog(luna, terra),
            [request()],
            calibrations={
                "without_ai_frontier": without_frontier,
                "current": calibration(
                    calibrated_candidate("codex", "gpt-5.6-luna", 0.75),
                    calibrated_candidate("codex", "gpt-5.6-terra", 0.45),
                ),
            },
        )
        changed_incremental = changed_result["ai_frontier_incremental"]
        self.assertEqual(changed_result["strategies"]["current"]["rows"][0]["route"], "gpt-5.6-luna")
        self.assertEqual(changed_incremental["numerator"], 1)
        self.assertEqual(changed_incremental["denominator"], 1)
        self.assertEqual(changed_incremental["rate"], 1.0)
        self.assertFalse(changed_incremental["delivery_improvement_proven"])
        self.assertIsNone(changed_incremental["actual_savings"])

    def test_empty_batch_is_insufficient_data_with_null_rates(self) -> None:
        result = evaluate_routes(
            catalog(capability("codex", "gpt-5.6-luna")),
            [],
            calibrations={"current": calibration()},
        )

        self.assertEqual(result["status"], "insufficient-data")
        self.assertEqual(result["request_count"], 0)
        for label in result["variant_order"]:
            strategy = result["strategies"][label]
            self.assertIsNone(strategy["coverage"]["rate"])
            self.assertIsNone(strategy["decision_change"]["rate"])
            self.assertEqual(strategy["decision_change"]["denominator"], 0)

    def test_snapshot_mismatch_is_invalid_but_request_pin_is_variant_bound(self) -> None:
        luna = capability("codex", "gpt-5.6-luna")
        task = request(
            performance_snapshot_id="performance-old",
            performance_digest="c" * 64,
            performance_calibration={"snapshot_id": "performance-old"},
        )
        valid = calibration(calibrated_candidate("codex", "gpt-5.6-luna", 0.75))
        result = evaluate_routes(catalog(luna), [task], calibrations={"current": valid})
        self.assertEqual(result["strategies"]["current"]["rows"][0]["route"], "gpt-5.6-luna")
        self.assertEqual(
            result["strategies"]["current"]["rows"][0]["source_snapshot"]["performance_snapshot_id"],
            "performance-aaaaaaaaaaaaaaaa",
        )

        mismatched = calibration(
            calibrated_candidate("codex", "gpt-5.6-luna", 0.75),
            contexts=[
                {
                    "task_type": "implementation",
                    "complexity": "standard",
                    "snapshot_id": "performance-cccccccccccccccc",
                    "digest": "d" * 64,
                    "candidates": [],
                }
            ],
        )
        with self.assertRaisesRegex(ValueError, "mismatched performance"):
            evaluate_routes(catalog(luna), [request()], calibrations={"current": mismatched})

    def test_calibration_for_requests_is_in_memory_and_context_specific(self) -> None:
        snapshot = {
            "snapshot_id": "performance-aaaaaaaaaaaaaaaa",
            "digest": "b" * 64,
            "baseline": load_benchmark_baseline(),
            "metrics": [],
            "source_provenance": {"model_identities": {}},
        }
        active_catalog = {
            "models": [
                {
                    "provider": "codex",
                    "model_id": "gpt-5.6-luna",
                    "agent_cli_version": "unattested",
                    "routable": True,
                    "reasoning": {"preferred_effort": "max"},
                }
            ]
        }
        matrix = calibration_for_requests(
            snapshot,
            active_catalog,
            [
                {"task_type": "docs", "complexity": "low"},
                {"task_type": "implementation", "complexity": "standard"},
                {"task_type": "docs", "complexity": "low"},
            ],
        )

        self.assertEqual(matrix["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(matrix["digest"], snapshot["digest"])
        self.assertEqual(
            [(context["task_type"], context["complexity"]) for context in matrix["contexts"]],
            [("docs", "low"), ("implementation", "standard")],
        )
        self.assertTrue(
            all(
                context["performance_snapshot_id"] == snapshot["snapshot_id"]
                and context["performance_digest"] == snapshot["digest"]
                for context in matrix["contexts"]
            )
        )


if __name__ == "__main__":
    unittest.main()
