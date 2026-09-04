"""Read-only Codex Radar integration for Workbench performance priors.

The portable :mod:`codex_radar_provider` owns collection and durable cache
semantics.  This module only classifies cache freshness and converts authorized or consented,
exact-model observations into weak Workbench benchmark priors.  It never makes
a routing decision and never treats IQ as a pass rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any

from codex_radar_provider import (
    ATTRIBUTION,
    RadarProviderError,
    RadarRegistry,
    validate_radar_snapshot,
)


RADAR_PRIOR_TASK_TYPES = ("implementation", "debugging", "tests", "docs")
RADAR_FRESH_TRANSFER_MULTIPLIER = 1.0
RADAR_STALE_TRANSFER_MULTIPLIER = 0.25
RADAR_MAX_EFFECTIVE_SAMPLE_STRENGTH = 2.0
RADAR_SAMPLE_STRENGTH_RATE = 0.05


@dataclass(frozen=True)
class WorkbenchRadar:
    """Workbench policy view over the provider's portable cache."""

    state_root: Path
    authorization_file: Path
    enabled: bool = True
    refresh_interval_seconds: int = 24 * 60 * 60
    stale_after_seconds: int = 7 * 24 * 60 * 60
    expire_after_seconds: int = 31 * 24 * 60 * 60

    @property
    def registry(self) -> RadarRegistry:
        return RadarRegistry(self.state_root)

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Classify the last-known-good cache without network access."""

        if not self.enabled:
            return self._empty_status("disabled", "Radar integration is disabled.")

        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        provider_status = self.registry.status(now=checked_at)
        snapshot = provider_status.get("snapshot")
        authorization = self.registry.authorization_status(self.authorization_file)
        if not isinstance(snapshot, Mapping):
            state = "unauthorized" if authorization.get("ok") is not True else "unavailable"
            reason = authorization.get("error") if state == "unauthorized" else "No cached Radar snapshot is available."
            return {
                **self._empty_status(state, str(reason)),
                "collector_authorization": authorization.get("status"),
                "database": provider_status.get("database"),
            }

        age_seconds = int(provider_status.get("age_seconds", 0))
        provider_stale_after = int(
            provider_status.get("stale_after_seconds", self.stale_after_seconds)
        )
        effective_stale_after = min(self.stale_after_seconds, provider_stale_after)
        if authorization.get("ok") is not True:
            return {
                "schema_version": 1,
                "provider": "codex-radar-provider",
                "enabled": True,
                "ok": False,
                "state": "unauthorized",
                "routing_prior_eligible": False,
                "offline_cache_available": True,
                "snapshot_id": snapshot.get("snapshot_id"),
                "digest": snapshot.get("digest"),
                "fetched_at": snapshot.get("fetched_at"),
                "source_updated_at": snapshot.get("source_updated_at"),
                "age_seconds": age_seconds,
                "stale_after_seconds": effective_stale_after,
                "provider_stale_after_seconds": provider_stale_after,
                "workbench_stale_after_seconds": self.stale_after_seconds,
                "expire_after_seconds": self.expire_after_seconds,
                "transfer_multiplier": 0.0,
                "model_count": len(snapshot.get("models", ())),
                "attribution": snapshot.get("attribution", ATTRIBUTION),
                "collector_authorization": authorization.get("status"),
                "snapshot_authorization": _snapshot_authorization_status(snapshot),
                "snapshot": dict(snapshot),
                "database": provider_status.get("database"),
                "reason": authorization.get("error", "Radar authorization is unavailable."),
            }
        if age_seconds <= effective_stale_after:
            state = "fresh"
            transfer_multiplier = RADAR_FRESH_TRANSFER_MULTIPLIER
        elif age_seconds <= self.expire_after_seconds:
            state = "stale"
            transfer_multiplier = RADAR_STALE_TRANSFER_MULTIPLIER
        else:
            state = "expired"
            transfer_multiplier = 0.0

        models = snapshot.get("models")
        model_count = len(models) if isinstance(models, list) else 0
        return {
            "schema_version": 1,
            "provider": "codex-radar-provider",
            "enabled": True,
            "ok": state in {"fresh", "stale"},
            "state": state,
            "routing_prior_eligible": state in {"fresh", "stale"},
            "offline_cache_available": True,
            "snapshot_id": snapshot.get("snapshot_id"),
            "digest": snapshot.get("digest"),
            "fetched_at": snapshot.get("fetched_at"),
            "source_updated_at": snapshot.get("source_updated_at"),
            "age_seconds": age_seconds,
            "stale_after_seconds": effective_stale_after,
            "provider_stale_after_seconds": provider_stale_after,
            "workbench_stale_after_seconds": self.stale_after_seconds,
            "expire_after_seconds": self.expire_after_seconds,
            "transfer_multiplier": transfer_multiplier,
            "model_count": model_count,
            "attribution": snapshot.get("attribution", ATTRIBUTION),
            "collector_authorization": authorization.get("status"),
            "snapshot_authorization": _snapshot_authorization_status(snapshot),
            "snapshot": dict(snapshot),
            "database": provider_status.get("database"),
        }

    def refresh(self) -> dict[str, Any]:
        """Refresh provider cache, then classify it under Workbench policy."""

        if not self.enabled:
            return self._empty_status("disabled", "Radar integration is disabled.")
        result = self.registry.refresh(
            self.authorization_file,
            minimum_refresh_interval_seconds=self.refresh_interval_seconds,
            stale_after_seconds=self.stale_after_seconds,
        )
        classified = self.status()
        for key in (
            "operation",
            "network_requested",
            "generation_created",
            "refresh_deferred",
            "last_error",
            "projection",
        ):
            if key in result:
                classified[key] = result[key]
        if result.get("ok") is not True:
            classified["ok"] = False
        return classified

    @staticmethod
    def _empty_status(state: str, reason: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider": "codex-radar-provider",
            "enabled": state != "disabled",
            "ok": False,
            "state": state,
            "routing_prior_eligible": False,
            "offline_cache_available": False,
            "snapshot_id": None,
            "digest": None,
            "model_count": 0,
            "attribution": ATTRIBUTION,
            "snapshot": None,
            "reason": reason,
        }


def radar_prior_records(
    status: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Convert usable exact-model Radar observations into weak prior records."""

    if (
        status.get("provider") != "codex-radar-provider"
        or status.get("routing_prior_eligible") is not True
        or status.get("state") not in {"fresh", "stale"}
        or status.get("collector_authorization") not in {"authorized", "consented"}
        or status.get("snapshot_authorization") not in {"authorized", "consented"}
    ):
        return []
    snapshot = status.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    try:
        validate_radar_snapshot(snapshot)
    except RadarProviderError:
        return []
    if (
        status.get("snapshot_id") != snapshot.get("snapshot_id")
        or status.get("digest") != snapshot.get("digest")
    ):
        return []
    expected_multiplier = (
        RADAR_FRESH_TRANSFER_MULTIPLIER
        if status.get("state") == "fresh"
        else RADAR_STALE_TRANSFER_MULTIPLIER
    )
    age_seconds = status.get("age_seconds")
    stale_after_seconds = status.get("stale_after_seconds")
    expire_after_seconds = status.get("expire_after_seconds")
    if (
        isinstance(age_seconds, bool)
        or not isinstance(age_seconds, int)
        or age_seconds < 0
        or isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds <= 0
        or isinstance(expire_after_seconds, bool)
        or not isinstance(expire_after_seconds, int)
        or expire_after_seconds < stale_after_seconds
    ):
        return []
    derived_state = (
        "fresh"
        if age_seconds <= stale_after_seconds
        else "stale"
        if age_seconds <= expire_after_seconds
        else "expired"
    )
    if status.get("state") != derived_state:
        return []
    raw_multiplier = status.get("transfer_multiplier")
    if isinstance(raw_multiplier, bool) or not isinstance(raw_multiplier, (int, float)):
        return []
    if abs(float(raw_multiplier) - expected_multiplier) > 1e-9:
        return []
    models = snapshot.get("models")
    if not isinstance(models, list):
        return []

    catalog_models = catalog.get("models") if isinstance(catalog, Mapping) else None
    known: dict[tuple[str, str], Mapping[str, Any]] = {}
    for candidate in catalog_models if isinstance(catalog_models, list) else ():
        if not isinstance(candidate, Mapping) or candidate.get("routable") is not True:
            continue
        provider = candidate.get("provider")
        model_id = candidate.get("model_id")
        if isinstance(provider, str) and isinstance(model_id, str):
            known[(provider, model_id)] = candidate

    multiplier = float(raw_multiplier)
    if multiplier <= 0:
        return []
    source_urls = snapshot.get("source_urls")
    sources = source_urls if isinstance(source_urls, Mapping) else {}
    upstream = snapshot.get("upstream")
    upstream_data = upstream if isinstance(upstream, Mapping) else {}
    snapshot_id = str(snapshot.get("snapshot_id", "unknown"))
    records: list[dict[str, Any]] = []
    for observed in models:
        if not isinstance(observed, Mapping) or observed.get("routing_eligible") is not True:
            continue
        provider = observed.get("provider")
        model_id = observed.get("model")
        effort = observed.get("reasoning_effort")
        pass_rate = observed.get("pass_rate")
        sample_count = observed.get("sample_count")
        if (
            not isinstance(provider, str)
            or not isinstance(model_id, str)
            or not isinstance(effort, str)
            or isinstance(pass_rate, bool)
            or not isinstance(pass_rate, (int, float))
            or isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count <= 0
            or not 0 <= float(pass_rate) <= 1
        ):
            continue
        capability = known.get((provider, model_id))
        if capability is None or not _effort_supported(capability, effort):
            continue
        strength = min(
            RADAR_MAX_EFFECTIVE_SAMPLE_STRENGTH,
            sample_count * RADAR_SAMPLE_STRENGTH_RATE,
        ) * multiplier
        if strength <= 0:
            continue
        family = capability.get("model_family")
        source_url = sources.get("intelligence_efficiency") or sources.get("current")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            continue
        records.append(
            {
                "record_id": _record_id(snapshot_id, model_id, effort),
                "source_url": source_url,
                "benchmark": "Codex Radar community tasks",
                "benchmark_version": str(
                    snapshot.get("source_updated_at")
                    or upstream_data.get("version")
                    or "snapshot-v1"
                ),
                "domain": "coding",
                "task_types": list(RADAR_PRIOR_TASK_TYPES),
                "provider": provider,
                "model_id": model_id,
                "model_family": family if isinstance(family, str) else "unknown",
                "agent_scaffold": "Codex Radar community observations",
                "score": round(float(pass_rate), 8),
                "score_kind": "resolved_rate",
                "provenance": "community_observation",
                "transfer_weight": 1.0,
                "effective_sample_strength": round(strength, 8),
                "quality_evidence": f"{status['snapshot_authorization']}-external-prior",
                "authorization_status": status["snapshot_authorization"],
                "collector_authorization": status["collector_authorization"],
                "reasoning_effort": effort,
                "external_snapshot_id": snapshot_id,
                "external_snapshot_digest": snapshot.get("digest"),
                "sample_count": sample_count,
                "iq_metadata": observed.get("iq"),
                "freshness_state": status.get("state"),
                "attribution": snapshot.get("attribution", ATTRIBUTION),
            }
        )
    return sorted(records, key=lambda item: (item["model_id"], item["reasoning_effort"]))


def _snapshot_authorization_status(snapshot: Mapping[str, Any]) -> str:
    authorization = snapshot.get("authorization")
    if isinstance(authorization, Mapping) and authorization.get("status") in {
        "authorized",
        "consented",
    }:
        return str(authorization["status"])
    return "invalid"


def _effort_supported(capability: Mapping[str, Any], effort: str) -> bool:
    reasoning = capability.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return False
    supported = reasoning.get("supported_efforts")
    return isinstance(supported, list) and effort in supported


def _record_id(snapshot_id: str, model_id: str, effort: str) -> str:
    suffix = snapshot_id.rsplit("-", 1)[-1]
    identity = re.sub(r"[^a-z0-9]+", "-", f"{model_id}-{effort}".lower()).strip("-")
    return f"codex-radar-{suffix}-{identity}"
