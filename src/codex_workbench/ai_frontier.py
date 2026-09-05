"""Read-only AI Frontier integration for Workbench performance priors.

The portable :mod:`ai_frontier_provider` owns collection and its durable
last-known-good cache.  This module is the Workbench policy boundary:
freshness, consent, exact catalog admission, and weak-prior conversion.

AI Frontier Quality is benchmark accuracy and therefore pseudo-evidence only;
it is never local first-pass evidence.  Consistency reduces evidence strength
and is exposed separately.  Publisher-observed cost is advisory metadata and
never a quota admission decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import math
import re
from typing import Any, Sequence

try:
    from ai_frontier_provider import AIFrontierRegistry, validate_ai_frontier_snapshot
except ImportError:  # Provider is an independently installable package.
    AIFrontierRegistry = None  # type: ignore[assignment,misc]

    def validate_ai_frontier_snapshot(value: object) -> object:
        raise ImportError("ai_frontier_provider is not installed")


AI_FRONTIER_PROVIDER = "ai-frontier-provider"
AI_FRONTIER_SOURCE_URL = "https://aifrontier.withmartian.com/"
AI_FRONTIER_RELIABILITY_URL = (
    "https://aifrontier.withmartian.com/api/reliability/leaderboard"
)
AI_FRONTIER_COST_URL = "https://aifrontier.withmartian.com/api/cost-comparison"
AI_FRONTIER_FRESH_TRANSFER_MULTIPLIER = 1.0
AI_FRONTIER_STALE_TRANSFER_MULTIPLIER = 0.25
AI_FRONTIER_EXPIRED_TRANSFER_MULTIPLIER = 0.0
AI_FRONTIER_MAX_RECORD_STRENGTH = 0.75
AI_FRONTIER_MAX_OVERALL_STRENGTH = 0.25
AI_FRONTIER_MAX_MODEL_STRENGTH = 2.0
AI_FRONTIER_TRANSFER_WEIGHT = 0.15
AI_FRONTIER_REFRESH_INTERVAL_SECONDS = 72 * 60 * 60
AI_FRONTIER_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
AI_FRONTIER_EXPIRE_AFTER_SECONDS = 31 * 24 * 60 * 60

AI_FRONTIER_CATEGORY_TASK_TYPES: dict[str, tuple[str, ...]] = {
    "coding": ("implementation", "debugging", "tests"),
    "agentic": ("implementation", "debugging", "tests", "exploration"),
    "reasoning": ("architecture", "review", "research", "exploration"),
    "instruction-following": ("docs",),
    "factuality": ("research", "review", "docs"),
}
AI_FRONTIER_OVERALL_TASK_TYPES = tuple(
    sorted({task for tasks in AI_FRONTIER_CATEGORY_TASK_TYPES.values() for task in tasks})
)
_AUTHORIZED_STATUSES = frozenset({"authorized", "consented"})
_KNOWN_PROVIDERS = {
    "openai": "codex",
    "anthropic": "claude",
    "codex": "codex",
    "claude": "claude",
}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip().rstrip("%"))
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _bounded_unit(value: object) -> float | None:
    result = _number(value)
    if result is None:
        return None
    if result > 1 and result <= 100:
        result /= 100
    return result if 0 <= result <= 1 else None


def _first(mapping: Mapping[str, Any], *keys: str) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalise_provider(value: object) -> str | None:
    text = _text(value)
    return _KNOWN_PROVIDERS.get(text.lower()) if text else None


def _model_family(model_id: str) -> str:
    lowered = model_id.lower()
    for family in ("spark", "luna", "terra", "sol", "fable", "opus", "sonnet"):
        if family in lowered:
            return family
    return "unknown"


def _parse_time(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _snapshot_age(snapshot: Mapping[str, Any], status: Mapping[str, Any], now: datetime) -> int:
    raw_age = status.get("age_seconds")
    if isinstance(raw_age, int) and not isinstance(raw_age, bool) and raw_age >= 0:
        return raw_age
    for field in ("fetched_at", "observed_at", "updated_at"):
        parsed = _parse_time(snapshot.get(field))
        if parsed is not None:
            return max(0, int((now - parsed).total_seconds()))
    return 2**31 - 1


def _snapshot_auth(snapshot: Mapping[str, Any]) -> str | None:
    for key in ("authorization", "consent"):
        value = snapshot.get(key)
        if isinstance(value, Mapping):
            status = _text(value.get("status"))
            if status is not None:
                return status.lower()
        elif isinstance(value, str):
            return value.strip().lower()
    return None


def _authorization_status(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("status", "authorization_status"):
        status = _text(value.get(key))
        if status is not None:
            return status.lower()
    return None


def _authorization_ok(value: object, status: str | None) -> bool:
    if status not in _AUTHORIZED_STATUSES:
        return False
    # The provider returns ok=True for a validated receipt.  Treat an omitted
    # flag as compatible with older adapters, but never accept an explicit
    # negative result.
    return not isinstance(value, Mapping) or value.get("ok") is not False


def _provider_error_message(value: object) -> str:
    if isinstance(value, Mapping):
        for key in ("error", "last_error", "reason"):
            text = _text(value.get(key))
            if text:
                return text
    return "AI Frontier provider cache is unavailable."


@dataclass(frozen=True)
class WorkbenchAIFrontier:
    """Workbench policy view over the provider's portable cache."""

    state_root: Path
    authorization_file: Path
    enabled: bool = True
    refresh_interval_seconds: int = AI_FRONTIER_REFRESH_INTERVAL_SECONDS
    stale_after_seconds: int = AI_FRONTIER_STALE_AFTER_SECONDS
    expire_after_seconds: int = AI_FRONTIER_EXPIRE_AFTER_SECONDS

    @property
    def registry(self) -> Any:
        if AIFrontierRegistry is None:
            raise RuntimeError("ai_frontier_provider is not installed")
        return AIFrontierRegistry(self.state_root)

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Classify the provider cache without networking."""

        if not self.enabled:
            return self._empty_status("disabled", "AI Frontier integration is disabled.")
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        try:
            registry = self.registry
            authorization_status = registry.authorization_status(self.authorization_file)
        except (OSError, TypeError, ValueError, RuntimeError, AttributeError) as error:
            registry = None
            authorization_status = {
                "ok": False,
                "status": "unauthorized",
                "error": str(error),
            }
        current_auth = _authorization_status(authorization_status)
        current_auth_ok = _authorization_ok(authorization_status, current_auth)
        try:
            if registry is None:
                raise RuntimeError("ai_frontier_provider is unavailable")
            provider_status = registry.status(now=checked_at)
        except TypeError:
            if registry is None:
                provider_status = {}
            else:
                provider_status = registry.status()
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            provider_status = {"ok": False, "error": str(error)}
        if not isinstance(provider_status, Mapping):
            provider_status = {}
        provider_status = dict(provider_status)
        snapshot = provider_status.get("snapshot")
        if not isinstance(snapshot, Mapping):
            try:
                active = self.registry.active()
            except (OSError, ValueError, RuntimeError):
                active = None
            if isinstance(active, Mapping):
                snapshot = active
        if not isinstance(snapshot, Mapping):
            state = "unavailable" if current_auth_ok else "unauthorized"
            return {
                **self._empty_status(state, _provider_error_message(provider_status)),
                "collector_authorization": current_auth,
                "database": provider_status.get("database"),
            }

        snapshot = dict(snapshot)
        now_utc = checked_at.astimezone(UTC)
        age_seconds = _snapshot_age(snapshot, provider_status, now_utc)
        provider_stale_after = provider_status.get(
            "stale_after_seconds", self.stale_after_seconds
        )
        if not isinstance(provider_stale_after, int) or isinstance(provider_stale_after, bool):
            provider_stale_after = self.stale_after_seconds
        effective_stale_after = min(self.stale_after_seconds, max(1, provider_stale_after))
        snapshot_auth = _snapshot_auth(snapshot)
        collector_auth = current_auth
        if snapshot_auth is None:
            snapshot_auth = _text(provider_status.get("snapshot_authorization"))
        auth_ok = current_auth_ok and snapshot_auth in _AUTHORIZED_STATUSES
        if not auth_ok:
            return {
                "schema_version": 1,
                "provider": AI_FRONTIER_PROVIDER,
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
                "transfer_multiplier": AI_FRONTIER_EXPIRED_TRANSFER_MULTIPLIER,
                "model_count": _model_count(snapshot),
                "collector_authorization": collector_auth,
                "snapshot_authorization": snapshot_auth,
                "snapshot": snapshot,
                "database": provider_status.get("database"),
                "reason": "AI Frontier personal-use authorization receipt is missing or invalid.",
            }

        if age_seconds <= effective_stale_after:
            state = "fresh"
            transfer_multiplier = AI_FRONTIER_FRESH_TRANSFER_MULTIPLIER
        elif age_seconds <= self.expire_after_seconds:
            state = "stale"
            transfer_multiplier = AI_FRONTIER_STALE_TRANSFER_MULTIPLIER
        else:
            state = "expired"
            transfer_multiplier = AI_FRONTIER_EXPIRED_TRANSFER_MULTIPLIER
        return {
            "schema_version": 1,
            "provider": AI_FRONTIER_PROVIDER,
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
            "model_count": _model_count(snapshot),
            "collector_authorization": collector_auth,
            "snapshot_authorization": snapshot_auth,
            "snapshot": snapshot,
            "database": provider_status.get("database"),
        }

    def refresh(self, *, source_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Refresh the provider cache, then classify it under Workbench policy."""

        if not self.enabled:
            return self._empty_status("disabled", "AI Frontier integration is disabled.")
        try:
            result = self.registry.refresh(
                self.authorization_file,
                minimum_refresh_interval_seconds=self.refresh_interval_seconds,
                stale_after_seconds=self.stale_after_seconds,
                model_source_ids=source_ids,
            )
        except TypeError:
            result = self.registry.refresh(
                self.authorization_file,
                minimum_refresh_interval_seconds=self.refresh_interval_seconds,
            )
        classified = self.status()
        if isinstance(result, Mapping):
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
            "provider": AI_FRONTIER_PROVIDER,
            "enabled": state != "disabled",
            "ok": False,
            "state": state,
            "routing_prior_eligible": False,
            "offline_cache_available": False,
            "snapshot_id": None,
            "digest": None,
            "model_count": 0,
            "collector_authorization": None,
            "snapshot_authorization": None,
            "snapshot": None,
            "reason": reason,
        }


def ai_frontier_prior_records(
    status: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Convert eligible observations into weak, exact-model benchmark records."""

    if not isinstance(status, Mapping) or not isinstance(catalog, Mapping):
        return []
    if (
        status.get("provider") != AI_FRONTIER_PROVIDER
        or status.get("routing_prior_eligible") is not True
        or status.get("state") not in {"fresh", "stale"}
        or status.get("collector_authorization") not in _AUTHORIZED_STATUSES
        or status.get("snapshot_authorization") not in _AUTHORIZED_STATUSES
    ):
        return []
    snapshot = status.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    try:
        validated = validate_ai_frontier_snapshot(snapshot)
    except (ImportError, OSError, TypeError, ValueError, KeyError, AttributeError):
        return []
    if isinstance(validated, Mapping):
        snapshot = validated
    if (
        status.get("snapshot_id") != snapshot.get("snapshot_id")
        or status.get("digest") != snapshot.get("digest")
    ):
        return []
    age_seconds = status.get("age_seconds")
    stale_after = status.get("stale_after_seconds")
    expire_after = status.get("expire_after_seconds")
    if (
        isinstance(age_seconds, bool)
        or not isinstance(age_seconds, int)
        or age_seconds < 0
        or isinstance(stale_after, bool)
        or not isinstance(stale_after, int)
        or stale_after <= 0
        or isinstance(expire_after, bool)
        or not isinstance(expire_after, int)
        or expire_after < stale_after
    ):
        return []
    derived_state = (
        "fresh"
        if age_seconds <= stale_after
        else "stale"
        if age_seconds <= expire_after
        else "expired"
    )
    if derived_state != status.get("state"):
        return []
    multiplier = _number(status.get("transfer_multiplier"))
    expected_multiplier = (
        AI_FRONTIER_FRESH_TRANSFER_MULTIPLIER
        if status.get("state") == "fresh"
        else AI_FRONTIER_STALE_TRANSFER_MULTIPLIER
    )
    if multiplier is None or multiplier <= 0 or abs(multiplier - expected_multiplier) > 1e-9:
        return []
    known = _known_catalog_models(catalog)
    if not known:
        return []
    models = _observed_models(snapshot)
    if not models:
        return []
    source_urls = snapshot.get("source_urls")
    source_urls = source_urls if isinstance(source_urls, Mapping) else {}
    source_url = _source_url(source_urls)
    snapshot_id = _text(snapshot.get("snapshot_id")) or "unknown"
    source_updated = (
        _text(snapshot.get("source_updated_at"))
        or _text(snapshot.get("fetched_at"))
        or "snapshot-v1"
    )
    records: list[dict[str, Any]] = []
    category_index = _category_index(snapshot)
    for observed in models:
        source_id = _text(observed.get("source_id"))
        model_id = _text(observed.get("model_id"))
        category_rows = category_index.get(source_id or model_id or "", ())
        # The provider stores cost-comparison rows as a separate category
        # because that endpoint has no model quality.  Join its relative cost
        # signal back to the model observation without treating it as quality.
        observed_for_records = dict(observed)
        for row in category_rows:
            category_key = _text(_first(row, "category_key", "category"))
            if category_key != "cost-comparison":
                continue
            if _first(observed_for_records, "cost", "Cost", "real_cost", "Real Cost") is None:
                observed_for_records["cost"] = _first(row, "cost", "Real Cost", "real_cost")
            if _first(observed_for_records, "cost_surprise", "Cost Surprise") is None:
                observed_for_records["cost_surprise"] = _first(row, "cost_surprise", "Cost Surprise")
        records.extend(
            _records_for_observation(
                observed_for_records,
                known=known,
                multiplier=multiplier,
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                source_url=source_url,
                source_updated=source_updated,
                freshness_state=str(status["state"]),
                category_rows=category_index.get(
                    source_id or model_id or "",
                    category_rows,
                ),
            )
        )
    return _cap_and_sort_records(records)


def _known_catalog_models(catalog: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    models = catalog.get("models")
    known: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(models, list):
        return known
    for candidate in models:
        if not isinstance(candidate, Mapping) or candidate.get("routable") is not True:
            continue
        provider = _normalise_provider(candidate.get("provider"))
        identity = candidate.get("identity")
        canonical_id = identity.get("canonical_model_id") if isinstance(identity, Mapping) else None
        model_id = _text(canonical_id) or _text(candidate.get("model_id"))
        if provider and model_id:
            known[(provider, model_id.lower())] = candidate
    return known


def _observed_models(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("models", "records", "reliability", "leaderboard", "reliability_leaderboard"):
        values = snapshot.get(key)
        if isinstance(values, list):
            return [item for item in values if isinstance(item, Mapping)]
    return []


def _model_identity(observed: Mapping[str, Any]) -> tuple[str | None, str | None]:
    model = _text(_first(observed, "model_id", "model", "executor", "llm_name", "name"))
    provider = _normalise_provider(_first(observed, "provider", "model_provider"))
    if provider is None and model and "/" in model:
        prefix, _, _ = model.partition("/")
        provider = _normalise_provider(prefix)
    return provider, model


def _effort_supported(capability: Mapping[str, Any], effort: str | None) -> bool:
    if effort is None:
        return True
    reasoning = capability.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return False
    supported = reasoning.get("supported_efforts")
    return isinstance(supported, list) and effort in supported


def _quality(observed: Mapping[str, Any]) -> float | None:
    return _bounded_unit(_first(observed, "quality", "Quality", "accuracy", "Accuracy", "score"))


def _category_values(observed: Mapping[str, Any]) -> list[tuple[str, float]]:
    raw = _first(observed, "categories", "category_metrics", "category_quality", "benchmarks")
    values: list[tuple[str, float]] = []
    if isinstance(raw, Mapping):
        for category, item in raw.items():
            category_name = _normalise_category(category)
            quality = _category_quality(item)
            if category_name is not None and quality is not None:
                values.append((category_name, quality))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            category_name = _normalise_category(
                _first(item, "category", "category_name", "benchmark", "domain")
            )
            quality = _category_quality(item)
            if category_name is not None and quality is not None:
                values.append((category_name, quality))
    return sorted(set(values), key=lambda item: item[0])


def _category_index(snapshot: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Index provider-normalized top-level category rows by source model."""

    raw = snapshot.get("categories")
    if not isinstance(raw, list):
        return {}
    indexed: dict[str, list[Mapping[str, Any]]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        source_id = _text(item.get("source_id"))
        model_id = _text(item.get("model_id"))
        if source_id is None and model_id is None:
            continue
        for key in {value for value in (source_id, model_id) if value is not None}:
            indexed.setdefault(key, []).append(item)
    return indexed


def _normalise_category(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = text.lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "instructionfollowing": "instruction-following",
        "instruction-following": "instruction-following",
        "factual": "factuality",
        "medical-reasoning": "medical",
    }
    return aliases.get(normalized, normalized)


def _category_quality(value: object) -> float | None:
    if isinstance(value, Mapping):
        return _bounded_unit(_first(value, "quality", "Quality", "accuracy", "Accuracy", "score"))
    return _bounded_unit(value)


def _source_url(source_urls: Mapping[str, Any]) -> str:
    for key in ("reliability", "reliability_leaderboard", "leaderboard", "quality", "homepage"):
        value = _text(source_urls.get(key))
        if value and value.startswith("https://"):
            return value
    return AI_FRONTIER_RELIABILITY_URL


def _records_for_observation(
    observed: Mapping[str, Any],
    *,
    known: Mapping[tuple[str, str], Mapping[str, Any]],
    multiplier: float,
    snapshot: Mapping[str, Any],
    snapshot_id: str,
    source_url: str,
    source_updated: str,
    freshness_state: str,
    category_rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    provider, model_id = _model_identity(observed)
    if provider is None or model_id is None:
        return []
    capability = known.get((provider, model_id.lower()))
    if capability is None:
        return []
    effort = _text(_first(observed, "reasoning_effort", "effort"))
    if not _effort_supported(capability, effort):
        return []
    consistency = _bounded_unit(_first(observed, "consistency", "Consistency"))
    consistency_std = _bounded_unit(
        _first(observed, "consistency_std", "Consistency Std", "consistency_stddev")
    )
    cost = _number(
        _first(observed, "observed_cost_usd", "real_cost", "Real Cost", "cost", "Cost")
    )
    cost_surprise = _number(_first(observed, "cost_surprise", "Cost Surprise"))
    if consistency is None:
        consistency_factor = 0.75
    else:
        consistency_factor = 0.5 + 0.5 * consistency
    if consistency_std is not None:
        consistency_factor *= max(0.5, 1.0 - 0.25 * consistency_std)
    consistency_factor = min(1.0, max(0.25, consistency_factor))
    common = {
        "provider": provider,
        "model_id": model_id,
        "model_family": _text(capability.get("model_family")) or _model_family(model_id),
        "effort": effort,
        "consistency": consistency,
        "consistency_std": consistency_std,
        "cost": cost,
        "cost_surprise": cost_surprise,
        "consistency_factor": consistency_factor,
        "source_id": _text(observed.get("source_id")) or AI_FRONTIER_PROVIDER,
        "lineage_id": _text(observed.get("lineage_id")) or f"{snapshot_id}:{model_id}",
        "correlation_group": _text(observed.get("correlation_group"))
        or "ai-frontier:reliability-leaderboard",
    }
    records: list[dict[str, Any]] = []
    overall = _quality(observed)
    if overall is not None:
        records.append(
            _make_record(
                common,
                overall,
                None,
                AI_FRONTIER_MAX_OVERALL_STRENGTH,
                multiplier,
                snapshot,
                snapshot_id,
                source_url,
                source_updated,
                freshness_state,
            )
        )
    category_values = _category_values(observed)
    category_values.extend(
        item
        for item in (
            (
                _normalise_category(
                    _first(row, "category", "category_key", "benchmark", "domain")
                ),
                _category_quality(row),
            )
            for row in category_rows
        )
        if item[0] is not None and item[1] is not None
    )
    for category, quality in sorted(set(category_values), key=lambda item: item[0]):
        if category == "medical" or category not in AI_FRONTIER_CATEGORY_TASK_TYPES:
            continue
        records.append(
            _make_record(
                common,
                quality,
                category,
                AI_FRONTIER_MAX_RECORD_STRENGTH,
                multiplier,
                snapshot,
                snapshot_id,
                source_url,
                source_updated,
                freshness_state,
            )
        )
    return records


def _make_record(
    common: Mapping[str, Any],
    quality: float,
    category: str | None,
    strength_cap: float,
    multiplier: float,
    snapshot: Mapping[str, Any],
    snapshot_id: str,
    source_url: str,
    source_updated: str,
    freshness_state: str,
) -> dict[str, Any]:
    strength = min(
        strength_cap,
        strength_cap * multiplier * float(common["consistency_factor"]),
    )
    task_types = (
        AI_FRONTIER_OVERALL_TASK_TYPES
        if category is None
        else AI_FRONTIER_CATEGORY_TASK_TYPES[category]
    )
    category_label = "overall" if category is None else category
    external_signals = {
        "quality_mean": round(quality, 8),
        "consistency_mean": _round_or_none(common.get("consistency")),
        "consistency_std_mean": _round_or_none(common.get("consistency_std")),
        "observed_cost_mean": _round_or_none(common.get("cost")),
        "cost_surprise_mean": _round_or_none(common.get("cost_surprise")),
        "source_count": 1,
    }
    record: dict[str, Any] = {
        "record_id": _record_id(snapshot_id, str(common["model_id"]), category_label),
        "source_url": source_url,
        "benchmark": "AI Frontier reliability leaderboard",
        "benchmark_version": source_updated,
        "domain": category or "overall",
        "task_types": list(task_types),
        "provider": common["provider"],
        "model_id": common["model_id"],
        "model_family": common["model_family"],
        "agent_scaffold": (
            "Martian AI Frontier public evaluation harness; not local Workbench first-pass"
        ),
        "score": round(quality, 8),
        "score_kind": "accuracy_pseudo_evidence",
        "provenance": "vendor_report",
        "transfer_weight": AI_FRONTIER_TRANSFER_WEIGHT,
        "effective_sample_strength": round(strength, 8),
        "quality_evidence": (
            "publisher AI Frontier accuracy; weak pseudo-evidence only, not local first-pass"
        ),
        "routing_prior_eligible": True,
        "authorization_status": "consented",
        "freshness_state": freshness_state,
        "external_snapshot_id": snapshot_id,
        "external_snapshot_digest": snapshot.get("digest"),
        "source_id": common["source_id"],
        "lineage_id": common["lineage_id"],
        "metric_kind": "accuracy",
        "correlation_group": common["correlation_group"],
        "external_signals": external_signals,
        "cost_signal_kind": (
            "publisher_observed_relative"
            if common.get("cost") is not None
            else "publisher_relative_or_missing"
        ),
    }
    if common.get("effort") is not None:
        record["reasoning_effort"] = common["effort"]
    if common.get("cost") is None and common.get("cost_surprise") is not None:
        record["relative_cost_signal"] = common["cost_surprise"]
    return record


def _cap_and_sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Bound public prior per exact model. Local successes/failures are added
    # by PerformanceRegistry and naturally dominate this weak prior.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault((str(record["provider"]), str(record["model_id"])), []).append(record)
    capped: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = sorted(grouped[key], key=lambda item: (item["domain"], item["record_id"]))
        total = sum(float(item["effective_sample_strength"]) for item in items)
        scale = (
            min(1.0, AI_FRONTIER_MAX_MODEL_STRENGTH / total)
            if total > 0
            else 0.0
        )
        for item in items:
            strength = float(item["effective_sample_strength"]) * scale
            if strength <= 0:
                continue
            item = dict(item)
            item["effective_sample_strength"] = round(strength, 8)
            capped.append(item)
    return sorted(capped, key=lambda item: (item["model_id"], item["domain"], item["record_id"]))


def _round_or_none(value: object) -> float | None:
    number = _number(value)
    return round(number, 8) if number is not None else None


def _record_id(snapshot_id: str, model_id: str, category: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", snapshot_id.lower()).strip("-")[-48:]
    identity = re.sub(r"[^a-z0-9]+", "-", f"{model_id}-{category}".lower()).strip("-")
    return f"ai-frontier-{suffix}-{identity}"[:120]


def _model_count(snapshot: Mapping[str, Any]) -> int:
    return len(_observed_models(snapshot))


__all__ = [
    "AI_FRONTIER_CATEGORY_TASK_TYPES",
    "AI_FRONTIER_EXPIRED_TRANSFER_MULTIPLIER",
    "AI_FRONTIER_FRESH_TRANSFER_MULTIPLIER",
    "AI_FRONTIER_MAX_MODEL_STRENGTH",
    "AI_FRONTIER_MAX_OVERALL_STRENGTH",
    "AI_FRONTIER_MAX_RECORD_STRENGTH",
    "AI_FRONTIER_PROVIDER",
    "AI_FRONTIER_REFRESH_INTERVAL_SECONDS",
    "AI_FRONTIER_STALE_TRANSFER_MULTIPLIER",
    "WorkbenchAIFrontier",
    "ai_frontier_prior_records",
]
