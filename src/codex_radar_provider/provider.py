"""Durable JSON snapshots for the public Codex Radar data contract.

This adapter intentionally consumes only the JSON endpoints documented by
WineChord/codex-radar v0.1.69.  It does not scrape HTML, read Codex/Claude
credentials, start a background process, or make routing decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen


SNAPSHOT_SCHEMA_VERSION = 1
AUTHORIZATION_SCHEMA = "codex-radar-provider-authorization"
AUTHORIZATION_VERSION = 1
UPSTREAM_NAME = "WineChord/codex-radar"
UPSTREAM_REPOSITORY = "https://github.com/WineChord/codex-radar"
UPSTREAM_VERSION = "0.1.69"
UPSTREAM_COMMIT = "4c83973df6b17e6b18b0b56e8735168580fea12b"
ATTRIBUTION = "数据来自 Codex 雷达 codexradar.com"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS = 10 * 60

SOURCE_URLS = {
    "current": "https://codexradar.com/current.json",
    "intelligence_efficiency": "https://codexradar.com/data/intelligence-efficiency.json",
    "model_ratings": "https://codexradar.com/api/model-ratings",
    "radar_insights": "https://api.codexradar.com/api/v1/radar-insights",
}
REQUIRED_PAYLOADS = frozenset(SOURCE_URLS)
_PAYLOAD_ALIASES = {
    "current": "current",
    "current.json": "current",
    "intelligence_efficiency": "intelligence_efficiency",
    "intelligence-efficiency": "intelligence_efficiency",
    "intelligence-efficiency.json": "intelligence_efficiency",
    "data/intelligence-efficiency.json": "intelligence_efficiency",
    "model_ratings": "model_ratings",
    "model-ratings": "model_ratings",
    "api/model-ratings": "model_ratings",
    "radar_insights": "radar_insights",
    "radar-insights": "radar_insights",
    "api/v1/radar-insights": "radar_insights",
}
_SNAPSHOT_ID_RE = re.compile(r"^codex-radar-v1-[0-9a-f]{16}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_KNOWN_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_KNOWN_ROUTING_MODELS = frozenset(
    {
        "gpt-5.3-codex-spark",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    }
)
_SENSITIVE_RECEIPT_KEYS = frozenset(
    {
        "secret",
        "client_secret",
        "password",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "session",
    }
)


class RadarProviderError(ValueError):
    """Raised when a durable radar document violates the provider contract."""


def _now_iso(value: datetime | None = None) -> str:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RadarProviderError(f"{label} must be a JSON object")
    return value


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _model_text(value: object) -> str | None:
    result = _text(value)
    return result.lower() if result else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _nonnegative_number(value: object) -> float | None:
    result = _finite_number(value)
    return result if result is not None and result >= 0 else None


def _nonnegative_int(value: object) -> int | None:
    result = _nonnegative_number(value)
    if result is None or not result.is_integer():
        return None
    return int(result)


def _first_value(payload: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _parse_timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    candidate = text
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest_timestamp(values: list[object]) -> str | None:
    candidates: list[tuple[datetime, str]] = []
    for value in values:
        if isinstance(value, Mapping):
            for nested in value.values():
                nested_text = _latest_timestamp([nested])
                if nested_text is not None:
                    parsed = _parse_timestamp(nested_text)
                    if parsed is not None:
                        candidates.append((parsed, nested_text))
            continue
        text = _text(value)
        parsed = _parse_timestamp(text)
        if text is not None and parsed is not None:
            candidates.append((parsed, text))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _safe_error(error: object) -> str:
    if isinstance(error, URLError):
        return f"network request failed ({error.__class__.__name__})"
    if isinstance(error, RadarProviderError):
        return str(error)
    if isinstance(error, (UnicodeDecodeError, json.JSONDecodeError)):
        return f"payload parsing failed ({error.__class__.__name__})"
    return f"refresh failed ({error.__class__.__name__})"


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_RECEIPT_KEYS
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_credential")
        or normalized.endswith("_credentials")
    )


def _redact_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _is_sensitive_key(key) else _redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _receipt_contains_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            _is_sensitive_key(key) or _receipt_contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_receipt_contains_secret(item) for item in value)
    return False


def _normalise_scope(value: object) -> list[str] | None:
    if isinstance(value, str):
        item = _text(value)
        return [item] if item else None
    if not isinstance(value, list):
        return None
    items = [_text(item) for item in value]
    if not items or any(item is None for item in items):
        return None
    return sorted(set(item for item in items if item is not None))


def _authorization_metadata(authorization_file: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        raw = json.loads(authorization_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "authorization receipt is missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "authorization receipt is unreadable"
    if not isinstance(raw, Mapping):
        return None, "authorization receipt must be a JSON object"
    if _receipt_contains_secret(raw):
        return None, "authorization receipt must not contain secret material"

    schema = _text(raw.get("schema"))
    version = _nonnegative_int(raw.get("version", raw.get("schema_version")))
    provider = _model_text(raw.get("provider"))
    status = _model_text(raw.get("status"))
    scope = _normalise_scope(raw.get("scope"))
    attribution = _text(raw.get("attribution"))

    if schema != AUTHORIZATION_SCHEMA or version != AUTHORIZATION_VERSION:
        return None, "authorization receipt schema is unsupported"
    if provider != "codex-radar" or status != "authorized":
        return None, "authorization receipt is not authorized for codex-radar"
    if scope is None:
        return None, "authorization receipt scope is invalid"
    if attribution is None or "codexradar.com" not in attribution.lower():
        return None, "authorization receipt attribution is invalid"
    return {
        "schema": schema,
        "version": version,
        "provider": provider,
        "status": status,
        "scope": scope,
        "attribution": attribution,
    }, None


def _normalise_payloads(payloads: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    normalized: dict[str, Mapping[str, object]] = {}
    for raw_key, value in payloads.items():
        key = _PAYLOAD_ALIASES.get(str(raw_key).strip().lower())
        if key is None:
            continue
        if key in normalized:
            raise RadarProviderError(f"payload {key!r} was supplied more than once")
        normalized[key] = _mapping(value, f"payload {key!r}")
    missing = sorted(REQUIRED_PAYLOADS - set(normalized))
    if missing:
        raise RadarProviderError(f"required payloads are missing: {', '.join(missing)}")
    return normalized


def _supported_current_schema(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value in {1, 2}
    text = _text(value)
    return text in {"1", "1.0", "2", "2.0", "homepage-fallback-v1"}


def _supported_efficiency_schema(value: object) -> bool:
    if value is None:
        return True
    number = _nonnegative_int(value)
    return number in {1, 2}


def _strict_schema_one(value: object) -> int | None:
    if value is None:
        return None
    number = _nonnegative_int(value)
    if number is None:
        raise RadarProviderError("radar insights schema must be an integer")
    return number


def _normalise_pass_rate(snapshot: Mapping[str, object], sample_count: int | None) -> float | None:
    passed = _nonnegative_int(snapshot.get("passed"))
    if sample_count is not None and sample_count > 0 and passed is not None and passed <= sample_count:
        return passed / sample_count
    raw = _nonnegative_number(snapshot.get("pass_rate"))
    if raw is None:
        return None
    if raw <= 1:
        return raw
    if raw <= 100:
        return raw / 100
    return None


def _snapshot_metrics(snapshot: Mapping[str, object]) -> dict[str, object]:
    sample_count = _nonnegative_int(
        _first_value(snapshot, "valid_tasks", "tasks", "sample_count")
    )
    iq = _nonnegative_number(_first_value(snapshot, "iq", "iq_score", "score"))
    average_cost = _nonnegative_number(
        _first_value(snapshot, "average_cost_usd", "average_price_usd")
    )
    if average_cost is None and _model_text(snapshot.get("cost_usd_basis")) in {
        "per_task_average",
        "per_task",
        "average_per_task",
    }:
        average_cost = _nonnegative_number(snapshot.get("cost_usd"))
    average_runtime = _nonnegative_number(
        _first_value(snapshot, "average_task_seconds", "average_runtime_seconds")
    )
    if average_runtime is None:
        minutes = _nonnegative_number(
            _first_value(snapshot, "average_minutes", "average_duration_minutes")
        )
        average_runtime = minutes * 60 if minutes is not None else None
    return {
        "pass_rate": _normalise_pass_rate(snapshot, sample_count),
        "iq": iq,
        "sample_count": sample_count,
        "avg_cost_usd": average_cost,
        "avg_runtime_seconds": average_runtime,
    }


def _routing_eligible(model: str, effort: str) -> bool:
    if model not in _KNOWN_ROUTING_MODELS or effort not in _KNOWN_EFFORTS:
        return False
    if model == "gpt-5.3-codex-spark":
        return effort == "xhigh"
    return True


def _provider_for_model(model: str) -> str:
    if model.startswith("gpt-") or "codex" in model:
        return "codex"
    if model.startswith("claude-"):
        return "claude"
    return "unknown"


def _merge_model(
    records: dict[tuple[str, str], dict[str, object]],
    *,
    model: object,
    effort: object,
    snapshot: Mapping[str, object],
    source: str,
) -> None:
    normalized_model = _model_text(model)
    normalized_effort = _model_text(effort)
    if normalized_model is None or normalized_effort is None:
        return
    key = (normalized_model, normalized_effort)
    record = records.setdefault(
        key,
        {
            "provider": _provider_for_model(normalized_model),
            "model": normalized_model,
            "reasoning_effort": normalized_effort,
            "routing_eligible": _routing_eligible(normalized_model, normalized_effort),
            "pass_rate": None,
            "iq": None,
            "sample_count": None,
            "avg_cost_usd": None,
            "avg_runtime_seconds": None,
            "community_rating": None,
            "metric_sources": {},
        },
    )
    metrics = _snapshot_metrics(snapshot)
    metric_sources = _mapping(record["metric_sources"], "model metric sources")
    for field, value in metrics.items():
        if value is not None:
            record[field] = value
            metric_sources[field] = source


def _extract_current_models(
    current: Mapping[str, object], records: dict[tuple[str, str], dict[str, object]]
) -> None:
    if not _supported_current_schema(current.get("schema_version")):
        raise RadarProviderError("current payload schema is unsupported")
    model_iq = current.get("model_iq", {})
    if model_iq is None:
        return
    model_iq = _mapping(model_iq, "current model_iq")

    latest = model_iq.get("latest")
    if latest is not None:
        latest = _mapping(latest, "current model_iq latest")
        _merge_model(
            records,
            model=latest.get("model"),
            effort=latest.get("reasoning_effort"),
            snapshot=latest,
            source="current.model_iq",
        )

    comparisons = model_iq.get("comparisons", {})
    if comparisons is None:
        return
    comparisons = _mapping(comparisons, "current model_iq comparisons")
    for name, comparison_value in comparisons.items():
        comparison = _mapping(comparison_value, f"current comparison {name!r}")
        latest = comparison.get("latest")
        if latest is None:
            continue
        latest = _mapping(latest, f"current comparison {name!r} latest")
        _merge_model(
            records,
            model=_first_value(latest, "model") or comparison.get("model"),
            effort=_first_value(latest, "reasoning_effort") or comparison.get("reasoning_effort"),
            snapshot=latest,
            source="current.model_iq.comparisons",
        )


def _extract_efficiency_models(
    payload: Mapping[str, object], records: dict[tuple[str, str], dict[str, object]]
) -> None:
    if not _supported_efficiency_schema(payload.get("schema")):
        raise RadarProviderError("intelligence efficiency payload schema is unsupported")
    points = payload.get("points", [])
    if not isinstance(points, list):
        raise RadarProviderError("intelligence efficiency points must be an array")
    for index, item in enumerate(points):
        point = _mapping(item, f"intelligence efficiency point {index}")
        model = _model_text(point.get("model"))
        effort = _model_text(point.get("effort"))
        iq = _nonnegative_number(point.get("iq"))
        sample_count = _nonnegative_int(point.get("valid_tasks"))
        passed = _nonnegative_int(point.get("passed"))
        if (
            model is None
            or effort is None
            or iq is None
            or sample_count is None
            or sample_count <= 0
            or passed is None
            or passed > sample_count
        ):
            continue
        average_cost = _nonnegative_number(point.get("average_price_usd"))
        average_minutes = _nonnegative_number(point.get("average_minutes"))
        _merge_model(
            records,
            model=model,
            effort=effort,
            snapshot={
                "model": model,
                "reasoning_effort": effort,
                "iq": iq,
                "passed": passed,
                "valid_tasks": sample_count,
                "average_price_usd": average_cost,
                "average_minutes": average_minutes,
            },
            source="intelligence_efficiency",
        )


def _matching_rating(
    ratings: Mapping[str, object], model: str, effort: str
) -> dict[str, object] | None:
    models = ratings.get("models", [])
    if not isinstance(models, list):
        raise RadarProviderError("model ratings models must be an array")
    expected_id = f"{model}-{effort}"
    expected_label = f"{model} {effort}"
    for candidate in models:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = _model_text(candidate.get("id"))
        candidate_label = _model_text(candidate.get("label"))
        if candidate_id != expected_id and candidate_label != expected_label:
            continue
        average = _nonnegative_number(candidate.get("average"))
        count = _nonnegative_int(candidate.get("count"))
        if average is None or count is None:
            return None
        return {"average": average, "sample_count": count}
    return None


def _attach_ratings(
    ratings: Mapping[str, object], records: dict[tuple[str, str], dict[str, object]]
) -> None:
    for record in records.values():
        rating = _matching_rating(
            ratings,
            str(record["model"]),
            str(record["reasoning_effort"]),
        )
        if rating is not None:
            record["community_rating"] = rating


def _array_from_aliases(payload: Mapping[str, object], keys: tuple[str, ...], label: str) -> list[object]:
    for key in keys:
        if key in payload:
            value = payload[key]
            if value is None:
                return []
            if not isinstance(value, list):
                raise RadarProviderError(f"{label} must be an array")
            return value
    return []


def _first_text(payload: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        text = _text(payload.get(key))
        if text is not None:
            return text
    return None


def _first_number(payload: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = _nonnegative_number(payload.get(key))
        if value is not None:
            return value
    return None


def _normalise_insights(payload: Mapping[str, object]) -> dict[str, object]:
    outer = payload
    wrapped = outer.get("data")
    if wrapped is not None and not isinstance(wrapped, Mapping):
        raise RadarProviderError("radar insights data wrapper must be a JSON object")
    body = _mapping(wrapped, "radar insights data") if wrapped is not None else outer
    schemas = [_strict_schema_one(outer.get("schema"))]
    if body is not outer:
        schemas.append(_strict_schema_one(body.get("schema")))
    declared_schemas = [item for item in schemas if item is not None]
    if not declared_schemas or any(item != 1 for item in declared_schemas):
        raise RadarProviderError("radar insights schema is unsupported")

    recommendations: list[dict[str, object]] = []
    for index, group_value in enumerate(
        _array_from_aliases(
            body,
            ("recommendations", "station_recommendations", "station_recs"),
            "radar insight recommendations",
        )
    ):
        group = _mapping(group_value, f"radar insight recommendation {index}")
        items: list[dict[str, object]] = []
        for item_value in _array_from_aliases(
            group,
            ("items", "models", "recommendations"),
            f"radar insight recommendation {index} items",
        ):
            item = _mapping(item_value, f"radar insight recommendation {index} item")
            model = _model_text(item.get("model"))
            effort = _model_text(item.get("effort"))
            iq = _first_number(item, "iq", "current_iq")
            if model is None or effort is None or iq is None:
                continue
            cost = _first_number(item, "average_cost_usd", "average_price_usd", "price_usd", "price")
            minutes = _first_number(
                item,
                "average_duration_minutes",
                "average_minutes",
                "minutes",
            )
            items.append(
                {
                    "model": model,
                    "reasoning_effort": effort,
                    "iq": iq,
                    "avg_cost_usd": cost,
                    "avg_runtime_seconds": minutes * 60 if minutes is not None else None,
                }
            )
        recommendations.append(
            {
                "key": _first_text(group, "key", "id"),
                "title": _text(group.get("title")),
                "rule": _first_text(group, "rule", "description"),
                "items": items,
            }
        )

    alert_value: object | None = None
    for key in ("degradation_alerts", "alerts", "degradation"):
        if key in body:
            alert_value = body[key]
            break
    alert_rule: str | None = None
    raw_alert_items: list[object] = []
    if isinstance(alert_value, list):
        raw_alert_items = alert_value
    elif isinstance(alert_value, Mapping):
        alert_rule = _text(alert_value.get("rule"))
        raw_alert_items = _array_from_aliases(
            alert_value,
            ("items", "alerts"),
            "radar insight degradation alerts",
        )
    elif alert_value is not None:
        raise RadarProviderError("radar insight degradation alerts must be an array or object")

    degradation_items: list[dict[str, object]] = []
    for index, item_value in enumerate(raw_alert_items):
        item = _mapping(item_value, f"radar insight degradation alert {index}")
        model = _model_text(item.get("model"))
        effort = _model_text(item.get("effort"))
        iq = _first_number(item, "iq", "current_iq")
        if model is None or effort is None or iq is None:
            continue
        average_24 = _first_number(item, "average_iq_24h")
        average_48 = _first_number(item, "average_iq_48h")
        drop_24 = _first_number(
            item,
            "from_24h_average_iq",
            "from_24h_high_iq",
            "degradation_24h_iq",
            "drop_24h",
            "drop24h",
        )
        drop_48 = _first_number(
            item,
            "from_48h_average_iq",
            "from_48h_high_iq",
            "degradation_48h_iq",
            "drop_48h",
            "drop48h",
        )
        if drop_24 is None and average_24 is not None:
            drop_24 = max(0.0, average_24 - iq)
        if drop_48 is None and average_48 is not None:
            drop_48 = max(0.0, average_48 - iq)
        if max(drop_24 or 0.0, drop_48 or 0.0) <= 0:
            continue
        degradation_items.append(
            {
                "model": model,
                "reasoning_effort": effort,
                "iq": iq,
                "drop_24h_iq": drop_24,
                "drop_48h_iq": drop_48,
            }
        )

    return {
        "schema": 1,
        "generated_at": _first_text(body, "generated_at") or _first_text(outer, "generated_at"),
        "source_updated_at": _latest_timestamp(
            [body.get("source_updated_at"), outer.get("source_updated_at")]
        ),
        "recommendations": recommendations,
        "degradation_alerts": {"rule": alert_rule, "items": degradation_items},
    }


def _payload_timestamps(
    payloads: Mapping[str, Mapping[str, object]], insights: Mapping[str, object]
) -> dict[str, str | None]:
    current = payloads["current"]
    model_iq = current.get("model_iq")
    model_iq_mapping = model_iq if isinstance(model_iq, Mapping) else {}
    data_source = model_iq_mapping.get("data_source")
    data_source_mapping = data_source if isinstance(data_source, Mapping) else {}
    efficiency = payloads["intelligence_efficiency"]
    efficiency_points = efficiency.get("points")
    point_times = (
        [item.get("latest_graded_at") for item in efficiency_points if isinstance(item, Mapping)]
        if isinstance(efficiency_points, list)
        else []
    )
    return {
        "current": _latest_timestamp(
            [
                current.get("checked_at"),
                current.get("monitored_at"),
                model_iq_mapping.get("updated_at"),
                data_source_mapping.get("checked_at"),
            ]
        ),
        "intelligence_efficiency": _latest_timestamp(
            [efficiency.get("source_updated_at"), *point_times]
        ),
        "model_ratings": _latest_timestamp([payloads["model_ratings"].get("updated_at")]),
        "radar_insights": _latest_timestamp(
            [insights.get("source_updated_at"), insights.get("generated_at")]
        ),
    }


def _normalise_snapshot(
    payloads: Mapping[str, Mapping[str, object]],
    authorization: Mapping[str, object],
    *,
    fetched_at: str,
    stale_after_seconds: int,
    ingest_mode: str,
) -> tuple[dict[str, object], dict[str, object]]:
    records: dict[tuple[str, str], dict[str, object]] = {}
    _extract_current_models(payloads["current"], records)
    _extract_efficiency_models(payloads["intelligence_efficiency"], records)
    _attach_ratings(payloads["model_ratings"], records)
    insights = _normalise_insights(payloads["radar_insights"])
    if not records:
        raise RadarProviderError("payloads did not contain a valid model and reasoning effort pair")

    source_timestamps = _payload_timestamps(payloads, insights)
    source_updated_at = _latest_timestamp(list(source_timestamps.values()))
    redacted_payloads = _redact_payload(payloads)
    raw_payload_digest = _digest(redacted_payloads)
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": None,
        "digest": None,
        "upstream": {
            "name": UPSTREAM_NAME,
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
            "commit": UPSTREAM_COMMIT,
            "json_contract": "WineChord/codex-radar JSON endpoints only",
        },
        "source_urls": dict(SOURCE_URLS),
        "attribution": str(authorization["attribution"]),
        "authorization": {
            "schema": str(authorization["schema"]),
            "version": int(authorization["version"]),
            "provider": str(authorization["provider"]),
            "status": str(authorization["status"]),
            "scope": list(authorization["scope"]),
        },
        "ingest_mode": ingest_mode,
        "fetched_at": fetched_at,
        "source_updated_at": source_updated_at,
        "source_timestamps": source_timestamps,
        "models": sorted(
            records.values(),
            key=lambda item: (str(item["model"]), str(item["reasoning_effort"])),
        ),
        "insights": insights,
        "raw_payload_digest": raw_payload_digest,
        "cache": {
            "state": "fresh",
            "stale_after_seconds": stale_after_seconds,
        },
    }
    identity = dict(snapshot)
    identity.pop("snapshot_id")
    identity.pop("digest")
    digest = _digest(identity)
    snapshot["digest"] = digest
    snapshot["snapshot_id"] = f"codex-radar-v1-{digest[:16]}"
    raw_generation = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "payload_digest": raw_payload_digest,
        "fetched_at": fetched_at,
        "payloads": redacted_payloads,
    }
    return snapshot, raw_generation


def validate_radar_snapshot(snapshot: Mapping[str, object]) -> None:
    """Validate the stable provider schema and content-addressed identity."""

    if not isinstance(snapshot, Mapping):
        raise RadarProviderError("radar snapshot must be a JSON object")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise RadarProviderError("radar snapshot schema_version is unsupported")
    for field in ("snapshot_id", "digest", "fetched_at", "attribution"):
        if _text(snapshot.get(field)) is None:
            raise RadarProviderError(f"radar snapshot {field} must be non-empty")
    snapshot_id = str(snapshot["snapshot_id"])
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise RadarProviderError("radar snapshot_id is invalid")
    digest = str(snapshot["digest"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RadarProviderError("radar snapshot digest is invalid")
    identity = dict(snapshot)
    identity.pop("snapshot_id", None)
    identity.pop("digest", None)
    expected = _digest(identity)
    if digest != expected or snapshot_id != f"codex-radar-v1-{digest[:16]}":
        raise RadarProviderError("radar snapshot identity does not match its content")

    upstream = snapshot.get("upstream")
    sources = snapshot.get("source_urls")
    authorization = snapshot.get("authorization")
    cache = snapshot.get("cache")
    models = snapshot.get("models")
    insights = snapshot.get("insights")
    if not isinstance(upstream, Mapping) or not isinstance(sources, Mapping):
        raise RadarProviderError("radar snapshot provenance is invalid")
    if not isinstance(authorization, Mapping) or authorization.get("status") != "authorized":
        raise RadarProviderError("radar snapshot authorization is invalid")
    if not isinstance(cache, Mapping) or cache.get("state") != "fresh":
        raise RadarProviderError("radar snapshot cache metadata is invalid")
    if not isinstance(cache.get("stale_after_seconds"), int) or cache["stale_after_seconds"] <= 0:
        raise RadarProviderError("radar snapshot stale_after_seconds is invalid")
    if not isinstance(models, list) or not models:
        raise RadarProviderError("radar snapshot models must be a non-empty array")
    if not isinstance(insights, Mapping):
        raise RadarProviderError("radar snapshot insights is invalid")

    for index, model in enumerate(models):
        if not isinstance(model, Mapping):
            raise RadarProviderError(f"radar model {index} must be an object")
        if _model_text(model.get("model")) is None or _model_text(model.get("reasoning_effort")) is None:
            raise RadarProviderError(f"radar model {index} lacks model or reasoning_effort")
        if not isinstance(model.get("routing_eligible"), bool):
            raise RadarProviderError(f"radar model {index} routing_eligible must be boolean")
        for field in ("pass_rate", "iq", "avg_cost_usd", "avg_runtime_seconds"):
            value = model.get(field)
            if value is not None and _nonnegative_number(value) is None:
                raise RadarProviderError(f"radar model {index} {field} must be non-negative or null")
        pass_rate = model.get("pass_rate")
        if pass_rate is not None and float(pass_rate) > 1:
            raise RadarProviderError(f"radar model {index} pass_rate must be in [0, 1]")
        sample_count = model.get("sample_count")
        if sample_count is not None and _nonnegative_int(sample_count) is None:
            raise RadarProviderError(f"radar model {index} sample_count must be a non-negative integer")


class RadarRegistry:
    """File-backed provider state that any local Python consumer can read."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self._generations_root = self.state_root / "generations"
        self._raw_root = self.state_root / "raw"
        self._active_path = self.state_root / "active.json"

    def active(self) -> dict[str, object] | None:
        """Return the valid last-known-good normalized snapshot, if one exists."""

        try:
            pointer = json.loads(self._active_path.read_text(encoding="utf-8"))
            if not isinstance(pointer, Mapping):
                return None
            snapshot_id = pointer.get("snapshot_id")
            if not isinstance(snapshot_id, str):
                return None
            return self.load_generation(snapshot_id)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def authorization_status(self, authorization_file: Path) -> dict[str, object]:
        """Read and validate an authorization receipt without any network activity.

        The response intentionally exposes only the receipt's non-secret
        authorization metadata and a safe validation summary.  It is suitable
        for a consumer health endpoint or preflight gate.
        """

        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "provider": "codex-radar-provider",
                "ok": False,
                "status": "unauthorized",
                "error": error or "authorization receipt is invalid",
            }
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "provider": "codex-radar-provider",
            "ok": True,
            "status": "authorized",
            "receipt": dict(authorization),
        }

    def load_generation(self, snapshot_id: str) -> dict[str, object] | None:
        """Load one normalized generation by its stable content-addressed ID."""

        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            return None
        path = self._generations_root / f"{snapshot_id}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return None
            validate_radar_snapshot(document)
            return document
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, RadarProviderError):
            return None

    def status(self, now: datetime | None = None) -> dict[str, object]:
        """Describe whether the active snapshot is usable fresh cache, stale cache, or absent."""

        snapshot = self.active()
        if snapshot is None:
            return {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "provider": "codex-radar-provider",
                "ok": False,
                "state": "unavailable",
                "cache_status": "unavailable",
                "snapshot_id": None,
                "digest": None,
                "snapshot": None,
            }
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        fetched = _parse_timestamp(snapshot.get("fetched_at"))
        cache = _mapping(snapshot["cache"], "radar snapshot cache")
        stale_after = int(cache["stale_after_seconds"])
        age_seconds = (
            max(0, int((checked_at.astimezone(UTC) - fetched).total_seconds()))
            if fetched is not None
            else stale_after + 1
        )
        state = "fresh" if age_seconds <= stale_after else "stale"
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "provider": "codex-radar-provider",
            "ok": state == "fresh",
            "state": state,
            "cache_status": "cache" if state == "fresh" else "stale-cache",
            "snapshot_id": snapshot["snapshot_id"],
            "digest": snapshot["digest"],
            "fetched_at": snapshot["fetched_at"],
            "source_updated_at": snapshot.get("source_updated_at"),
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after,
            "authorization_status": _mapping(snapshot["authorization"], "radar snapshot authorization")[
                "status"
            ],
            "snapshot": snapshot,
        }

    def refresh(
        self,
        authorization_file: Path,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        api_key_env: str | None = None,
        api_key_header: str | None = None,
        minimum_refresh_interval_seconds: int = DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
    ) -> dict[str, object]:
        """Fetch all documented JSON endpoints after local authorization validation."""

        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return self._failure("refresh", error or "authorization is invalid", authorization_status="unauthorized")
        if timeout_seconds <= 0 or stale_after_seconds <= 0 or minimum_refresh_interval_seconds < 0:
            return self._failure("refresh", "refresh timing values are invalid")
        key, key_error = self._api_key(api_key_env, api_key_header)
        if key_error is not None:
            return self._failure("refresh", key_error)

        existing = self.active()
        if existing is not None and minimum_refresh_interval_seconds > 0:
            fetched = _parse_timestamp(existing.get("fetched_at"))
            if fetched is not None:
                age = (datetime.now(UTC) - fetched).total_seconds()
                if 0 <= age < minimum_refresh_interval_seconds:
                    result = self.status()
                    result.update(
                        {
                            "operation": "refresh",
                            "network_requested": False,
                            "refresh_deferred": "minimum_refresh_interval",
                        }
                    )
                    return result

        try:
            payloads = {
                name: self._fetch_json(url, timeout_seconds, api_key_header, key)
                for name, url in SOURCE_URLS.items()
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RadarProviderError) as exc:
            return self._failure("refresh", _safe_error(exc))
        return self._ingest(
            payloads,
            authorization,
            stale_after_seconds=stale_after_seconds,
            ingest_mode="refresh",
            fetched_at=None,
        )

    def import_payloads(
        self,
        payloads: Mapping[str, object],
        authorization_file: Path,
        *,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        fetched_at: str | datetime | None = None,
    ) -> dict[str, object]:
        """Persist authorized local JSON exports without making any network request."""

        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return self._failure("import", error or "authorization is invalid", authorization_status="unauthorized")
        if stale_after_seconds <= 0:
            return self._failure("import", "stale_after_seconds must be positive")
        return self._ingest(
            payloads,
            authorization,
            stale_after_seconds=stale_after_seconds,
            ingest_mode="import",
            fetched_at=fetched_at,
        )

    def _api_key(
        self, api_key_env: str | None, api_key_header: str | None
    ) -> tuple[str | None, str | None]:
        if api_key_env is None and api_key_header is None:
            return None, None
        if _text(api_key_env) is None or _text(api_key_header) is None:
            return None, "api_key_env and api_key_header must be supplied together"
        if not _HEADER_NAME_RE.fullmatch(str(api_key_header)):
            return None, "api_key_header is invalid"
        value = os.environ.get(str(api_key_env))
        if _text(value) is None:
            return None, "configured API key environment variable is unavailable"
        return value, None

    @staticmethod
    def _fetch_json(
        url: str,
        timeout_seconds: int,
        api_key_header: str | None,
        api_key: str | None,
    ) -> Mapping[str, object]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "codex-radar-provider/1",
        }
        if api_key_header is not None and api_key is not None:
            headers[api_key_header] = api_key
        request = Request(url, headers=headers)
        response = urlopen(request, timeout=float(timeout_seconds))
        try:
            payload = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        decoded = json.loads(payload.decode("utf-8"))
        return _mapping(decoded, f"response for {url}")

    def _ingest(
        self,
        payloads: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        stale_after_seconds: int,
        ingest_mode: str,
        fetched_at: str | datetime | None,
    ) -> dict[str, object]:
        try:
            normalized_payloads = _normalise_payloads(payloads)
            recorded_at = _now_iso(fetched_at) if isinstance(fetched_at, datetime) else fetched_at or _now_iso()
            if _parse_timestamp(recorded_at) is None:
                raise RadarProviderError("fetched_at must be an ISO-8601 timestamp")
            snapshot, raw_generation = _normalise_snapshot(
                normalized_payloads,
                authorization,
                fetched_at=recorded_at,
                stale_after_seconds=stale_after_seconds,
                ingest_mode=ingest_mode,
            )
            previous = self.active()
            previous_timestamp = (
                _parse_timestamp(previous.get("source_updated_at")) if previous is not None else None
            )
            incoming_timestamp = _parse_timestamp(snapshot.get("source_updated_at"))
            if (
                previous_timestamp is not None
                and incoming_timestamp is not None
                and incoming_timestamp < previous_timestamp
            ):
                raise RadarProviderError("source timestamp regressed; last-known-good snapshot was retained")
            validate_radar_snapshot(snapshot)
            self._persist(snapshot, raw_generation)
        except (OSError, TypeError, ValueError, RadarProviderError) as exc:
            return self._failure(ingest_mode, _safe_error(exc))
        result = self.status()
        result.update(
            {
                "operation": ingest_mode,
                "network_requested": ingest_mode == "refresh",
                "generation_created": True,
            }
        )
        return result

    def _persist(self, snapshot: Mapping[str, object], raw_generation: Mapping[str, object]) -> None:
        snapshot_id = str(snapshot["snapshot_id"])
        self._generations_root.mkdir(parents=True, exist_ok=True)
        self._raw_root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._raw_root / f"{snapshot_id}.json", raw_generation)
        self._atomic_write(self._generations_root / f"{snapshot_id}.json", snapshot)
        self._atomic_write(
            self._active_path,
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "updated_at": _now_iso(),
            },
        )

    @staticmethod
    def _atomic_write(path: Path, document: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_canonical_json(document))
                handle.write("\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _failure(
        self,
        operation: str,
        error: str,
        *,
        authorization_status: str | None = None,
    ) -> dict[str, object]:
        result = self.status()
        result.update(
            {
                "ok": False,
                "operation": operation,
                "network_requested": False,
                "generation_created": False,
                "last_error": error,
            }
        )
        if authorization_status is not None:
            result["authorization_status"] = authorization_status
        return result
