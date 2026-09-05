from __future__ import annotations

import unittest

from codex_workbench.model import LEGACY_ROUTING_STRATEGY_VERSION, TaskContract
from codex_workbench.routing import route_task


CATALOG_ID = "catalog-routing-receipt"
CATALOG_DIGEST = "d" * 64


def _catalog() -> dict[str, object]:
    return {
        "snapshot_id": CATALOG_ID,
        "digest": CATALOG_DIGEST,
        "capabilities": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "capability_id": "codex:luna",
                "status": "available",
                "routable": True,
                "runtime_available": True,
                "roles": ["worker"],
                "task_types": ["implementation"],
                "complexities": ["standard"],
                "features": ["structured_output"],
                "reasoning_efforts": ["max"],
                "quality_score": 80,
                "estimated_cost_units": 1,
                "estimated_latency_ms": 100,
                "estimated_throughput": 10,
                "concurrency_capacity": 2,
                "active_count": 0,
            },
        ],
    }


def _contract(*, pinned_catalog: bool = True, **changes: object) -> TaskContract:
    values: dict[str, object] = {
        "task_id": "routing-receipt-task",
        "repository": "/tmp/example",
        "base_sha": "abc123",
        "objective": "bounded implementation",
        "allowed_scope": ("src",),
    }
    if pinned_catalog:
        values.update(
            capability_snapshot_id=CATALOG_ID,
            capability_digest=CATALOG_DIGEST,
        )
    values.update(changes)
    return TaskContract(**values)  # type: ignore[arg-type]


def _local_calibration() -> dict[str, object]:
    return {
        "status": "ok",
        "snapshot_id": "performance-" + "a" * 16,
        "digest": "b" * 64,
        "semantic_version": "local-outcomes-only-v2",
        "calibration_policy": {
            "local_outcomes_only": True,
            "external_evidence_updates_beta": False,
        },
        "task_type": "implementation",
        "complexity": "standard",
        "candidates": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "agent_version": "unattested",
                "reasoning_effort": "max",
                "quality": {
                    "prior": {"evidence_status": "available"},
                    "posterior": {
                        "lower_bound_95": 0.7,
                        "runtime_sample_count": 5,
                    },
                },
                "calibration_cohort": {
                    "task_type": "implementation",
                    "complexity": "standard",
                    "harness": "workbench-verifier-v1",
                    "score_kind": "verified-task-acceptance",
                    "reasoning_effort": "max",
                    "agent_name": "codex-cli",
                    "agent_version": "unattested",
                },
            },
        ],
    }


class RoutingReceiptBridgeTests(unittest.TestCase):
    def test_v3_wrapper_serializes_policy_receipt_and_empirical_abstention(self) -> None:
        decision = route_task(_contract(), capability_snapshot=_catalog())

        receipt = decision.performance_routing_receipt
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["policy_version"], "model-routing-v3")
        self.assertEqual(receipt["ranking_algorithm_version"], "quality-equivalence-efficiency-v1")
        self.assertEqual(receipt["performance_semantic_status"], "no-performance-calibration")
        self.assertEqual(receipt["empirical_ranking_status"], "abstained")
        self.assertIn(
            "not every declared quality-equivalence candidate has usable local runtime outcomes",
            receipt["empirical_ranking_reason"],
        )
        self.assertEqual(receipt["source"], "declared-policy")
        self.assertEqual(len(receipt["public_evidence_summary"]["candidate_summaries"]), 1)
        self.assertEqual(decision.to_dict()["performance_routing_receipt"], receipt)

    def test_v3_wrapper_marks_exact_local_observation_separately(self) -> None:
        decision = route_task(
            _contract(),
            capability_snapshot=_catalog(),
            performance_calibration=_local_calibration(),
        )

        receipt = decision.performance_routing_receipt
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["source"], "local-runtime")
        self.assertEqual(receipt["performance_semantic_status"], "local-outcomes-only-v2")
        self.assertEqual(receipt["empirical_ranking_status"], "used")

    def test_legacy_route_keeps_selection_and_has_no_v3_receipt(self) -> None:
        decision = route_task(
            _contract(pinned_catalog=False, routing_strategy=LEGACY_ROUTING_STRATEGY_VERSION),
            claude_models_available=(),
        )

        self.assertEqual((decision.executor, decision.model), ("codex", "gpt-5.6-luna"))
        self.assertEqual(decision.strategy_version, LEGACY_ROUTING_STRATEGY_VERSION)
        self.assertIsNone(decision.performance_routing_receipt)
        self.assertEqual(decision.quality_source, "declared-policy")
        self.assertIsNone(decision.to_dict()["performance_routing_receipt"])


if __name__ == "__main__":
    unittest.main()
