from __future__ import annotations

import unittest

from codex_workbench.public_evidence_ranking import (
    canonical_cohort_key,
    rank_comparable_public_evidence,
)


def evidence(
    *,
    source: str,
    model: str,
    value: float,
    sample_count: int | None,
    benchmark: str = "coding-bench",
    benchmark_version: str = "2026-09",
    lineage_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": source,
        "provider": "codex",
        "model_id": model,
        "canonical_model_id": model,
        "reasoning_effort": "max",
        "metric_kind": "pass_rate",
        "score_kind": "resolved_rate",
        "value": value,
        "unit": "proportion",
        "sample_count": sample_count,
        "observed_at": "2026-09-04T00:00:00Z",
        "benchmark": benchmark,
        "benchmark_version": benchmark_version,
        "harness": "exact-harness-v1",
        "domain": "coding",
        "task_type": "implementation",
        "lineage_id": lineage_id or f"{source}:{model}:{benchmark}",
        "correlation_group": f"{source}:{benchmark}",
        "comparability": {"status": "comparable", "missing_conditions": []},
    }
    record["cohort_key"] = canonical_cohort_key(record)
    return record


def candidate(candidate_id: str, *records: dict[str, object]) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "provider": "codex",
        "model": candidate_id,
        "reasoning_effort": "max",
        "public_evidence": list(records),
    }


class PublicEvidenceRankingTests(unittest.TestCase):
    def test_same_cohort_comparable_evidence_creates_secondary_preference(self) -> None:
        result = rank_comparable_public_evidence(
            [
                candidate("gpt-5.6-luna", evidence(source="radar", model="gpt-5.6-luna", value=0.70, sample_count=200)),
                candidate("gpt-5.6-terra", evidence(source="radar", model="gpt-5.6-terra", value=0.95, sample_count=200)),
            ],
            task_type="implementation",
        )

        self.assertEqual(result["status"], "used")
        self.assertEqual(result["preference_ranks"]["gpt-5.6-terra"], 0)
        self.assertEqual(result["preference_ranks"]["gpt-5.6-luna"], 1)
        self.assertEqual(
            result["candidate_summaries"]["gpt-5.6-terra"]["supporting_sources"],
            [{"source": "radar", "cohort_key": canonical_cohort_key(evidence(source="radar", model="gpt-5.6-terra", value=0.95, sample_count=200))}],
        )

    def test_conflict_abstains_and_duplicate_lineage_is_one_vote(self) -> None:
        terra_a = evidence(
            source="source-a",
            model="gpt-5.6-terra",
            value=0.95,
            sample_count=200,
            lineage_id="shared-terra-a",
        )
        result = rank_comparable_public_evidence(
            [
                candidate(
                    "gpt-5.6-luna",
                    evidence(source="source-a", model="gpt-5.6-luna", value=0.70, sample_count=200, lineage_id="shared-luna-a"),
                    evidence(source="source-b", model="gpt-5.6-luna", value=0.95, sample_count=200),
                ),
                candidate(
                    "gpt-5.6-terra",
                    terra_a,
                    dict(terra_a),
                    evidence(source="source-b", model="gpt-5.6-terra", value=0.70, sample_count=200),
                ),
            ],
            task_type="implementation",
        )

        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["preference_ranks"], {"gpt-5.6-luna": 0, "gpt-5.6-terra": 0})
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(
            {entry["source"] for entry in result["conflicts"][0]["sources"]},
            {"source-a", "source-b"},
        )

    def test_small_or_denominator_free_pass_rate_never_fabricates_confidence(self) -> None:
        for sample_count, expected_reason in (
            (1, "pass-rate uncertainty intervals overlap"),
            (None, "pass-rate denominator is missing; confidence is not fabricated"),
        ):
            with self.subTest(sample_count=sample_count):
                result = rank_comparable_public_evidence(
                    [
                        candidate("gpt-5.6-luna", evidence(source="radar", model="gpt-5.6-luna", value=0.80, sample_count=200)),
                        candidate("gpt-5.6-terra", evidence(source="radar", model="gpt-5.6-terra", value=0.99, sample_count=sample_count)),
                    ],
                    task_type="implementation",
                )

                self.assertEqual(result["status"], "abstained")
                reasons = {
                    entry["reason"]
                    for summary in result["candidate_summaries"].values()
                    for entry in summary["abstained_sources"]
                }
                self.assertIn(expected_reason, reasons)

    def test_gpt55_high_xhigh_and_ungraded_frontier_rows_do_not_mix(self) -> None:
        gpt55 = "gpt-5.5"
        terra = "gpt-5.6-terra"
        radar_high = evidence(source="codex-radar", model=gpt55, value=0.99, sample_count=200)
        radar_high["reasoning_effort"] = "high"
        radar_high["cohort_key"] = canonical_cohort_key(radar_high)
        frontier = evidence(source="ai-frontier", model=gpt55, value=0.99, sample_count=None)
        frontier["reasoning_effort"] = "xhigh"
        frontier["comparability"] = {"status": "reference_only", "missing_conditions": ["exact harness"]}
        frontier["cohort_key"] = canonical_cohort_key(frontier)
        terra_record = evidence(source="codex-radar", model=terra, value=0.70, sample_count=200)
        terra_record["reasoning_effort"] = "xhigh"
        terra_record["cohort_key"] = canonical_cohort_key(terra_record)

        result = rank_comparable_public_evidence(
            [
                {
                    "candidate_id": gpt55,
                    "provider": "codex",
                    "model": gpt55,
                    "reasoning_effort": "xhigh",
                    "public_evidence": [radar_high, frontier],
                },
                {
                    "candidate_id": terra,
                    "provider": "codex",
                    "model": terra,
                    "reasoning_effort": "xhigh",
                    "public_evidence": [terra_record],
                },
            ],
            task_type="implementation",
        )

        self.assertEqual(result["status"], "abstained")
        reasons = {
            entry["reason"]
            for entry in result["candidate_summaries"][gpt55]["abstained_sources"]
        }
        self.assertIn("public evidence does not match the exact reasoning effort/task type", reasons)
        self.assertIn("public evidence is reference_only or lacks declared comparability", reasons)
        self.assertEqual(result["preference_ranks"], {gpt55: 0, terra: 0})

    def test_cycle_abstains_instead_of_creating_a_nontransitive_sort(self) -> None:
        alpha = "gpt-5.6-luna"
        beta = "gpt-5.6-terra"
        gamma = "gpt-5.6-sol"
        result = rank_comparable_public_evidence(
            [
                candidate(
                    alpha,
                    evidence(source="source-a", model=alpha, value=0.95, sample_count=200, benchmark="bench-a"),
                    evidence(source="source-c", model=alpha, value=0.70, sample_count=200, benchmark="bench-c"),
                ),
                candidate(
                    beta,
                    evidence(source="source-a", model=beta, value=0.70, sample_count=200, benchmark="bench-a"),
                    evidence(source="source-b", model=beta, value=0.95, sample_count=200, benchmark="bench-b"),
                ),
                candidate(
                    gamma,
                    evidence(source="source-b", model=gamma, value=0.70, sample_count=200, benchmark="bench-b"),
                    evidence(source="source-c", model=gamma, value=0.95, sample_count=200, benchmark="bench-c"),
                ),
            ],
            task_type="implementation",
        )

        self.assertEqual(result["status"], "abstained")
        self.assertIn("cycle", result["reason"])
        self.assertEqual(result["preference_ranks"], {alpha: 0, beta: 0, gamma: 0})


if __name__ == "__main__":
    unittest.main()
