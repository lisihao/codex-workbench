from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_radar_provider import RadarRegistry, validate_radar_snapshot
from codex_radar_provider.cli import main


def authorization_receipt() -> dict[str, object]:
    return {
        "schema": "codex-radar-provider-authorization",
        "version": 1,
        "provider": "codex-radar",
        "status": "authorized",
        "scope": ["model-quality-json"],
        "attribution": "数据来自 Codex 雷达 codexradar.com",
    }


def payloads(*, source_time: str = "2026-09-04T12:00:00Z") -> dict[str, object]:
    return {
        "current": {
            "schema_version": "2.0",
            "checked_at": source_time,
            "model_iq": {
                "updated_at": source_time,
                "latest": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "passed": 7,
                    "valid_tasks": 10,
                    "score": 95.5,
                    "average_cost_usd": 2.4,
                    "average_task_seconds": 300,
                },
                "comparisons": {
                    "unknown": {
                        "model": "gpt-9.9-unknown",
                        "reasoning_effort": "high",
                        "latest": {
                            "passed": 4,
                            "tasks": 10,
                            "score": 81.2,
                        },
                    }
                },
            },
        },
        "intelligence_efficiency": {
            "schema": 2,
            "source_updated_at": source_time,
            "points": [
                {
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                    "iq": 101.25,
                    "passed": 9,
                    "valid_tasks": 12,
                    "average_price_usd": 3.1,
                    "average_minutes": 7.0,
                    "latest_graded_at": source_time,
                }
            ],
        },
        "model_ratings": {
            "ok": True,
            "updated_at": source_time,
            "models": [
                {
                    "id": "gpt-5.6-luna-max",
                    "label": "gpt-5.6-luna max",
                    "average": 8.8,
                    "count": 9,
                }
            ],
        },
        "radar_insights": {
            "schema": 1,
            "generated_at": source_time,
            "source_updated_at": source_time,
            "station_recs": [
                {
                    "id": "daily-development",
                    "description": "综合成本",
                    "models": [
                        {
                            "model": "gpt-5.6-luna",
                            "effort": "max",
                            "current_iq": "101.25",
                            "average_price_usd": "3.1",
                            "average_minutes": "7",
                        }
                    ],
                }
            ],
            "alerts": {
                "rule": "only degradation",
                "alerts": [
                    {
                        "model": "gpt-5.6-luna",
                        "effort": "max",
                        "iq": 101.25,
                        "drop_24h": 2.5,
                    }
                ],
            },
        },
    }


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


class RadarProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = RadarRegistry(self.root / "state")
        self.receipt = self.root / "authorization.json"
        self.receipt.write_text(json.dumps(authorization_receipt()), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_refresh_without_authorization_does_not_request_network(self) -> None:
        calls: list[object] = []

        def unexpected(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            raise AssertionError("network must not be called without authorization")

        with patch("codex_radar_provider.provider.urlopen", unexpected):
            result = self.registry.refresh(self.root / "missing-authorization.json")

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["authorization_status"], "unauthorized")
        self.assertEqual(calls, [])

    def test_import_persists_redacted_raw_and_normalized_generation(self) -> None:
        incoming = payloads()
        incoming["current"]["access_token"] = "never-persist-this"  # type: ignore[index]

        result = self.registry.import_payloads(
            incoming,
            self.receipt,
            fetched_at="2026-09-04T12:30:00Z",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "fresh")
        snapshot = self.registry.active()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        validate_radar_snapshot(snapshot)
        self.assertEqual(snapshot["upstream"]["version"], "0.1.69")  # type: ignore[index]
        self.assertEqual(snapshot["source_urls"]["current"], "https://codexradar.com/current.json")  # type: ignore[index]
        luna = next(item for item in snapshot["models"] if item["model"] == "gpt-5.6-luna")  # type: ignore[index]
        self.assertEqual(luna["pass_rate"], 0.75)
        self.assertEqual(luna["iq"], 101.25)
        self.assertEqual(luna["sample_count"], 12)
        self.assertEqual(luna["avg_cost_usd"], 3.1)
        self.assertEqual(luna["avg_runtime_seconds"], 420.0)
        self.assertEqual(luna["community_rating"], {"average": 8.8, "sample_count": 9})
        self.assertEqual(snapshot["insights"]["recommendations"][0]["items"][0]["iq"], 101.25)  # type: ignore[index]

        snapshot_id = str(snapshot["snapshot_id"])
        raw = json.loads((self.root / "state" / "raw" / f"{snapshot_id}.json").read_text())
        self.assertEqual(raw["payloads"]["current"]["access_token"], "<redacted>")
        self.assertNotIn("never-persist-this", json.dumps(raw))
        self.assertEqual(self.registry.load_generation(snapshot_id), snapshot)

    def test_unknown_model_is_preserved_but_not_routing_eligible(self) -> None:
        self.registry.import_payloads(payloads(), self.receipt)
        snapshot = self.registry.active()
        assert snapshot is not None
        unknown = next(item for item in snapshot["models"] if item["model"] == "gpt-9.9-unknown")  # type: ignore[index]
        self.assertFalse(unknown["routing_eligible"])
        self.assertEqual(unknown["pass_rate"], 0.4)
        self.assertEqual(unknown["iq"], 81.2)

    def test_invalid_schema_retains_last_known_good_cache(self) -> None:
        initial = self.registry.import_payloads(payloads(), self.receipt)
        first_id = initial["snapshot_id"]
        invalid = payloads()
        invalid["radar_insights"]["schema"] = 2  # type: ignore[index]

        result = self.registry.import_payloads(invalid, self.receipt)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "fresh")
        self.assertEqual(result["snapshot_id"], first_id)
        self.assertIn("schema", result["last_error"])

    def test_timestamp_regression_retains_last_known_good_cache(self) -> None:
        initial = self.registry.import_payloads(
            payloads(source_time="2026-09-04T12:00:00Z"),
            self.receipt,
        )
        older = self.registry.import_payloads(
            payloads(source_time="2026-09-03T12:00:00Z"),
            self.receipt,
        )

        self.assertFalse(older["ok"])
        self.assertEqual(older["snapshot_id"], initial["snapshot_id"])
        self.assertIn("timestamp regressed", older["last_error"])

    def test_status_marks_expired_cache_stale(self) -> None:
        self.registry.import_payloads(
            payloads(),
            self.receipt,
            fetched_at="2026-09-04T12:00:00Z",
            stale_after_seconds=60,
        )

        status = self.registry.status(datetime(2026, 9, 4, 12, 2, tzinfo=UTC))

        self.assertFalse(status["ok"])
        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["cache_status"], "stale-cache")
        self.assertEqual(status["age_seconds"], 120)

    def test_import_accepts_datetime_fetched_at(self) -> None:
        result = self.registry.import_payloads(
            payloads(),
            self.receipt,
            fetched_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["fetched_at"], "2026-09-04T12:00:00Z")

    def test_receipt_with_secret_is_rejected_without_network(self) -> None:
        secret_receipt = self.root / "secret-receipt.json"
        unsafe = authorization_receipt()
        unsafe["access_token"] = "not-allowed"
        secret_receipt.write_text(json.dumps(unsafe), encoding="utf-8")
        with patch("codex_radar_provider.provider.urlopen") as network:
            result = self.registry.refresh(secret_receipt)
        network.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("secret", result["last_error"])

    def test_authorization_status_is_local_and_secret_safe(self) -> None:
        with patch("codex_radar_provider.provider.urlopen") as network:
            authorized = self.registry.authorization_status(self.receipt)
        network.assert_not_called()
        self.assertTrue(authorized["ok"])
        self.assertEqual(authorized["status"], "authorized")
        self.assertEqual(authorized["receipt"]["scope"], ["model-quality-json"])  # type: ignore[index]

        unsafe_receipt = self.root / "unsafe.json"
        unsafe = authorization_receipt()
        unsafe["api_key"] = "not-safe"
        unsafe_receipt.write_text(json.dumps(unsafe), encoding="utf-8")
        unauthorized = self.registry.authorization_status(unsafe_receipt)
        self.assertFalse(unauthorized["ok"])
        self.assertEqual(unauthorized["status"], "unauthorized")
        self.assertNotIn("not-safe", json.dumps(unauthorized))

    def test_refresh_uses_explicit_api_key_header_without_persisting_it(self) -> None:
        values = payloads()
        seen: list[object] = []

        def fake_urlopen(request: object, timeout: float) -> _Response:
            seen.append((request, timeout))
            full_url = request.full_url  # type: ignore[attr-defined]
            endpoint = {
                "https://codexradar.com/current.json": "current",
                "https://codexradar.com/data/intelligence-efficiency.json": "intelligence_efficiency",
                "https://codexradar.com/api/model-ratings": "model_ratings",
                "https://api.codexradar.com/api/v1/radar-insights": "radar_insights",
            }[full_url]
            return _Response(values[endpoint])

        with patch.dict(os.environ, {"RADAR_TEST_KEY": "top-secret"}):
            with patch("codex_radar_provider.provider.urlopen", fake_urlopen):
                result = self.registry.refresh(
                    self.receipt,
                    api_key_env="RADAR_TEST_KEY",
                    api_key_header="X-Radar-Key",
                    minimum_refresh_interval_seconds=0,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(seen), 4)
        request, timeout = seen[0]
        self.assertEqual(timeout, 15.0)
        self.assertEqual(request.get_header("X-radar-key"), "top-secret")  # type: ignore[attr-defined]
        state_contents = "".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "state").rglob("*.json")
        )
        self.assertNotIn("top-secret", state_contents)

    def test_cli_import_and_status_emit_stable_json(self) -> None:
        payload_file = self.root / "payloads.json"
        payload_file.write_text(json.dumps(payloads()), encoding="utf-8")
        state_root = self.root / "cli-state"

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--state-root",
                    str(state_root),
                    "import",
                    "--authorization-file",
                    str(self.receipt),
                    "--payloads-json",
                    str(payload_file),
                    "--fetched-at",
                    "2026-09-04T12:30:00Z",
                ]
            )
        imported = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["snapshot"]["schema_version"], 1)

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--state-root", str(state_root), "status"])
        status = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(status["state"], "fresh")
        self.assertEqual(status["snapshot_id"], imported["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
