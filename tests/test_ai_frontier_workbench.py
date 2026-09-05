from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from ai_frontier_provider import AIFrontierRegistry
from codex_workbench.ai_frontier import (
    AI_FRONTIER_CATEGORY_TASK_TYPES,
    AI_FRONTIER_REFRESH_INTERVAL_SECONDS,
    WorkbenchAIFrontier,
    ai_frontier_public_evidence_records,
)
from codex_workbench.performance import PerformanceRegistry, build_performance_snapshot


def payloads(*, categories: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "reliability_leaderboard": [
            {
                "Executor": "openai/gpt-5.6-luna",
                "Quality": 0.82,
                "Cost": 1.7,
                "Consistency": 0.94,
                "Consistency Std": 0.03,
            },
            {
                "Executor": "openai/gpt-9.9-unknown",
                "Quality": 0.99,
                "Cost": 0.1,
                "Consistency": 0.99,
                "Consistency Std": 0.01,
            },
        ],
        "cost_comparison": [
            {
                "LLMs": "openai/gpt-5.6-luna",
                "Quoted Cost": 2.2,
                "Real Cost": 1.7,
                "Cost Surprise": -0.2,
            },
            {
                "LLMs": "openai/gpt-9.9-unknown",
                "Quoted Cost": 2.2,
                "Real Cost": 0.1,
                "Cost Surprise": -0.9,
            },
        ],
        "model_benchmarks": {
            "openai/gpt-5.6-luna": {
                "categories": categories
                if categories is not None
                else [{"id": "coding", "label": "Coding", "quality": 0.84, "cost": 1.8}],
            }
        },
    }


def catalog() -> dict[str, object]:
    return {
        "catalog_id": "catalog-ai-frontier-test",
        "digest": "a" * 64,
        "models": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "model_family": "luna",
                "routable": True,
            },
            {
                "provider": "codex",
                "model_id": "gpt-9.9-unknown",
                "model_family": "unknown",
                "routable": False,
            },
        ],
    }


class WorkbenchAIFrontierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state_root = root / "state"
        self.receipt = root / "consent.json"
        self.registry = AIFrontierRegistry(self.state_root)
        self.registry.consent_personal_use(
            self.receipt,
            accepted_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _status(
        self,
        *,
        fetched_at: str = "2026-09-04T12:00:00Z",
        stale_after_seconds: int = 60,
        expire_after_seconds: int = 180,
        now: datetime = datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC),
    ) -> dict[str, object]:
        self.registry.import_payloads(
            payloads(),
            self.receipt,
            fetched_at=fetched_at,
            stale_after_seconds=stale_after_seconds,
        )
        return WorkbenchAIFrontier(
            self.state_root,
            self.receipt,
            stale_after_seconds=stale_after_seconds,
            expire_after_seconds=expire_after_seconds,
        ).status(now=now)

    def test_valid_consent_without_snapshot_is_unavailable_not_unauthorized(self) -> None:
        status = WorkbenchAIFrontier(self.state_root, self.receipt).status(
            now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC)
        )

        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["collector_authorization"], "consented")
        self.assertFalse(status["routing_prior_eligible"])
        self.assertFalse(status["offline_cache_available"])

    def test_invalid_current_consent_disables_old_lkg_snapshot(self) -> None:
        self._status()
        self.receipt.write_text("{}", encoding="utf-8")

        status = WorkbenchAIFrontier(self.state_root, self.receipt).status(
            now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC)
        )

        self.assertEqual(status["state"], "unauthorized")
        self.assertEqual(status["collector_authorization"], "unauthorized")
        self.assertEqual(status["snapshot_authorization"], "consented")
        self.assertTrue(status["offline_cache_available"])
        self.assertFalse(status["routing_prior_eligible"])
        self.assertEqual(ai_frontier_public_evidence_records(status, catalog()), [])

    def test_exact_routable_model_enters_but_unknown_and_non_routable_do_not(self) -> None:
        status = self._status()
        records = ai_frontier_public_evidence_records(status, catalog())

        self.assertEqual({record["model_id"] for record in records}, {"gpt-5.6-luna"})
        self.assertEqual({record["domain"] for record in records}, {"coding", "overall"})
        self.assertTrue(all(record["provider"] == "codex" for record in records))
        self.assertTrue(all(record["reference_eligible"] for record in records))
        self.assertTrue(all(record["calibration_eligible"] is False for record in records))
        self.assertTrue(all("effective_sample_strength" not in record for record in records))
        self.assertTrue(all(record["metric_kind"] == "accuracy" for record in records))
        self.assertTrue(all(record["unit"] == "proportion" for record in records))

    def test_category_mapping_and_medical_exclusion_are_explicit(self) -> None:
        categories = [
            {"id": category, "label": category, "quality": 0.6}
            for category in (
                "coding",
                "agentic",
                "reasoning",
                "instruction-following",
                "factuality",
                "medical",
            )
        ]
        self.registry.import_payloads(
            payloads(categories=categories),
            self.receipt,
            fetched_at="2026-09-04T12:00:00Z",
        )
        status = WorkbenchAIFrontier(self.state_root, self.receipt).status(
            now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC)
        )
        records = ai_frontier_public_evidence_records(status, catalog())
        by_domain = {record["domain"]: record for record in records}

        self.assertNotIn("medical", by_domain)
        for category, task_types in AI_FRONTIER_CATEGORY_TASK_TYPES.items():
            self.assertEqual(tuple(by_domain[category]["task_types"]), task_types)

    def test_consistency_is_reference_metadata_not_a_local_success_rate(self) -> None:
        low_consistency = payloads()
        low_consistency["reliability_leaderboard"][0]["Consistency"] = 0.1  # type: ignore[index]
        self.registry.import_payloads(
            low_consistency,
            self.receipt,
            fetched_at="2026-09-04T12:00:00Z",
        )
        status = WorkbenchAIFrontier(self.state_root, self.receipt).status(
            now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC)
        )
        coding = next(
            record
            for record in ai_frontier_public_evidence_records(status, catalog())
            if record["domain"] == "coding"
        )

        self.assertEqual(coding["score"], 0.84)
        self.assertNotEqual(coding["score"], coding["external_signals"]["consistency_mean"])
        self.assertFalse(coding["calibration_eligible"])
        self.assertFalse(coding["routing_prior_eligible"])
        self.assertEqual(coding["external_signals"]["observed_cost_mean"], 1.7)
        self.assertEqual(coding["external_signals"]["cost_surprise_mean"], -0.2)

    def test_fresh_stale_expired_and_offline_lkg_states_bound_prior_use(self) -> None:
        fresh = self._status()
        stale = self._status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))
        expired = self._status(now=datetime(2026, 9, 4, 12, 4, tzinfo=UTC))

        self.assertEqual(fresh["state"], "fresh")
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(expired["state"], "expired")
        self.assertTrue(fresh["offline_cache_available"])
        self.assertTrue(fresh["reference_eligible"])
        self.assertTrue(stale["reference_eligible"])
        self.assertFalse(expired["reference_eligible"])
        self.assertEqual(
            len(ai_frontier_public_evidence_records(fresh, catalog())),
            len(ai_frontier_public_evidence_records(stale, catalog())),
        )
        self.assertEqual(ai_frontier_public_evidence_records(expired, catalog()), [])
        self.assertEqual(AI_FRONTIER_REFRESH_INTERVAL_SECONDS, 72 * 60 * 60)

    def test_performance_snapshot_keeps_ai_frontier_provenance_and_old_radar_argument(self) -> None:
        status = self._status()
        snapshot = build_performance_snapshot(
            [],
            [],
            catalog(),
            radar_status={"provider": "codex-radar-provider", "state": "expired"},
            ai_frontier_status=status,
        )

        external = snapshot["source_provenance"]["external_priors"]
        self.assertIn("codex_radar", external)
        self.assertEqual(external["ai_frontier"]["reference_record_count"], 2)
        self.assertTrue(external["ai_frontier"]["reference_eligible"])
        self.assertFalse(external["ai_frontier"]["routing_prior_eligible"])
        self.assertFalse(external["ai_frontier"]["used_for_calibration"])
        self.assertTrue(external["ai_frontier"]["quality_is_external_metric"])
        self.assertFalse(external["ai_frontier"]["quality_is_local_probability"])
        self.assertFalse(external["ai_frontier"]["quality_is_pseudo_observation"])
        self.assertFalse(external["ai_frontier"]["quality_is_local_first_pass"])
        self.assertFalse(external["ai_frontier"]["consistency_is_success_rate"])
        self.assertFalse(external["ai_frontier"]["cost_is_quota_admission"])
        ai_evidence = [item for item in snapshot["public_evidence"] if item["source"] == "ai-frontier"]

        old_snapshot = build_performance_snapshot([], [], catalog())
        self.assertNotIn("external_priors", old_snapshot["source_provenance"])

    def test_candidate_exposes_reference_only_external_evidence_without_changing_beta(self) -> None:
        status = self._status()

        class EmptyStore:
            @staticmethod
            def read_events(*, after: int = 0, limit: int = 500) -> list[object]:
                return []

            @staticmethod
            def list_tasks(limit: int = 100) -> list[object]:
                return []

            @staticmethod
            def latest_quota() -> None:
                return None

        registry = PerformanceRegistry(self.state_root / "workbench")
        registry.refresh(
            EmptyStore(),  # type: ignore[arg-type]
            catalog(),
            ai_frontier_status=status,
        )
        candidate = next(
            item
            for item in registry.calibrate(catalog(), "implementation", "standard")["candidates"]
            if item["model_id"] == "gpt-5.6-luna"
        )
        prior = candidate["quality"]["prior"]

        self.assertEqual(prior["kind"], "conservative-policy-prior")
        self.assertFalse(prior["empirical"])
        self.assertEqual(prior["external_signals"]["source_count"], 0)
        evidence = [
            item for item in candidate["public_evidence"]
            if item["source"] == "ai-frontier"
        ]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["source_id"], "openai/gpt-5.6-luna")
        self.assertTrue(all(item["comparability"]["status"] == "reference_only" for item in evidence))
        self.assertEqual(candidate["quality"]["posterior"]["alpha"], 1.0)
        self.assertEqual(candidate["quality"]["posterior"]["beta"], 2.0)
        self.assertEqual(candidate["runtime"]["attempt_count"], 0)
