from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_radar_provider import RadarRegistry
from codex_workbench.performance import PerformanceRegistry
from codex_workbench.radar import WorkbenchRadar, radar_prior_records


def _authorization(status: str = "authorized") -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": "codex-radar-provider-authorization",
        "version": 1,
        "provider": "codex-radar",
        "status": status,
        "scope": ["model-quality-json"],
        "attribution": "数据来自 Codex 雷达 codexradar.com",
    }
    if status == "consented":
        receipt.update(
            {
                "basis": "local_operator_consent",
                "scope": ["public-json"],
                "accepted_at": "2026-09-04T11:00:00Z",
            }
        )
    return receipt


def _payloads(*, passed: int = 9, valid_tasks: int = 12) -> dict[str, object]:
    observed_at = "2026-09-04T12:00:00Z"
    return {
        "current": {
            "schema_version": 2,
            "checked_at": observed_at,
            "model_iq": {
                "latest": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "passed": passed,
                    "valid_tasks": valid_tasks,
                    "iq": 101.25,
                },
                "comparisons": {
                    "unknown": {
                        "model": "gpt-9.9-unknown",
                        "reasoning_effort": "high",
                        "latest": {"passed": 9, "valid_tasks": 10, "iq": 140},
                    }
                },
            },
        },
        "intelligence_efficiency": {
            "schema": 2,
            "source_updated_at": observed_at,
            "points": [
                {
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                    "iq": 101.25,
                    "passed": passed,
                    "valid_tasks": valid_tasks,
                }
            ],
        },
        "model_ratings": {"updated_at": observed_at, "models": []},
        "radar_insights": {
            "schema": 1,
            "generated_at": observed_at,
            "source_updated_at": observed_at,
            "recommendations": [],
            "alerts": [],
        },
    }


def _catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "model_family": "luna",
                "routable": True,
                "reasoning": {
                    "preferred_effort": "max",
                    "supported_efforts": ["high", "max"],
                },
            },
            {
                "provider": "codex",
                "model_id": "gpt-9.9-unknown",
                "model_family": "unknown",
                "routable": False,
                "reasoning": {"supported_efforts": ["high"]},
            },
        ]
    }


class WorkbenchRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state_root = root / "radar"
        self.authorization = root / "authorization.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(
        self,
        fetched_at: str = "2026-09-04T12:00:00Z",
        *,
        stale_after_seconds: int = 7 * 24 * 60 * 60,
        passed: int = 9,
        valid_tasks: int = 12,
        authorization_status: str = "authorized",
    ) -> None:
        self.authorization.write_text(
            json.dumps(_authorization(authorization_status)), encoding="utf-8"
        )
        result = RadarRegistry(self.state_root).import_payloads(
            _payloads(passed=passed, valid_tasks=valid_tasks),
            self.authorization,
            fetched_at=fetched_at,
            stale_after_seconds=stale_after_seconds,
        )
        self.assertTrue(result["generation_created"])

    def test_no_cache_and_no_receipt_is_unauthorized_without_network(self) -> None:
        status = WorkbenchRadar(self.state_root, self.authorization).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["state"], "unauthorized")
        self.assertFalse(status["routing_prior_eligible"])
        self.assertFalse(status["offline_cache_available"])
        self.assertEqual(status["database"]["backend"], "sqlite")

    def test_cache_has_fresh_stale_and_expired_workbench_boundaries(self) -> None:
        self._import()
        radar = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=60,
            expire_after_seconds=180,
        )

        fresh = radar.status(now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC))
        stale = radar.status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))
        expired = radar.status(now=datetime(2026, 9, 4, 12, 4, tzinfo=UTC))

        self.assertEqual((fresh["state"], fresh["transfer_multiplier"]), ("fresh", 1.0))
        self.assertEqual((stale["state"], stale["transfer_multiplier"]), ("stale", 0.25))
        self.assertTrue(stale["offline_cache_available"])
        self.assertEqual((expired["state"], expired["transfer_multiplier"]), ("expired", 0.0))
        self.assertFalse(expired["routing_prior_eligible"])

    def test_provider_freshness_is_never_weakened_by_workbench_policy(self) -> None:
        self._import(stale_after_seconds=60)
        status = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=7 * 24 * 60 * 60,
            expire_after_seconds=31 * 24 * 60 * 60,
        ).status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))

        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["stale_after_seconds"], 60)
        self.assertEqual(status["provider_stale_after_seconds"], 60)
        self.assertEqual(status["transfer_multiplier"], 0.25)

    def test_current_authorization_is_required_even_when_cache_exists(self) -> None:
        self._import()
        self.authorization.unlink()

        status = WorkbenchRadar(self.state_root, self.authorization).status(
            now=datetime(2026, 9, 4, 12, 1, tzinfo=UTC)
        )

        self.assertEqual(status["state"], "unauthorized")
        self.assertTrue(status["offline_cache_available"])
        self.assertFalse(status["routing_prior_eligible"])
        self.assertEqual(radar_prior_records(status, _catalog()), [])

    def test_only_exact_routable_model_and_effort_becomes_a_weak_prior(self) -> None:
        self._import()
        status = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=24 * 60 * 60,
            expire_after_seconds=2 * 24 * 60 * 60,
        ).status(now=datetime(2026, 9, 4, 12, 1, tzinfo=UTC))

        records = radar_prior_records(status, _catalog())

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["model_id"], "gpt-5.6-luna")
        self.assertEqual(record["reasoning_effort"], "max")
        self.assertEqual(record["score"], 0.75)
        self.assertEqual(record["effective_sample_strength"], 0.6)
        self.assertEqual(record["iq_metadata"], 101.25)
        self.assertNotEqual(record["score"], record["iq_metadata"])
        self.assertEqual(record["provenance"], "community_observation")

    def test_consented_cache_is_eligible_and_preserves_consent_status(self) -> None:
        self._import(authorization_status="consented")
        status = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=24 * 60 * 60,
            expire_after_seconds=2 * 24 * 60 * 60,
        ).status(now=datetime(2026, 9, 4, 12, 1, tzinfo=UTC))

        records = radar_prior_records(status, _catalog())

        self.assertEqual(status["state"], "fresh")
        self.assertTrue(status["routing_prior_eligible"])
        self.assertEqual(status["collector_authorization"], "consented")
        self.assertEqual(status["snapshot_authorization"], "consented")
        self.assertEqual(status["database"]["row_counts"]["radar_active"], 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["authorization_status"], "consented")
        self.assertEqual(records[0]["collector_authorization"], "consented")
        self.assertEqual(records[0]["quality_evidence"], "consented-external-prior")

        stale = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=60,
            expire_after_seconds=180,
        ).status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))
        self.assertEqual(stale["state"], "stale")
        self.assertTrue(radar_prior_records(stale, _catalog()))

    def test_refresh_passes_the_daily_interval_to_provider(self) -> None:
        registry = mock.Mock()
        registry.refresh.return_value = {
            "ok": False,
            "network_requested": False,
            "projection": {"ok": False, "state": "degraded"},
        }
        with mock.patch("codex_workbench.radar.RadarRegistry", return_value=registry):
            radar = WorkbenchRadar(
                self.state_root,
                self.authorization,
                refresh_interval_seconds=24 * 60 * 60,
            )
            result = radar.refresh()

        registry.refresh.assert_called_once_with(
            self.authorization,
            minimum_refresh_interval_seconds=24 * 60 * 60,
            stale_after_seconds=7 * 24 * 60 * 60,
        )
        self.assertEqual(result["projection"]["state"], "degraded")

    def test_status_and_snapshot_identity_must_remain_bound(self) -> None:
        self._import()
        status = WorkbenchRadar(self.state_root, self.authorization).status(
            now=datetime(2026, 9, 4, 12, 1, tzinfo=UTC)
        )

        mutations = (
            {"snapshot_id": "codex-radar-v1-0000000000000000"},
            {"digest": "0" * 64},
            {"transfer_multiplier": 0.5},
            {"collector_authorization": "unauthorized"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                forged = {**status, **mutation}
                self.assertEqual(radar_prior_records(forged, _catalog()), [])

        stale = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=60,
            expire_after_seconds=180,
        ).status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))
        forged_fresh = {**stale, "state": "fresh", "transfer_multiplier": 1.0}
        self.assertEqual(radar_prior_records(forged_fresh, _catalog()), [])

    def test_large_external_sample_is_capped_below_the_static_exact_baseline(self) -> None:
        self._import(passed=90, valid_tasks=100)
        radar = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=60,
            expire_after_seconds=180,
        )

        fresh = radar.status(now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC))
        stale = radar.status(now=datetime(2026, 9, 4, 12, 2, tzinfo=UTC))

        self.assertEqual(radar_prior_records(fresh, _catalog())[0]["effective_sample_strength"], 2.0)
        self.assertEqual(radar_prior_records(stale, _catalog())[0]["effective_sample_strength"], 0.5)

    def test_performance_snapshot_pins_radar_provenance_and_keeps_it_advisory(self) -> None:
        self._import()
        radar_status = WorkbenchRadar(
            self.state_root,
            self.authorization,
        ).status(now=datetime(2026, 9, 4, 12, 1, tzinfo=UTC))

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

        registry = PerformanceRegistry(Path(self.temporary.name) / "workbench")
        refreshed = registry.refresh(EmptyStore(), _catalog(), radar_status=radar_status)  # type: ignore[arg-type]
        calibration = registry.calibrate(_catalog(), "implementation", "standard")
        luna = calibration["candidates"][0]
        evidence = luna["quality"]["prior"]["evidence"]
        radar_evidence = [item for item in evidence if item["benchmark"] == "Codex Radar community tasks"]
        static_evidence = [item for item in evidence if item["benchmark"] != "Codex Radar community tasks"]
        provenance = refreshed["snapshot"]["source_provenance"]["external_priors"]["codex_radar"]

        self.assertEqual(len(radar_evidence), 1)
        self.assertEqual(radar_evidence[0]["reasoning_effort"], "max")
        self.assertTrue(all("reasoning_effort" not in item for item in static_evidence))
        self.assertEqual(provenance["imported_record_count"], 1)
        self.assertTrue(provenance["offline_last_known_good"])
        self.assertFalse(provenance["iq_used_as_pass_rate"])
        self.assertTrue(refreshed["snapshot"]["advisory_policy"]["hard_capability_gates_required"])
        self.assertFalse(refreshed["snapshot"]["advisory_policy"]["routing_override_permitted"])

    def test_expired_refresh_replaces_a_previously_eligible_radar_prior(self) -> None:
        self._import()
        radar = WorkbenchRadar(
            self.state_root,
            self.authorization,
            stale_after_seconds=60,
            expire_after_seconds=180,
        )

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

        registry = PerformanceRegistry(Path(self.temporary.name) / "workbench-expiry")
        fresh = radar.status(now=datetime(2026, 9, 4, 12, 0, 30, tzinfo=UTC))
        first = registry.refresh(EmptyStore(), _catalog(), radar_status=fresh)  # type: ignore[arg-type]
        self.assertEqual(
            first["snapshot"]["source_provenance"]["external_priors"]["codex_radar"]["imported_record_count"],
            1,
        )

        expired = radar.status(now=datetime(2026, 9, 4, 12, 4, tzinfo=UTC))
        second = registry.refresh(EmptyStore(), _catalog(), radar_status=expired)  # type: ignore[arg-type]
        provenance = second["snapshot"]["source_provenance"]["external_priors"]["codex_radar"]
        calibration = registry.calibrate(_catalog(), "implementation", "standard")
        evidence = calibration["candidates"][0]["quality"]["prior"]["evidence"]

        self.assertNotEqual(first["active_generation_id"], second["active_generation_id"])
        self.assertEqual(provenance["state"], "expired")
        self.assertEqual(provenance["imported_record_count"], 0)
        self.assertFalse(provenance["routing_prior_eligible"])
        self.assertFalse(
            any(item["benchmark"] == "Codex Radar community tasks" for item in evidence)
        )


if __name__ == "__main__":
    unittest.main()
