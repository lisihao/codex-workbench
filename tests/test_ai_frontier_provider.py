from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_frontier_provider import AIFrontierRegistry, validate_ai_frontier_snapshot
from ai_frontier_provider.cli import _parser, main
from ai_frontier_provider.provider import (
    DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
    HARD_MINIMUM_REFRESH_INTERVAL_SECONDS,
    SOURCE_URLS,
    TERMS_URL,
    USER_AGENT,
)


def payloads() -> dict[str, object]:
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
                "Executor": "anthropic/claude-opus-4-6",
                "Quality": 0.91,
                "Cost": 3.4,
                "Consistency": 0.97,
                "Consistency Std": 0.01,
            },
        ],
        "cost_comparison": [
            {
                "LLMs": "openai/gpt-5.6-luna",
                "Quoted Cost": 2.2,
                "Real Cost": 1.7,
                "Cost Surprise": -0.5,
            },
            {
                "LLMs": "anthropic/claude-opus-4-6",
                "Quoted Cost": 4.2,
                "Real Cost": 3.4,
                "Cost Surprise": 0.8,
            },
        ],
        "model_benchmarks": {
            "openai/gpt-5.6-luna": {
                "categories": [
                    {
                        "id": "coding",
                        "label": "Coding",
                        "quality": 0.84,
                        "cost": 1.8,
                        "benchmarks": [
                            {"key": "terminal-bench", "label": "Terminal-Bench 2.0", "quality": 0.8, "cost": 1.9}
                        ],
                    }
                ]
            }
        },
    }


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, limit: int | None = None) -> bytes:
        return self.payload if limit is None else self.payload[:limit]

    def close(self) -> None:
        self.closed = True


class AIFrontierProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = AIFrontierRegistry(self.root / "state")
        self.receipt = self.root / "personal-use.json"
        self.registry.consent_personal_use(
            self.receipt,
            accepted_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_import_persists_raw_normalized_and_active_in_sqlite(self) -> None:
        result = self.registry.import_payloads(
            payloads(), self.receipt, fetched_at="2026-09-04T12:30:00Z"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "fresh")
        snapshot = self.registry.active()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        validate_ai_frontier_snapshot(snapshot)
        self.assertEqual(snapshot["source"]["remote_timestamp"], None)  # type: ignore[index]
        self.assertEqual(snapshot["source"]["remote_version"], None)  # type: ignore[index]
        self.assertEqual(snapshot["source"]["terms_url"], TERMS_URL)  # type: ignore[index]
        self.assertFalse(snapshot["routing_boundary"]["frontier_oracle_used_for_routing"])  # type: ignore[index]
        luna = next(item for item in snapshot["models"] if item["model_id"] == "gpt-5.6-luna")  # type: ignore[index]
        self.assertEqual(luna["provider"], "codex")
        self.assertEqual(luna["quality"], 0.82)
        self.assertEqual(luna["consistency_semantics"], "stability_not_success_rate")
        self.assertEqual(luna["real_cost"], 1.7)
        self.assertEqual(luna["cost_surprise"], -0.5)
        self.assertEqual(luna["real_cost_semantics"], "publisher_defined_relative_cost")
        self.assertFalse(luna["routing_eligible"])
        coding = next(item for item in snapshot["categories"] if item["category_key"] == "coding")  # type: ignore[index]
        self.assertEqual(coding["quality"], 0.84)
        self.assertEqual(coding["cost"], 1.8)

        status = self.registry.status()
        self.assertEqual(
            status["database"]["row_counts"],  # type: ignore[index]
            {
                "ai_frontier_snapshots": 1,
                "ai_frontier_raw_payloads": 3,
                "ai_frontier_models": 2,
                "ai_frontier_categories": 3,
                "ai_frontier_active": 1,
            },
        )
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.registry.database_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        with sqlite3.connect(self.registry.database_path) as connection:
            raw_names = [row[0] for row in connection.execute("SELECT payload_name FROM ai_frontier_raw_payloads")]
        self.assertEqual(sorted(raw_names), ["cost_comparison", "model_benchmarks:openai/gpt-5.6-luna", "reliability_leaderboard"])

    def test_refresh_is_low_frequency_and_only_fetches_explicit_models(self) -> None:
        values = payloads()
        calls: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> _Response:
            calls.append((request, timeout))
            url = request.full_url  # type: ignore[attr-defined]
            if url == SOURCE_URLS["reliability_leaderboard"]:
                return _Response(values["reliability_leaderboard"])
            if url == SOURCE_URLS["cost_comparison"]:
                return _Response(values["cost_comparison"])
            if url.startswith(SOURCE_URLS["single_model_benchmarks"]):
                return _Response(values["model_benchmarks"]["openai/gpt-5.6-luna"])  # type: ignore[index]
            raise AssertionError(f"unexpected request: {url}")

        with patch("ai_frontier_provider.provider.urlopen", fake_urlopen):
            result = self.registry.refresh(self.receipt, model_source_ids=["openai/gpt-5.6-luna"])

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 3)
        request, timeout = calls[0]
        self.assertEqual(timeout, 15.0)
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)  # type: ignore[attr-defined]
        self.assertEqual(request.get_header("Cookie"), None)  # type: ignore[attr-defined]
        self.assertIn("llm_name=openai%2Fgpt-5.6-luna", calls[-1][0].full_url)  # type: ignore[index,attr-defined]

        with patch("ai_frontier_provider.provider.urlopen") as network:
            deferred = self.registry.refresh(self.receipt)
        network.assert_not_called()
        self.assertTrue(deferred["ok"])
        self.assertFalse(deferred["network_requested"])
        self.assertEqual(deferred["refresh_deferred"], "minimum_refresh_interval")

    def test_refresh_defaults_to_two_aggregate_requests_and_caps_models_before_network(self) -> None:
        values = payloads()
        bare = AIFrontierRegistry(self.root / "bare-state")
        bare_receipt = self.root / "bare-consent.json"
        bare.consent_personal_use(bare_receipt)
        calls: list[str] = []

        def aggregate_only(request: object, timeout: float) -> _Response:
            url = request.full_url  # type: ignore[attr-defined]
            calls.append(url)
            if url == SOURCE_URLS["reliability_leaderboard"]:
                return _Response(values["reliability_leaderboard"])
            if url == SOURCE_URLS["cost_comparison"]:
                return _Response(values["cost_comparison"])
            raise AssertionError(f"unexpected request: {url}")

        with patch("ai_frontier_provider.provider.urlopen", aggregate_only):
            result = bare.refresh(bare_receipt)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [SOURCE_URLS["reliability_leaderboard"], SOURCE_URLS["cost_comparison"]])

        with patch("ai_frontier_provider.provider.urlopen") as network:
            capped = self.registry.refresh(
                self.receipt,
                model_source_ids=[f"openai/model-{index}" for index in range(9)],
            )
        network.assert_not_called()
        self.assertFalse(capped["ok"])
        self.assertIn("at most 8", capped["last_error"])

    def test_absent_requested_model_skips_detail_but_persists_aggregates(self) -> None:
        values = payloads()
        bare = AIFrontierRegistry(self.root / "absent-state")
        bare_receipt = self.root / "absent-consent.json"
        bare.consent_personal_use(bare_receipt)
        calls: list[str] = []

        def aggregate_only(request: object, timeout: float) -> _Response:
            url = request.full_url  # type: ignore[attr-defined]
            calls.append(url)
            if url == SOURCE_URLS["reliability_leaderboard"]:
                return _Response(values["reliability_leaderboard"])
            if url == SOURCE_URLS["cost_comparison"]:
                return _Response(values["cost_comparison"])
            raise AssertionError(f"unexpected detail request: {url}")

        with patch("ai_frontier_provider.provider.urlopen", aggregate_only):
            result = bare.refresh(bare_receipt, model_source_ids=["openai/gpt-5.6-sol"])

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [SOURCE_URLS["reliability_leaderboard"], SOURCE_URLS["cost_comparison"]])
        expected_projection = {
            "requested_source_ids": ["openai/gpt-5.6-sol"],
            "selected_source_ids": [],
            "skipped_source_ids": ["openai/gpt-5.6-sol"],
        }
        self.assertEqual(result["detail_request"], expected_projection)
        snapshot = bare.active()
        assert snapshot is not None
        self.assertEqual(snapshot["detail_request"], expected_projection)
        self.assertEqual(
            bare.status()["database"]["row_counts"]["ai_frontier_raw_payloads"],  # type: ignore[index]
            2,
        )

    def test_refresh_without_personal_consent_is_disabled_before_network(self) -> None:
        unavailable = self.registry.status()
        self.assertEqual(unavailable["authorization_status"], "unauthorized")
        self.assertEqual(unavailable["policy_state"], "disabled_by_policy")
        with patch("ai_frontier_provider.provider.urlopen") as network:
            result = self.registry.refresh(self.root / "missing-consent.json")

        network.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["authorization_status"], "unauthorized")
        self.assertEqual(result["policy_state"], "disabled_by_policy")
        self.assertFalse(result["network_requested"])

    def test_network_failure_keeps_last_known_good(self) -> None:
        initial = self.registry.import_payloads(
            payloads(), self.receipt, fetched_at="2026-09-01T12:00:00Z"
        )
        first_id = initial["snapshot_id"]

        with patch("ai_frontier_provider.provider.urlopen", side_effect=OSError("offline")):
            failed = self.registry.refresh(self.receipt)

        self.assertFalse(failed["ok"])
        self.assertTrue(failed["network_requested"])
        self.assertEqual(failed["snapshot_id"], first_id)
        self.assertEqual(self.registry.active()["snapshot_id"], first_id)  # type: ignore[index]

    def test_invalid_duplicate_and_nonfinite_payloads_do_not_replace_lkg(self) -> None:
        initial = self.registry.import_payloads(payloads(), self.receipt)
        initial_id = initial["snapshot_id"]

        duplicate = payloads()
        duplicate["reliability/leaderboard"] = duplicate["reliability_leaderboard"]
        duplicate_result = self.registry.import_payloads(duplicate, self.receipt)
        self.assertFalse(duplicate_result["ok"])
        self.assertEqual(duplicate_result["snapshot_id"], initial_id)

        nonfinite = payloads()
        nonfinite["reliability_leaderboard"][0]["Quality"] = float("nan")  # type: ignore[index]
        nonfinite_result = self.registry.import_payloads(nonfinite, self.receipt)
        self.assertFalse(nonfinite_result["ok"])
        self.assertEqual(nonfinite_result["snapshot_id"], initial_id)

        repeated = payloads()
        repeated["reliability_leaderboard"].append(dict(repeated["reliability_leaderboard"][0]))  # type: ignore[index]
        repeated_result = self.registry.import_payloads(repeated, self.receipt)
        self.assertFalse(repeated_result["ok"])
        self.assertEqual(repeated_result["snapshot_id"], initial_id)

    def test_database_recovers_active_snapshot_after_process_restart(self) -> None:
        imported = self.registry.import_payloads(payloads(), self.receipt)
        restarted = AIFrontierRegistry(self.root / "state")

        self.assertEqual(restarted.active()["snapshot_id"], imported["snapshot_id"])  # type: ignore[index]
        self.assertEqual(
            restarted.load_generation(str(imported["snapshot_id"])), restarted.active()
        )

    def test_cli_consent_requires_explicit_personal_use_and_default_is_72_hours(self) -> None:
        state_root = self.root / "cli-state"
        output = io.StringIO()
        with redirect_stdout(output):
            rejected_exit = main(["--state-root", str(state_root), "consent"])
        rejected = json.loads(output.getvalue())
        self.assertEqual(rejected_exit, 2)
        self.assertFalse(rejected["ok"])
        self.assertIn("--personal-use", rejected["error"])

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--state-root", str(state_root), "consent", "--personal-use"])
        consent = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(consent["ok"])
        self.assertTrue(consent["receipt"]["not_official_authorization"])
        self.assertEqual(consent["receipt"]["terms_url"], TERMS_URL)
        self.assertEqual(stat.S_IMODE((state_root / "authorization.json").stat().st_mode), 0o600)

        args = _parser().parse_args(
            ["--state-root", str(state_root), "refresh", "--authorization-file", str(self.receipt)]
        )
        self.assertEqual(args.minimum_refresh_interval_seconds, DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS)
        self.assertGreaterEqual(args.minimum_refresh_interval_seconds, HARD_MINIMUM_REFRESH_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
