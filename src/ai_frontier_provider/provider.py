"""Offline-first snapshots for Martian AI Frontier public JSON observations.

This package deliberately stays independent of Codex Workbench and DSH.  It
collects only the public aggregate JSON endpoints advertised by AI Frontier,
requires an explicit local personal-use consent receipt, and persists a
last-known-good (LKG) snapshot in an isolated SQLite database.  It never
scrapes HTML, reads cookies or credentials, invokes a model, or makes routing
decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Mapping, Sequence
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SNAPSHOT_SCHEMA_VERSION = 1
DATABASE_SCHEMA_VERSION = 1
AUTHORIZATION_SCHEMA = "ai-frontier-provider-authorization"
AUTHORIZATION_VERSION = 1
SOURCE_KEY = "martian-ai-frontier"
PROVIDER_NAME = "ai-frontier-provider"
ATTRIBUTION = "数据来自 Martian AI Frontier aifrontier.withmartian.com"
BASE_URL = "https://aifrontier.withmartian.com"
PAPER_URL = "https://arxiv.org/abs/2606.26836"
# This is source provenance rather than an assertion of a publisher license.
TERMS_URL = "https://withmartian.com/terms-of-service"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS = 72 * 60 * 60
HARD_MINIMUM_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
MAX_MODEL_BENCHMARKS = 8
USER_AGENT = "ai-frontier-provider/0.1.0 (+https://github.com/lisihao/codex-workbench)"
LOCAL_OPERATOR_CONSENT_BASIS = "local_operator_consent"
PERSONAL_USE_SCOPE = "public-json"

SOURCE_URLS = {
    "reliability_leaderboard": f"{BASE_URL}/api/reliability/leaderboard",
    "cost_comparison": f"{BASE_URL}/api/cost-comparison",
    "single_model_benchmarks": f"{BASE_URL}/api/single-model/benchmarks",
}

_SNAPSHOT_ID_RE = re.compile(r"^ai-frontier-v1-[0-9a-f]{16}$")
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
_PAYLOAD_ALIASES = {
    "reliability_leaderboard": "reliability_leaderboard",
    "reliability/leaderboard": "reliability_leaderboard",
    "/api/reliability/leaderboard": "reliability_leaderboard",
    "cost_comparison": "cost_comparison",
    "cost-comparison": "cost_comparison",
    "/api/cost-comparison": "cost_comparison",
    "model_benchmarks": "model_benchmarks",
    "benchmarks": "model_benchmarks",
}


class AIFrontierProviderError(ValueError):
    """Raised when a public-source document violates this provider contract."""


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
        raise AIFrontierProviderError(f"{label} must be a JSON object")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise AIFrontierProviderError(f"{label} must be a JSON array")
    return value


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _source_id(value: object) -> str | None:
    text = _text(value)
    return text.casefold() if text else None


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise AIFrontierProviderError(f"{label} must be a finite number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise AIFrontierProviderError(f"{label} must be a finite number") from exc
    else:
        raise AIFrontierProviderError(f"{label} must be a finite number")
    if not math.isfinite(number):
        raise AIFrontierProviderError(f"{label} must be a finite number")
    return number


def _nonnegative_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise AIFrontierProviderError(f"{label} must not be negative")
    return number


def _optional_finite_number(value: object, label: str) -> float | None:
    return None if value is None else _finite_number(value, label)


def _optional_nonnegative_number(value: object, label: str) -> float | None:
    return None if value is None else _nonnegative_number(value, label)


def _parse_timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _receipt_contains_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_is_sensitive_key(key) or _receipt_contains_secret(item) for key, item in value.items())
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
        return None, "personal-use consent receipt is missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "personal-use consent receipt is unreadable"
    if not isinstance(raw, Mapping):
        return None, "personal-use consent receipt must be a JSON object"
    if _receipt_contains_secret(raw):
        return None, "personal-use consent receipt must not contain secret material"

    schema = _text(raw.get("schema"))
    version = raw.get("version", raw.get("schema_version"))
    provider = _source_id(raw.get("provider"))
    status = _source_id(raw.get("status"))
    scope = _normalise_scope(raw.get("scope"))
    basis = _source_id(raw.get("basis"))
    accepted_at = _parse_timestamp(raw.get("accepted_at"))
    attribution = _text(raw.get("attribution"))
    if schema != AUTHORIZATION_SCHEMA or version != AUTHORIZATION_VERSION:
        return None, "personal-use consent receipt schema is unsupported"
    if provider != "ai-frontier" or status != "consented":
        return None, "personal-use consent receipt is not valid for ai-frontier"
    if basis != LOCAL_OPERATOR_CONSENT_BASIS or scope is None or PERSONAL_USE_SCOPE not in scope:
        return None, "personal-use consent receipt scope is invalid"
    if accepted_at is None:
        return None, "personal-use consent receipt accepted_at is invalid"
    if attribution is None or "aifrontier.withmartian.com" not in attribution.casefold():
        return None, "personal-use consent receipt attribution is invalid"
    if raw.get("not_official_authorization") is not True or _text(raw.get("terms_url")) != TERMS_URL:
        return None, "personal-use consent receipt must declare its non-official authorization boundary"
    return {
        "schema": schema,
        "version": AUTHORIZATION_VERSION,
        "provider": "ai-frontier",
        "status": "consented",
        "basis": LOCAL_OPERATOR_CONSENT_BASIS,
        "scope": scope,
        "accepted_at": _now_iso(accepted_at),
        "attribution": attribution,
        "terms_url": TERMS_URL,
        "not_official_authorization": True,
    }, None


def _atomic_write_document(path: Path, document: Mapping[str, object], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(document))
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_personal_use_consent(
    authorization_file: Path,
    *,
    accepted_at: datetime | None = None,
) -> dict[str, object]:
    """Persist a local operator's consent; it is not publisher authorization."""

    receipt = {
        "schema": AUTHORIZATION_SCHEMA,
        "version": AUTHORIZATION_VERSION,
        "provider": "ai-frontier",
        "status": "consented",
        "basis": LOCAL_OPERATOR_CONSENT_BASIS,
        "scope": [PERSONAL_USE_SCOPE],
        "accepted_at": _now_iso(accepted_at),
        "attribution": ATTRIBUTION,
        "terms_url": TERMS_URL,
        # Local consent never represents publisher authorization or a terms exception.
        "not_official_authorization": True,
    }
    _atomic_write_document(Path(authorization_file), receipt, mode=0o600)
    return receipt


def _ensure_finite_json(value: object, label: str = "payload") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise AIFrontierProviderError(f"{label} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _ensure_finite_json(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite_json(item, f"{label}[{index}]")


def _model_identity(value: object) -> dict[str, str]:
    raw = _text(value)
    if raw is None:
        raise AIFrontierProviderError("model source identifier must be non-empty")
    source_id = raw.casefold()
    model_id = source_id
    provider = "other"
    for prefix, candidate_provider in (("anthropic/", "claude"), ("anthropic:", "claude"), ("openai/", "codex"), ("openai:", "codex")):
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix) :]
            provider = candidate_provider
            break
    if provider == "other":
        if model_id.startswith("claude-") or "/claude-" in model_id:
            provider = "claude"
        elif model_id.startswith("gpt-") or "codex" in model_id:
            provider = "codex"
    return {"source_id": source_id, "provider": provider, "model_id": model_id}


def _row_value(row: Mapping[str, object], *names: str) -> object | None:
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        if name in row:
            return row[name]
        candidate = lowered.get(name.casefold())
        if candidate is not None:
            return candidate
    return None


def _normalise_leaderboard(payload: object) -> tuple[list[dict[str, object]], dict[str, str]]:
    rows = _array(payload, "reliability leaderboard")
    if not rows:
        raise AIFrontierProviderError("reliability leaderboard must not be empty")
    records: list[dict[str, object]] = []
    source_names: dict[str, str] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, f"reliability leaderboard row {index}")
        executor = _text(_row_value(row, "Executor"))
        if executor is None:
            raise AIFrontierProviderError(f"reliability leaderboard row {index} lacks Executor")
        identity = _model_identity(executor)
        source_id = identity["source_id"]
        if source_id in source_names:
            raise AIFrontierProviderError(f"reliability leaderboard repeats Executor {executor!r}")
        source_names[source_id] = executor
        quality = _nonnegative_number(_row_value(row, "Quality"), f"leaderboard {executor} Quality")
        cost = _nonnegative_number(_row_value(row, "Cost"), f"leaderboard {executor} Cost")
        consistency = _nonnegative_number(
            _row_value(row, "Consistency"), f"leaderboard {executor} Consistency"
        )
        consistency_std = _optional_nonnegative_number(
            _row_value(row, "Consistency Std", "ConsistencyStd"),
            f"leaderboard {executor} Consistency Std",
        )
        records.append(
            {
                **identity,
                "source_executor": executor,
                "quality": quality,
                "cost": cost,
                "consistency": consistency,
                "consistency_std": consistency_std,
                "quality_semantics": "cross_benchmark_quality_not_success_rate",
                "consistency_semantics": "stability_not_success_rate",
                "cost_semantics": "publisher_defined_relative_cost",
                # A producer of observations is not a router.  Consumers must
                # combine this with their own capability and quota gates.
                "routing_eligible": False,
            }
        )
    return sorted(records, key=lambda item: str(item["source_id"])), source_names


def _normalise_cost_comparison(payload: object) -> list[dict[str, object]]:
    rows = _array(payload, "cost comparison")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, value in enumerate(rows):
        row = _mapping(value, f"cost comparison row {index}")
        llms = _text(_row_value(row, "LLMs", "LLM"))
        if llms is None:
            raise AIFrontierProviderError(f"cost comparison row {index} lacks LLMs")
        identity = _model_identity(llms)
        source_id = identity["source_id"]
        if source_id in seen:
            raise AIFrontierProviderError(f"cost comparison repeats LLMs {llms!r}")
        seen.add(source_id)
        records.append(
            {
                **identity,
                "source_llms": llms,
                "category": "cost-comparison",
                "category_key": "cost-comparison",
                "quality": None,
                "cost": _nonnegative_number(_row_value(row, "Real Cost"), f"cost comparison {llms} Real Cost"),
                "quoted_cost": _nonnegative_number(
                    _row_value(row, "Quoted Cost"), f"cost comparison {llms} Quoted Cost"
                ),
                "cost_surprise": _finite_number(
                    _row_value(row, "Cost Surprise"), f"cost comparison {llms} Cost Surprise"
                ),
                "quality_semantics": "not_collected",
                "cost_semantics": "publisher_defined_relative_cost",
            }
        )
    return sorted(records, key=lambda item: str(item["source_id"]))


def _benchmark_rows(payload: object, source_model: str) -> list[object]:
    if isinstance(payload, list):
        return payload
    body = _mapping(payload, f"benchmarks response for {source_model}")
    for key in ("benchmarks", "data", "results", "categories"):
        if key in body:
            return _array(body[key], f"benchmarks {key} for {source_model}")
    raise AIFrontierProviderError(f"benchmarks response for {source_model} lacks a benchmark array")


def _normalise_benchmarks(
    payloads: Mapping[str, object], source_names: Mapping[str, str]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw_source_id, payload in sorted(payloads.items()):
        source_id = _source_id(raw_source_id)
        if source_id is None or source_id not in source_names:
            raise AIFrontierProviderError("benchmarks were supplied for a model absent from the leaderboard")
        identity = _model_identity(source_names[source_id])
        for index, value in enumerate(_benchmark_rows(payload, source_names[source_id])):
            row = _mapping(value, f"benchmark {source_names[source_id]} row {index}")
            category = _text(_row_value(row, "Category", "Benchmark", "Name", "label", "id"))
            if category is None:
                raise AIFrontierProviderError(
                    f"benchmark {source_names[source_id]} row {index} lacks category"
                )
            category_key = _source_id(_row_value(row, "id", "key", "Category", "Benchmark", "Name", "label"))
            if category_key is None:
                raise AIFrontierProviderError(
                    f"benchmark {source_names[source_id]} row {index} has an invalid category key"
                )
            key = (source_id, category_key)
            if key in seen:
                raise AIFrontierProviderError(
                    f"benchmarks repeat category {category!r} for {source_names[source_id]!r}"
                )
            seen.add(key)
            quality_raw = _row_value(row, "Quality", "Score", "Performance")
            cost_raw = _row_value(row, "Cost", "Real Cost")
            quality = _optional_nonnegative_number(
                quality_raw, f"benchmark {source_names[source_id]} {category} Quality"
            )
            cost = _optional_nonnegative_number(cost_raw, f"benchmark {source_names[source_id]} {category} Cost")
            if quality is None and cost is None:
                raise AIFrontierProviderError(
                    f"benchmark {source_names[source_id]} {category} lacks Quality and Cost"
                )
            records.append(
                {
                    **identity,
                    "source_executor": source_names[source_id],
                    "category": category,
                    "category_key": category_key,
                    "quality": quality,
                    "cost": cost,
                    "quality_semantics": "benchmark_category_quality_not_success_rate",
                    "cost_semantics": "publisher_defined_relative_cost",
                }
            )
    return sorted(records, key=lambda item: (str(item["source_id"]), str(item["category_key"])))


def _normalise_payloads(payloads: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, value in payloads.items():
        key = _PAYLOAD_ALIASES.get(str(raw_key).strip().casefold())
        if key is None:
            raise AIFrontierProviderError(f"unexpected payload {raw_key!r}")
        if key in normalized:
            raise AIFrontierProviderError(f"payload {key!r} was supplied more than once")
        _ensure_finite_json(value, f"payload {key}")
        normalized[key] = value
    missing = {"reliability_leaderboard", "cost_comparison"} - set(normalized)
    if missing:
        raise AIFrontierProviderError(f"required payloads are missing: {', '.join(sorted(missing))}")
    benchmarks = normalized.get("model_benchmarks", {})
    if not isinstance(benchmarks, Mapping):
        raise AIFrontierProviderError("model_benchmarks must be an object keyed by source model identifier")
    normalized["model_benchmarks"] = benchmarks
    return normalized


def _detail_request_projection(
    *,
    requested_source_ids: Sequence[str],
    selected_source_ids: Sequence[str],
    skipped_source_ids: Sequence[str],
    benchmark_payloads: Mapping[str, object],
) -> dict[str, list[str]]:
    """Make optional-detail selection explicit without treating absence as failure."""

    requested = _normalise_requested_model_ids(requested_source_ids)
    selected = _normalise_requested_model_ids(selected_source_ids)
    skipped = _normalise_requested_model_ids(skipped_source_ids)
    if not requested and not selected and not skipped:
        selected = _normalise_requested_model_ids(
            [str(source_id) for source_id in benchmark_payloads]
        )
        requested = list(selected)
    if set(selected) & set(skipped) or set(requested) != set(selected) | set(skipped):
        raise AIFrontierProviderError("detail request projection does not partition requested source IDs")
    if set(selected) != {_source_id(source_id) for source_id in benchmark_payloads}:
        raise AIFrontierProviderError("detail request projection does not match fetched benchmark payloads")
    return {
        "requested_source_ids": sorted(requested),
        "selected_source_ids": sorted(selected),
        "skipped_source_ids": sorted(skipped),
    }


def _normalise_snapshot(
    payloads: Mapping[str, object],
    authorization: Mapping[str, object],
    *,
    fetched_at: str,
    stale_after_seconds: int,
    ingest_mode: str,
    requested_source_ids: Sequence[str] = (),
    selected_source_ids: Sequence[str] = (),
    skipped_source_ids: Sequence[str] = (),
) -> tuple[dict[str, object], dict[str, object]]:
    leaderboard, source_names = _normalise_leaderboard(payloads["reliability_leaderboard"])
    cost_categories = _normalise_cost_comparison(payloads["cost_comparison"])
    cost_by_source = {str(item["source_id"]): item for item in cost_categories}
    for model in leaderboard:
        cost_comparison = cost_by_source.get(str(model["source_id"]))
        if cost_comparison is not None:
            model["real_cost"] = cost_comparison["cost"]
            model["quoted_cost"] = cost_comparison["quoted_cost"]
            model["cost_surprise"] = cost_comparison["cost_surprise"]
            model["real_cost_semantics"] = "publisher_defined_relative_cost"
    benchmark_payloads = _mapping(payloads["model_benchmarks"], "model benchmarks")
    benchmark_categories = _normalise_benchmarks(benchmark_payloads, source_names)
    detail_request = _detail_request_projection(
        requested_source_ids=requested_source_ids,
        selected_source_ids=selected_source_ids,
        skipped_source_ids=skipped_source_ids,
        benchmark_payloads=benchmark_payloads,
    )
    categories = sorted(
        [*cost_categories, *benchmark_categories],
        key=lambda item: (str(item["source_id"]), str(item["category_key"])),
    )
    raw_payloads = {
        "reliability_leaderboard": payloads["reliability_leaderboard"],
        "cost_comparison": payloads["cost_comparison"],
        **{
            f"model_benchmarks:{source_id}": payload
            for source_id, payload in sorted(_mapping(payloads["model_benchmarks"], "model benchmarks").items())
        },
    }
    raw_payload_digest = _digest(raw_payloads)
    snapshot: dict[str, object] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": None,
        "digest": None,
        "source": {
            "key": SOURCE_KEY,
            "name": "Martian AI Frontier",
            "base_url": BASE_URL,
            "api_contract": "public JSON aggregate endpoints only",
            "remote_timestamp": None,
            "remote_version": None,
            "paper_url": PAPER_URL,
            "terms_url": TERMS_URL,
        },
        "source_urls": dict(SOURCE_URLS),
        "attribution": str(authorization["attribution"]),
        "authorization": dict(authorization),
        "ingest_mode": ingest_mode,
        "fetched_at": fetched_at,
        "models": leaderboard,
        "categories": categories,
        "detail_request": detail_request,
        "routing_boundary": {
            "frontier_oracle_collected": False,
            "frontier_oracle_used_for_routing": False,
            "model_observations_are_not_success_rates": True,
            "reason": "Aggregate Quality and Consistency remain source observations; routing is a consumer responsibility.",
        },
        "raw_payload_digest": raw_payload_digest,
        "cache": {"state": "fresh", "stale_after_seconds": stale_after_seconds},
    }
    identity = dict(snapshot)
    identity.pop("snapshot_id")
    identity.pop("digest")
    digest = _digest(identity)
    snapshot["digest"] = digest
    snapshot["snapshot_id"] = f"ai-frontier-v1-{digest[:16]}"
    raw_generation = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "payload_digest": raw_payload_digest,
        "fetched_at": fetched_at,
        "payloads": raw_payloads,
    }
    return snapshot, raw_generation


def validate_ai_frontier_snapshot(snapshot: Mapping[str, object]) -> None:
    """Validate the portable, content-addressed AI Frontier snapshot contract."""

    if not isinstance(snapshot, Mapping):
        raise AIFrontierProviderError("ai-frontier snapshot must be a JSON object")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise AIFrontierProviderError("ai-frontier snapshot schema_version is unsupported")
    snapshot_id = _text(snapshot.get("snapshot_id"))
    digest = _text(snapshot.get("digest"))
    if snapshot_id is None or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise AIFrontierProviderError("ai-frontier snapshot_id is invalid")
    if digest is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AIFrontierProviderError("ai-frontier snapshot digest is invalid")
    identity = dict(snapshot)
    identity.pop("snapshot_id", None)
    identity.pop("digest", None)
    if digest != _digest(identity) or snapshot_id != f"ai-frontier-v1-{digest[:16]}":
        raise AIFrontierProviderError("ai-frontier snapshot identity does not match its content")
    if _parse_timestamp(snapshot.get("fetched_at")) is None:
        raise AIFrontierProviderError("ai-frontier snapshot fetched_at is invalid")
    source = _mapping(snapshot.get("source"), "ai-frontier snapshot source")
    if source.get("key") != SOURCE_KEY or source.get("remote_timestamp") is not None or source.get("remote_version") is not None:
        raise AIFrontierProviderError("ai-frontier source provenance is invalid")
    authorization = _mapping(snapshot.get("authorization"), "ai-frontier snapshot authorization")
    if _source_id(authorization.get("provider")) != "ai-frontier" or _source_id(authorization.get("status")) != "consented":
        raise AIFrontierProviderError("ai-frontier snapshot authorization is invalid")
    if _source_id(authorization.get("basis")) != LOCAL_OPERATOR_CONSENT_BASIS:
        raise AIFrontierProviderError("ai-frontier snapshot personal-use basis is invalid")
    scope = _normalise_scope(authorization.get("scope"))
    if scope is None or PERSONAL_USE_SCOPE not in scope or _parse_timestamp(authorization.get("accepted_at")) is None:
        raise AIFrontierProviderError("ai-frontier snapshot personal-use consent is invalid")
    if authorization.get("not_official_authorization") is not True or _text(authorization.get("terms_url")) != TERMS_URL:
        raise AIFrontierProviderError("ai-frontier snapshot non-official authorization boundary is invalid")
    cache = _mapping(snapshot.get("cache"), "ai-frontier snapshot cache")
    if cache.get("state") != "fresh" or not isinstance(cache.get("stale_after_seconds"), int) or cache["stale_after_seconds"] <= 0:
        raise AIFrontierProviderError("ai-frontier snapshot cache is invalid")
    boundary = _mapping(snapshot.get("routing_boundary"), "ai-frontier routing boundary")
    if boundary.get("frontier_oracle_collected") is not False or boundary.get("frontier_oracle_used_for_routing") is not False:
        raise AIFrontierProviderError("ai-frontier frontier/oracle boundary is invalid")
    detail_request = _mapping(snapshot.get("detail_request"), "ai-frontier detail request")
    detail_lists: dict[str, list[str]] = {}
    for key in ("requested_source_ids", "selected_source_ids", "skipped_source_ids"):
        values = detail_request.get(key)
        if not isinstance(values, list):
            raise AIFrontierProviderError(f"ai-frontier detail request {key} must be an array")
        normalized = _normalise_requested_model_ids(values)
        if normalized != sorted(normalized):
            raise AIFrontierProviderError(f"ai-frontier detail request {key} must be sorted")
        detail_lists[key] = normalized
    requested = set(detail_lists["requested_source_ids"])
    selected = set(detail_lists["selected_source_ids"])
    skipped = set(detail_lists["skipped_source_ids"])
    if selected & skipped or requested != selected | skipped:
        raise AIFrontierProviderError("ai-frontier detail request does not partition requested source IDs")
    models = _array(snapshot.get("models"), "ai-frontier snapshot models")
    if not models:
        raise AIFrontierProviderError("ai-frontier snapshot models must not be empty")
    seen_models: set[str] = set()
    for index, value in enumerate(models):
        model = _mapping(value, f"ai-frontier model {index}")
        source_id = _source_id(model.get("source_id"))
        if source_id is None or source_id in seen_models:
            raise AIFrontierProviderError("ai-frontier model source_id is invalid or repeated")
        seen_models.add(source_id)
        if model.get("provider") not in {"codex", "claude", "other"} or _text(model.get("model_id")) is None:
            raise AIFrontierProviderError("ai-frontier model identity is invalid")
        if model.get("routing_eligible") is not False:
            raise AIFrontierProviderError("ai-frontier model must not claim routing eligibility")
        for field in (
            "quality",
            "cost",
            "real_cost",
            "quoted_cost",
            "consistency",
            "consistency_std",
        ):
            if field in model and model[field] is not None:
                _nonnegative_number(model[field], f"ai-frontier model {index} {field}")
        if model.get("cost_surprise") is not None:
            _finite_number(model["cost_surprise"], f"ai-frontier model {index} cost_surprise")
    if not selected <= seen_models:
        raise AIFrontierProviderError("ai-frontier detail request selected an absent leaderboard model")
    categories = _array(snapshot.get("categories"), "ai-frontier snapshot categories")
    seen_categories: set[tuple[str, str]] = set()
    for index, value in enumerate(categories):
        category = _mapping(value, f"ai-frontier category {index}")
        source_id = _source_id(category.get("source_id"))
        category_key = _source_id(category.get("category_key"))
        if source_id is None or category_key is None or (source_id, category_key) in seen_categories:
            raise AIFrontierProviderError("ai-frontier category identity is invalid or repeated")
        seen_categories.add((source_id, category_key))
        for field in ("quality", "cost", "quoted_cost"):
            if category.get(field) is not None:
                _nonnegative_number(category[field], f"ai-frontier category {index} {field}")
        if category.get("cost_surprise") is not None:
            _finite_number(category["cost_surprise"], f"ai-frontier category {index} cost_surprise")


class AIFrontierRegistry:
    """SQLite-backed public observations with a fail-closed, offline LKG API."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self._database_path = self.state_root / "ai-frontier.sqlite3"

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _ensure_state_root(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)

    def _connection(self) -> sqlite3.Connection:
        self._ensure_state_root()
        connection = sqlite3.connect(str(self._database_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            os.chmod(self._database_path, 0o600)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > DATABASE_SCHEMA_VERSION:
                raise AIFrontierProviderError("ai-frontier database schema is newer than this provider")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_frontier_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    ingest_mode TEXT NOT NULL,
                    authorization_status TEXT NOT NULL,
                    authorization_accepted_at TEXT NOT NULL,
                    snapshot_document TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_frontier_raw_payloads (
                    snapshot_id TEXT NOT NULL REFERENCES ai_frontier_snapshots(snapshot_id) ON DELETE CASCADE,
                    payload_name TEXT NOT NULL,
                    payload_document TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, payload_name)
                );
                CREATE TABLE IF NOT EXISTS ai_frontier_models (
                    snapshot_id TEXT NOT NULL REFERENCES ai_frontier_snapshots(snapshot_id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    model_document TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS ai_frontier_categories (
                    snapshot_id TEXT NOT NULL REFERENCES ai_frontier_snapshots(snapshot_id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    category_key TEXT NOT NULL,
                    category_document TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, source_id, category_key)
                );
                CREATE TABLE IF NOT EXISTS ai_frontier_active (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    snapshot_id TEXT NOT NULL REFERENCES ai_frontier_snapshots(snapshot_id),
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ai_frontier_models_source_id_idx
                    ON ai_frontier_models(source_id);
                """
            )
            if current_version < DATABASE_SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def _database_status(self) -> dict[str, object]:
        table_names = (
            "ai_frontier_snapshots",
            "ai_frontier_raw_payloads",
            "ai_frontier_models",
            "ai_frontier_categories",
            "ai_frontier_active",
        )
        result: dict[str, object] = {
            "backend": "sqlite",
            "schema_version": DATABASE_SCHEMA_VERSION,
            "path": str(self.database_path),
            "row_counts": {name: 0 for name in table_names},
        }
        try:
            connection = self._connection()
            try:
                result["row_counts"] = {
                    name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                    for name in table_names
                }
            finally:
                connection.close()
        except (OSError, sqlite3.Error, AIFrontierProviderError) as exc:
            result.update({"ok": False, "error": _safe_error(exc)})
        else:
            result["ok"] = True
        return result

    @staticmethod
    def _document_from_json(value: object) -> dict[str, object] | None:
        if not isinstance(value, str):
            return None
        try:
            document = json.loads(value)
            if not isinstance(document, dict):
                return None
            validate_ai_frontier_snapshot(document)
            return document
        except (UnicodeDecodeError, json.JSONDecodeError, AIFrontierProviderError):
            return None

    def _database_generation(self, snapshot_id: str) -> dict[str, object] | None:
        try:
            connection = self._connection()
            try:
                row = connection.execute(
                    "SELECT snapshot_document FROM ai_frontier_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, AIFrontierProviderError):
            return None
        return self._document_from_json(row["snapshot_document"]) if row is not None else None

    def active(self) -> dict[str, object] | None:
        try:
            connection = self._connection()
            try:
                row = connection.execute(
                    """
                    SELECT snapshots.snapshot_document
                    FROM ai_frontier_active AS active
                    JOIN ai_frontier_snapshots AS snapshots ON snapshots.snapshot_id = active.snapshot_id
                    WHERE active.slot = 1
                    """
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, AIFrontierProviderError):
            return None
        return self._document_from_json(row["snapshot_document"]) if row is not None else None

    def consent_personal_use(
        self, authorization_file: Path | None = None, *, accepted_at: datetime | None = None
    ) -> dict[str, object]:
        self._ensure_state_root()
        target = Path(authorization_file) if authorization_file is not None else self.state_root / "authorization.json"
        receipt = write_personal_use_consent(target, accepted_at=accepted_at)
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "provider": PROVIDER_NAME,
            "ok": True,
            "operation": "consent-personal-use",
            "state": "consented",
            "status": "consented",
            "network_requested": False,
            "authorization_file": str(target),
            "receipt": receipt,
        }

    def authorization_status(self, authorization_file: Path) -> dict[str, object]:
        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "provider": PROVIDER_NAME,
                "ok": False,
                "status": "unauthorized",
                "error": error or "personal-use consent receipt is invalid",
            }
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "provider": PROVIDER_NAME,
            "ok": True,
            "status": "consented",
            "receipt": authorization,
        }

    def load_generation(self, snapshot_id: str) -> dict[str, object] | None:
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            return None
        return self._database_generation(snapshot_id)

    def status(self, now: datetime | None = None) -> dict[str, object]:
        snapshot = self.active()
        database = self._database_status()
        if snapshot is None:
            return {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "provider": PROVIDER_NAME,
                "ok": False,
                "state": "unavailable",
                "cache_status": "unavailable",
                "snapshot_id": None,
                "digest": None,
                "snapshot": None,
                "authorization_status": "unauthorized",
                "policy_state": "disabled_by_policy",
                "database": database,
            }
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        fetched_at = _parse_timestamp(snapshot.get("fetched_at"))
        cache = _mapping(snapshot["cache"], "ai-frontier snapshot cache")
        stale_after = int(cache["stale_after_seconds"])
        age_seconds = (
            max(0, int((checked_at.astimezone(UTC) - fetched_at).total_seconds()))
            if fetched_at is not None
            else stale_after + 1
        )
        state = "fresh" if age_seconds <= stale_after else "stale"
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "provider": PROVIDER_NAME,
            "ok": state == "fresh",
            "state": state,
            "cache_status": "cache" if state == "fresh" else "stale-cache",
            "snapshot_id": snapshot["snapshot_id"],
            "digest": snapshot["digest"],
            "fetched_at": snapshot["fetched_at"],
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after,
            "authorization_status": "consented",
            "detail_request": snapshot.get("detail_request"),
            "snapshot": snapshot,
            "database": database,
        }

    def refresh(
        self,
        authorization_file: Path,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        minimum_refresh_interval_seconds: int = DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
        model_source_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Fetch two public aggregates and explicitly selected model benchmarks once.

        There are deliberately no retries.  A scheduler may call this process at
        a low frequency, but the registry applies its own 24-hour lower bound so
        a misconfigured scheduler cannot create a request storm.
        """

        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return self._failure(
                "refresh",
                error or "personal-use consent receipt is invalid",
                authorization_status="unauthorized",
                policy_state="disabled_by_policy",
            )
        try:
            requested = _normalise_requested_model_ids(model_source_ids)
        except AIFrontierProviderError as exc:
            return self._failure("refresh", str(exc))
        if timeout_seconds <= 0 or stale_after_seconds <= 0:
            return self._failure("refresh", "refresh timing values are invalid")
        if minimum_refresh_interval_seconds < HARD_MINIMUM_REFRESH_INTERVAL_SECONDS:
            return self._failure(
                "refresh",
                f"minimum_refresh_interval_seconds must be at least {HARD_MINIMUM_REFRESH_INTERVAL_SECONDS}",
            )
        existing = self.active()
        if existing is not None:
            fetched_at = _parse_timestamp(existing.get("fetched_at"))
            if fetched_at is not None:
                age = (datetime.now(UTC) - fetched_at).total_seconds()
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
            payloads: dict[str, object] = {
                "reliability_leaderboard": self._fetch_json(
                    SOURCE_URLS["reliability_leaderboard"], timeout_seconds
                ),
                "cost_comparison": self._fetch_json(SOURCE_URLS["cost_comparison"], timeout_seconds),
            }
            _, source_names = _normalise_leaderboard(payloads["reliability_leaderboard"])
            selected: dict[str, object] = {}
            skipped: list[str] = []
            for source_id in requested:
                executor = source_names.get(source_id)
                if executor is None:
                    skipped.append(source_id)
                    continue
                query = urlencode({"llm_name": executor})
                selected[source_id] = self._fetch_json(
                    f"{SOURCE_URLS['single_model_benchmarks']}?{query}", timeout_seconds
                )
            payloads["model_benchmarks"] = selected
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AIFrontierProviderError) as exc:
            return self._failure("refresh", _safe_error(exc), network_requested=True)
        return self._ingest(
            payloads,
            authorization,
            stale_after_seconds=stale_after_seconds,
            ingest_mode="refresh",
            fetched_at=None,
            network_requested=True,
            requested_source_ids=requested,
            selected_source_ids=list(selected),
            skipped_source_ids=skipped,
        )

    def import_payloads(
        self,
        payloads: Mapping[str, object],
        authorization_file: Path,
        *,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        fetched_at: str | datetime | None = None,
    ) -> dict[str, object]:
        """Store authorized local JSON fixtures without making network requests."""

        authorization, error = _authorization_metadata(Path(authorization_file))
        if authorization is None:
            return self._failure(
                "import",
                error or "personal-use consent receipt is invalid",
                authorization_status="unauthorized",
                policy_state="disabled_by_policy",
            )
        if stale_after_seconds <= 0:
            return self._failure("import", "stale_after_seconds must be positive")
        return self._ingest(
            payloads,
            authorization,
            stale_after_seconds=stale_after_seconds,
            ingest_mode="import",
            fetched_at=fetched_at,
            network_requested=False,
        )

    @staticmethod
    def _fetch_json(url: str, timeout_seconds: int) -> object:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        response = urlopen(request, timeout=float(timeout_seconds))
        try:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if len(payload) > MAX_RESPONSE_BYTES:
            raise AIFrontierProviderError("response body exceeds the configured limit")
        return json.loads(payload.decode("utf-8"))

    def _ingest(
        self,
        payloads: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        stale_after_seconds: int,
        ingest_mode: str,
        fetched_at: str | datetime | None,
        network_requested: bool,
        requested_source_ids: Sequence[str] = (),
        selected_source_ids: Sequence[str] = (),
        skipped_source_ids: Sequence[str] = (),
    ) -> dict[str, object]:
        try:
            normalized_payloads = _normalise_payloads(payloads)
            if isinstance(fetched_at, datetime):
                recorded_at = _now_iso(fetched_at)
            elif fetched_at is None:
                recorded_at = _now_iso()
            else:
                timestamp = _parse_timestamp(fetched_at)
                if timestamp is None:
                    raise AIFrontierProviderError("fetched_at must be an ISO-8601 timestamp")
                recorded_at = _now_iso(timestamp)
            snapshot, raw_generation = _normalise_snapshot(
                normalized_payloads,
                authorization,
                fetched_at=recorded_at,
                stale_after_seconds=stale_after_seconds,
                ingest_mode=ingest_mode,
                requested_source_ids=requested_source_ids,
                selected_source_ids=selected_source_ids,
                skipped_source_ids=skipped_source_ids,
            )
            validate_ai_frontier_snapshot(snapshot)
            self._persist_database(snapshot, raw_generation)
        except (OSError, sqlite3.Error, TypeError, ValueError, AIFrontierProviderError) as exc:
            return self._failure(ingest_mode, _safe_error(exc), network_requested=network_requested)
        result = self.status()
        result.update(
            {
                "operation": ingest_mode,
                "network_requested": network_requested,
                "generation_created": True,
            }
        )
        return result

    def _persist_database(
        self, snapshot: Mapping[str, object], raw_generation: Mapping[str, object]
    ) -> None:
        """Persist raw, normalized, snapshot, and active pointer in one transaction."""

        validate_ai_frontier_snapshot(snapshot)
        snapshot_id = str(snapshot["snapshot_id"])
        authorization = _mapping(snapshot["authorization"], "ai-frontier authorization")
        models = _array(snapshot["models"], "ai-frontier models")
        categories = _array(snapshot["categories"], "ai-frontier categories")
        raw_payloads = _mapping(raw_generation.get("payloads"), "ai-frontier raw payloads")
        connection = self._connection()
        try:
            with connection:
                existing = connection.execute(
                    "SELECT digest FROM ai_frontier_snapshots WHERE snapshot_id = ?", (snapshot_id,)
                ).fetchone()
                if existing is not None and existing["digest"] != snapshot["digest"]:
                    raise AIFrontierProviderError("ai-frontier snapshot ID conflicts with a different digest")
                connection.execute(
                    """
                    INSERT INTO ai_frontier_snapshots (
                        snapshot_id, digest, schema_version, fetched_at, ingest_mode,
                        authorization_status, authorization_accepted_at, snapshot_document, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                        digest = excluded.digest,
                        schema_version = excluded.schema_version,
                        fetched_at = excluded.fetched_at,
                        ingest_mode = excluded.ingest_mode,
                        authorization_status = excluded.authorization_status,
                        authorization_accepted_at = excluded.authorization_accepted_at,
                        snapshot_document = excluded.snapshot_document
                    """,
                    (
                        snapshot_id,
                        str(snapshot["digest"]),
                        int(snapshot["schema_version"]),
                        str(snapshot["fetched_at"]),
                        str(snapshot["ingest_mode"]),
                        str(authorization["status"]),
                        str(authorization["accepted_at"]),
                        _canonical_json(snapshot),
                        _now_iso(),
                    ),
                )
                connection.execute("DELETE FROM ai_frontier_raw_payloads WHERE snapshot_id = ?", (snapshot_id,))
                connection.executemany(
                    """
                    INSERT INTO ai_frontier_raw_payloads (snapshot_id, payload_name, payload_document)
                    VALUES (?, ?, ?)
                    """,
                    [(snapshot_id, str(name), _canonical_json(payload)) for name, payload in sorted(raw_payloads.items())],
                )
                connection.execute("DELETE FROM ai_frontier_models WHERE snapshot_id = ?", (snapshot_id,))
                connection.executemany(
                    """
                    INSERT INTO ai_frontier_models (snapshot_id, source_id, model_document)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (snapshot_id, str(_mapping(model, "ai-frontier model")["source_id"]), _canonical_json(model))
                        for model in models
                    ],
                )
                connection.execute("DELETE FROM ai_frontier_categories WHERE snapshot_id = ?", (snapshot_id,))
                connection.executemany(
                    """
                    INSERT INTO ai_frontier_categories (
                        snapshot_id, source_id, category_key, category_document
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            snapshot_id,
                            str(_mapping(category, "ai-frontier category")["source_id"]),
                            str(_mapping(category, "ai-frontier category")["category_key"]),
                            _canonical_json(category),
                        )
                        for category in categories
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO ai_frontier_active (slot, snapshot_id, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(slot) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (snapshot_id, _now_iso()),
                )
        finally:
            connection.close()

    def _failure(
        self,
        operation: str,
        error: str,
        *,
        authorization_status: str | None = None,
        network_requested: bool = False,
        policy_state: str | None = None,
    ) -> dict[str, object]:
        result = self.status()
        result.update(
            {
                "ok": False,
                "operation": operation,
                "network_requested": network_requested,
                "generation_created": False,
                "last_error": error,
            }
        )
        if authorization_status is not None:
            result["authorization_status"] = authorization_status
        if policy_state is not None:
            result["policy_state"] = policy_state
        return result


def _normalise_requested_model_ids(model_source_ids: Sequence[str] | None) -> list[str]:
    if model_source_ids is None:
        return []
    if isinstance(model_source_ids, (str, bytes)):
        raise AIFrontierProviderError("model_source_ids must be a sequence of model identifiers")
    result: list[str] = []
    seen: set[str] = set()
    for value in model_source_ids:
        source_id = _source_id(value)
        if source_id is None:
            raise AIFrontierProviderError("model_source_ids contains an invalid model identifier")
        if source_id in seen:
            raise AIFrontierProviderError("model_source_ids contains a duplicate model identifier")
        seen.add(source_id)
        result.append(source_id)
    if len(result) > MAX_MODEL_BENCHMARKS:
        raise AIFrontierProviderError(
            f"model_source_ids may contain at most {MAX_MODEL_BENCHMARKS} models"
        )
    return result


def _safe_error(error: object) -> str:
    if isinstance(error, URLError):
        return f"network request failed ({error.__class__.__name__})"
    if isinstance(error, AIFrontierProviderError):
        return str(error)
    if isinstance(error, (UnicodeDecodeError, json.JSONDecodeError)):
        return f"payload parsing failed ({error.__class__.__name__})"
    return f"refresh failed ({error.__class__.__name__})"
