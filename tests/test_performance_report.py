from __future__ import annotations

import csv
from datetime import datetime
import io
import unittest

from codex_workbench.performance_report import (
    build_model_performance_report,
    report_to_csv,
    report_to_html,
)


def _inputs() -> dict[str, object]:
    catalog = {
        "catalog_id": "catalog-report-test",
        "observed_at": "2026-09-04T12:00:00Z",
        "models": [
            {
                "provider": "codex",
                "model_id": "gpt-6-astra",
                "model_family": "unknown",
                "routable": False,
                "reasoning": {"supported_efforts": ["medium", "high"]},
            },
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "model_family": "luna",
                "routable": True,
                "reasoning": {"supported_efforts": ["high", "max"]},
            },
            {
                "provider": "codex",
                "model_id": "model<unsafe>",
                "model_family": "unknown",
                "routable": False,
                "reasoning": {"supported_efforts": []},
            },
        ],
    }
    baseline = {
        "schema_version": 1,
        "baseline_id": "baseline-report-test",
        "records": [
            {
                "record_id": "baseline-luna",
                "source_url": "https://example.test/benchmark",
                "benchmark": "Example Bench",
                "benchmark_version": "v1",
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "model_family": "luna",
                "score": 0.8,
                "score_kind": "accuracy",
                "effective_sample_strength": 1,
                "provenance": "independent",
            },
            {
                "record_id": "baseline-unknown",
                "source_url": "https://example.test/benchmark?v=secret",
                "benchmark": "Example Bench",
                "benchmark_version": "v2",
                "provider": "codex",
                "model_id": "gpt-5.6-sol",
                "model_family": "sol",
                "score": None,
                "score_kind": "not_published",
                "effective_sample_strength": 0,
                "provenance": "vendor_report",
            },
        ],
    }
    radar_status = {
        "state": "fresh",
        "snapshot_id": "codex-radar-test",
        "active": {
            "snapshot_id": "codex-radar-test",
            "digest": "r" * 64,
            "fetched_at": "2026-09-04T12:10:00Z",
            "source_updated_at": "2026-09-04T12:09:59.123Z",
            "source_urls": {"intelligence_efficiency": "https://radar.test/data.json"},
            "models": [
                {
                    "provider": "codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "routing_eligible": True,
                    "iq": 101.5,
                    "pass_rate": 0.75,
                    "sample_count": 12,
                    "avg_cost_usd": 1.25,
                    "avg_runtime_seconds": 42.0,
                },
                {
                    "provider": "unknown",
                    "model": "unroutable-external",
                    "reasoning_effort": "high",
                    "routing_eligible": False,
                    "iq": None,
                    "pass_rate": None,
                    "sample_count": None,
                    "avg_cost_usd": None,
                    "avg_runtime_seconds": None,
                },
            ],
        },
    }
    ai_frontier_status = {
        "state": "fresh",
        "snapshot_id": "ai-frontier-test",
        "snapshot": {
            "snapshot_id": "ai-frontier-test",
            "digest": "a" * 64,
            "fetched_at": "2026-09-04T12:20:00Z",
            "source_urls": {
                "reliability": "https://frontier.test/reliability",
                "cost_comparison": "https://frontier.test/cost",
            },
            "models": [
                {
                    "source_id": "openai/gpt-5.6-luna",
                    "provider": "codex",
                    "model_id": "gpt-5.6-luna",
                    "quality": 0.82,
                    "consistency": 0.94,
                    "consistency_std": 0.03,
                    "real_cost": 1.7,
                    "quoted_cost": 2.2,
                    "cost_surprise": -0.2,
                    "routing_eligible": False,
                }
            ],
            "categories": [
                {
                    "source_id": "openai/gpt-5.6-luna",
                    "model_id": "gpt-5.6-luna",
                    "category_key": "coding",
                    "quality": 0.84,
                },
                {
                    "source_id": "openai/gpt-5.6-luna",
                    "model_id": "gpt-5.6-luna",
                    "category_key": "cost-comparison",
                    "quality": None,
                    "cost": 1.7,
                    "quoted_cost": 2.2,
                    "cost_surprise": -0.2,
                },
            ],
        },
    }
    performance_snapshot = {
        "snapshot_id": "performance-test",
        "digest": "p" * 64,
        "metrics": [
            {
                "key": {
                    "provider": "codex",
                    "model_id": "gpt-5.6-luna",
                    "agent_name": "codex",
                    "agent_version": "unattested",
                    "reasoning_effort": "max",
                    "task_type": "implementation",
                    "complexity": "standard",
                },
                "runtime": {
                    "attempt_count": 1,
                    "first_pass": {"rate": None},
                    "final_acceptance": {"rate": None},
                    "duration_seconds": {"mean": 31, "p50": 31, "sample_count": 1},
                    "quality_calibration": {
                        "sample_count": 0,
                        "successes": 0,
                        "failures": 0,
                        "unresolved": 1,
                    },
                },
            }
        ],
    }
    return {
        "catalog": catalog,
        "baseline": baseline,
        "radar_status": radar_status,
        "ai_frontier_status": ai_frontier_status,
        "performance_snapshot": performance_snapshot,
    }


class PerformanceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = _inputs()
        self.report = build_model_performance_report(**self.values)  # type: ignore[arg-type]

    def test_complete_row_counts_keep_every_source_observation(self) -> None:
        self.assertEqual(self.report["counts"]["by_source"], {
            "ai_frontier": 3,
            "baseline": 2,
            "catalog": 2,
            "local_runtime": 1,
            "radar": 2,
        })
        self.assertEqual(self.report["counts"]["total_observations"], 10)
        self.assertEqual(
            len([row for row in self.report["observations"] if row["observation_type"] == "radar_model_effort"]),
            2,
        )
        self.assertEqual(
            len([row for row in self.report["observations"] if row["source"] == "ai_frontier"]),
            3,
        )

    def test_catalog_only_astra_is_null_and_does_not_inherit_luna_data(self) -> None:
        astra = next(
            row
            for row in self.report["observations"]
            if row["model_id"] == "gpt-6-astra"
        )
        self.assertEqual(astra["observation_type"], "catalog_only")
        self.assertIsNone(astra["quality_fraction"])
        self.assertIsNone(astra["cost_usd"])
        self.assertEqual(astra["missing_data"], ["no_performance_observation"])

    def test_units_keep_radar_usd_separate_from_frontier_relative_cost(self) -> None:
        radar = next(row for row in self.report["observations"] if row["source"] == "radar" and row["model_id"] == "gpt-5.6-luna")
        frontier = next(row for row in self.report["observations"] if row["observation_type"] == "ai_frontier_model")
        self.assertEqual(radar["cost_usd"], 1.25)
        self.assertEqual(radar["units"]["cost_usd"], "USD per task")
        self.assertEqual(frontier["publisher_relative_cost"], 1.7)
        self.assertIsNone(frontier["cost_usd"])
        self.assertIn("publisher-relative", frontier["units"]["publisher_relative_cost"])
        self.assertNotIn("USD", frontier["units"]["publisher_relative_cost"])

    def test_runtime_unattested_bucket_does_not_claim_observed_quality(self) -> None:
        runtime = next(row for row in self.report["observations"] if row["source"] == "local_runtime")
        self.assertEqual(runtime["runtime_attempt_count"], 1)
        self.assertEqual(runtime["runtime_quality_sample_count"], 0)
        self.assertIsNone(runtime["first_pass_rate"])
        self.assertIsNone(runtime["final_acceptance_rate"])
        self.assertEqual(runtime["quality_status"], "unavailable")
        self.assertIn("agent_version_unattested", runtime["data_quality_flags"])
        self.assertIn("quality_denominator_zero", runtime["data_quality_flags"])

    def test_csv_and_html_have_explicit_units_dates_and_escaped_text(self) -> None:
        rows = list(csv.DictReader(io.StringIO(report_to_csv(self.report))))
        astra = next(row for row in rows if row["model_id"] == "gpt-6-astra")
        self.assertEqual(astra["cost_usd"], "N/A")
        self.assertEqual(astra["cost_usd_unit"], "USD per task")
        radar = next(row for row in rows if row["source"] == "radar" and row["model_id"] == "gpt-5.6-luna")
        self.assertEqual(radar["latency_unit"], "seconds per task")
        self.assertEqual(radar["captured_at"], "2026-09-04T12:10:00Z")
        self.assertNotEqual(radar["generated_at"], radar["captured_at"])
        datetime.fromisoformat(radar["generated_at"].replace("Z", "+00:00"))

        html = report_to_html(self.report)
        self.assertIn("<html lang=\"zh-CN\">", html)
        self.assertIn("&lt;unsafe&gt;", html)
        self.assertNotIn("model<unsafe>", html)
        self.assertIn("id=\"search\"", html)
        self.assertIn("id=\"source-filter\"", html)
        self.assertIn("id=\"model-filter\"", html)
        self.assertIn("aria-controls=\"ledger-table\"", html)


if __name__ == "__main__":
    unittest.main()
