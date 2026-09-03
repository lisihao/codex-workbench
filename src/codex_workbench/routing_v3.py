"""Capability-driven, deterministic routing policy for new Workbench tasks.

This module deliberately has no dependency on the durable catalog implementation.
It accepts plain mappings (or small ``to_dict`` objects) so the catalog can evolve
without making a task's routing policy depend on a live CLI probe at execution
time.  A caller pins the returned ``catalog_snapshot_id`` and
``catalog_digest`` into its task contract before dispatch.

The policy is intentionally conservative: a capability is not admitted merely
because a model name looks familiar.  It must be active, routable, available at
runtime, explicitly declare the requested capability, and meet all resource and
quality constraints.  Cost is a tie-breaker only after the acceptance-quality
ordering is satisfied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
import json
import math
from typing import Any, Literal


ROUTING_V3_POLICY_VERSION = "model-routing-v3"
RoutingV3Role = Literal["planner", "worker", "verifier", "challenge", "control"]

_ACTIVE_STATUSES = frozenset({"active", "available", "current", "verified"})
_CLAUDE_PROVIDERS = frozenset({"claude", "anthropic"})
_WORKER_TASK_TYPES = frozenset(
    {"implementation", "debugging", "tests", "docs", "exploration"}
)
_ARCHITECTURE_TASK_TYPES = frozenset({"architecture", "review", "research", "creative"})
_VALID_ROLES = frozenset({"planner", "worker", "verifier", "challenge", "control"})

# Catalogs may report qualitative policy classes before there is enough local
# benchmark evidence for a measured value.  These are *ordering ordinals*, not
# performance claims: they let a known policy stay deterministic while the
# registry keeps the original class alongside the route receipt.
_QUALITY_ORDINAL = {
    "unknown": 0.0,
    "focused-mechanical": 60.0,
    "production": 80.0,
    "frontier": 100.0,
}
_COST_ORDINAL = {
    "unknown": 1_000_000.0,
    "lowest": 1.0,
    "efficient": 2.0,
    "balanced": 3.0,
    "high": 4.0,
    "highest": 5.0,
}
_LATENCY_ORDINAL = {
    "unknown": 1_000_000.0,
    "fastest": 1.0,
    "fast": 2.0,
    "balanced": 3.0,
    "deliberate": 4.0,
}
_THROUGHPUT_ORDINAL = {"unknown": 0.0, "high": 4.0, "medium": 2.0, "control-plane": 1.0}


@dataclass(frozen=True)
class RejectedCandidate:
    """A catalog capability which was considered but did not pass a hard gate."""

    provider: str
    model: str
    capability_id: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "capability_id": self.capability_id,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RankedCandidate:
    """One admissible capability and the observable inputs behind its ranking."""

    rank: int
    provider: str
    model: str
    capability_id: str
    capability_digest: str
    quality_score: float
    quality_floor: float
    acceptance_risk: str
    estimated_cost_units: float
    estimated_latency_ms: float
    estimated_throughput: float
    concurrency_utilization: float
    preference_rank: int
    score: tuple[float | str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "provider": self.provider,
            "model": self.model,
            "capability_id": self.capability_id,
            "capability_digest": self.capability_digest,
            "quality_score": self.quality_score,
            "quality_floor": self.quality_floor,
            "acceptance_risk": self.acceptance_risk,
            "estimated_cost_units": self.estimated_cost_units,
            "estimated_latency_ms": self.estimated_latency_ms,
            "estimated_throughput": self.estimated_throughput,
            "concurrency_utilization": self.concurrency_utilization,
            "preference_rank": self.preference_rank,
            "score": list(self.score),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RoutingV3Decision:
    """Serializable capability-routing result for one pinned catalog snapshot."""

    accepted: bool
    policy_version: str
    catalog_snapshot_id: str
    catalog_digest: str
    reason: str
    selected: RankedCandidate | None
    ranked_candidates: tuple[RankedCandidate, ...]
    parallel_candidates: tuple[RankedCandidate, ...]
    rejected_candidates: tuple[RejectedCandidate, ...]

    @property
    def model(self) -> str | None:
        return self.selected.model if self.selected is not None else None

    @property
    def provider(self) -> str | None:
        return self.selected.provider if self.selected is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "policy_version": self.policy_version,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "catalog_digest": self.catalog_digest,
            "reason": self.reason,
            "selected": self.selected.to_dict() if self.selected is not None else None,
            "ranked_candidates": [candidate.to_dict() for candidate in self.ranked_candidates],
            "parallel_candidates": [candidate.to_dict() for candidate in self.parallel_candidates],
            "rejected_candidates": [candidate.to_dict() for candidate in self.rejected_candidates],
        }


# Short aliases make the module pleasant to use from a future planner without
# coupling that planner to legacy ``routing.py`` classes.
RouteV3Decision = RoutingV3Decision


def route_capability_snapshot(
    snapshot: Mapping[str, Any] | Any,
    request: Mapping[str, Any] | Any,
    *,
    active_model_ids: Iterable[str] = (),
    policy_version: str = ROUTING_V3_POLICY_VERSION,
) -> RoutingV3Decision:
    """Route ``request`` against an immutable capability ``snapshot``.

    Supported snapshot keys are deliberately modest and serializable:

    ``snapshot_id``/``generation``, ``digest``, ``capabilities`` (or ``models``),
    and optional ``provider_runtime``/``claude_quota``.  Each capability declares
    provider, model, status, routable/runtime availability, supported roles/task
    types/complexities/features/efforts, quality, cost, latency, throughput, and
    concurrency.  Synonymous field names are accepted so catalog schema updates
    can be adapted at the boundary rather than by this policy.
    """

    catalog = _as_mapping(snapshot, label="capability snapshot")
    requested = _as_mapping(request, label="routing request")
    role = _request_role(requested)
    task_type = _text(requested.get("task_type"), default="implementation")
    complexity = _text(requested.get("complexity"), default="standard")
    quality_floor = _quality_number(requested.get("quality_floor"), default=0.0)
    required_features = _string_set(requested.get("required_features", ()))
    reasoning_effort = _optional_text(requested.get("reasoning_effort"))
    risk = _text(requested.get("acceptance_risk"), default="standard")
    active = frozenset(_text(value) for value in active_model_ids)

    snapshot_id = _snapshot_id(catalog)
    catalog_digest = _catalog_digest(catalog)
    capabilities = _capability_records(catalog)
    admitted: list[dict[str, Any]] = []
    rejected: list[RejectedCandidate] = []

    for raw_record in capabilities:
        record = _as_mapping(raw_record, label="capability record")
        provider, model, capability_id = _identity(record)
        reasons = _hard_gate_reasons(
            catalog,
            record,
            requested,
            role=role,
            task_type=task_type,
            complexity=complexity,
            quality_floor=quality_floor,
            required_features=required_features,
            reasoning_effort=reasoning_effort,
            active_model_ids=active,
        )
        if reasons:
            rejected.append(
                RejectedCandidate(
                    provider=provider,
                    model=model,
                    capability_id=capability_id,
                    reasons=tuple(sorted(reasons)),
                )
            )
            continue
        admitted.append(
            _candidate_inputs(
                record,
                provider=provider,
                model=model,
                capability_id=capability_id,
                quality_floor=quality_floor,
                acceptance_risk=risk,
                snapshot=catalog,
                request=requested,
                active_model_ids=active,
            )
        )

    # Quality is intentionally the first sort key.  This makes it impossible
    # for a lower-quality candidate to win purely because it is cheap; cost,
    # latency, throughput, and current pool pressure only decide among equal
    # acceptance-quality candidates.
    admitted.sort(key=_candidate_sort_key)
    ranked = tuple(
        RankedCandidate(rank=index, **candidate)
        for index, candidate in enumerate(admitted, start=1)
    )
    rejected = tuple(
        sorted(rejected, key=lambda item: (item.provider, item.model, item.capability_id, item.reasons))
    )
    selected = ranked[0] if ranked else None
    parallel = _parallel_provider_candidates(ranked, requested)
    if selected is None:
        reason = "no capability passed routing-v3 hard gates"
    else:
        reason = (
            f"selected {selected.provider}:{selected.model}; acceptance quality "
            f"{selected.quality_score:g} is ranked before cost and latency"
        )
    return RoutingV3Decision(
        accepted=selected is not None,
        policy_version=policy_version,
        catalog_snapshot_id=snapshot_id,
        catalog_digest=catalog_digest,
        reason=reason,
        selected=selected,
        ranked_candidates=ranked,
        parallel_candidates=parallel,
        rejected_candidates=rejected,
    )


def route_v3(
    snapshot: Mapping[str, Any] | Any,
    request: Mapping[str, Any] | Any,
    **kwargs: Any,
) -> RoutingV3Decision:
    """Compatibility-friendly short name for :func:`route_capability_snapshot`."""

    return route_capability_snapshot(snapshot, request, **kwargs)


def _as_mapping(value: Mapping[str, Any] | Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return dict(mapped)
    if is_dataclass(value) and not isinstance(value, type):
        mapped = asdict(value)
        if isinstance(mapped, Mapping):
            return dict(mapped)
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    raise TypeError(f"{label} must be a mapping or expose to_dict()")


def _request_role(request: Mapping[str, Any]) -> RoutingV3Role:
    raw = _text(request.get("role"), default="worker")
    if raw == "cross_module_control":
        raw = "control"
    if raw not in _VALID_ROLES:
        raise ValueError(f"unsupported routing-v3 role: {raw!r}")
    return raw  # type: ignore[return-value]


def _capability_records(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
    for key in ("capabilities", "models", "records"):
        value = snapshot.get(key)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            return tuple(value)
    raise ValueError("capability snapshot must contain a capabilities sequence")


def _identity(record: Mapping[str, Any]) -> tuple[str, str, str]:
    provider = _text(record.get("provider"), default="unknown")
    model = _text(
        _first(record, "model", "model_id", "model_name", "id"),
        default="unknown",
    )
    capability_id = _text(
        _first(record, "capability_id", "record_id", "id"),
        default=f"{provider}:{model}",
    )
    return provider, model, capability_id


def _hard_gate_reasons(
    snapshot: Mapping[str, Any],
    record: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    role: RoutingV3Role,
    task_type: str,
    complexity: str,
    quality_floor: float,
    required_features: frozenset[str],
    reasoning_effort: str | None,
    active_model_ids: frozenset[str],
) -> list[str]:
    provider, model, _ = _identity(record)
    reasons: list[str] = []
    status = _text(record.get("status"), default="unknown")
    if status not in _ACTIVE_STATUSES:
        reasons.append(f"capability status {status!r} is not routable")
    if _truthy(record.get("deprecated")):
        reasons.append("capability is deprecated")
    if not _truthy(_first(record, "routable", "routing_enabled", "admitted")):
        reasons.append("capability is not marked routable")
    if not _provider_runtime_available(snapshot, record, provider):
        reasons.append(f"provider runtime {provider!r} is unavailable")
    if not _capability_runtime_available(record, status=status):
        reasons.append("capability runtime is unavailable")

    _require_role_support(reasons, record, role)
    _require_task_type_support(
        reasons,
        record,
        task_type,
    )
    _require_declared_support(
        reasons,
        record,
        ("complexities", "supported_complexities"),
        complexity,
        "complexity",
        required=False,
    )
    _require_features(reasons, record, required_features)
    if reasoning_effort is not None:
        _require_declared_support(
            reasons,
            record,
            (
                "reasoning_efforts",
                "supported_reasoning_efforts",
                "reasoning_effort",
                "reasoning",
            ),
            reasoning_effort,
            "reasoning effort",
        )

    quality = _record_quality(record)
    if quality is None:
        reasons.append("capability does not declare a quality score")
    elif quality < quality_floor:
        reasons.append(
            f"quality {quality:g} is below required quality floor {quality_floor:g}"
        )
    _tier_policy_reasons(reasons, record, request, role, task_type, complexity, model)
    if provider in _CLAUDE_PROVIDERS:
        _claude_quota_reasons(reasons, snapshot, request, record, model)
    _concurrency_reasons(reasons, snapshot, request, record, model, active_model_ids)
    return reasons


def _require_declared_support(
    reasons: list[str],
    record: Mapping[str, Any],
    aliases: tuple[str, ...],
    requested: str,
    label: str,
    *,
    required: bool = True,
) -> None:
    values = _declared_values(record, aliases)
    if not values:
        if required:
            reasons.append(f"capability does not declare {label} support")
    elif "*" not in values and requested not in values:
        reasons.append(f"capability does not support {label} {requested!r}")


def _require_role_support(
    reasons: list[str], record: Mapping[str, Any], role: RoutingV3Role
) -> None:
    values = _declared_values(record, ("roles", "supported_roles", "task_roles"))
    compatible = {
        "planner": {"planner"},
        "verifier": {"verifier"},
        # Current capability observations call cross-module control work
        # architecture/research.  Tier policy below still restricts this to
        # Sol, so this alias cannot widen another provider's authority.
        "control": {"control", "architecture", "research"},
        "worker": {"worker", "reviewer"},
        "challenge": {"challenge", "architecture_challenge", "reviewer", "research"},
    }[role]
    if not values:
        reasons.append("capability does not declare role support")
    elif "*" not in values and not (values & compatible):
        reasons.append(f"capability does not support role {role!r}")


def _require_task_type_support(
    reasons: list[str], record: Mapping[str, Any], task_type: str
) -> None:
    values = _declared_values(record, ("task_types", "supported_task_types"))
    roles = _declared_values(record, ("roles", "supported_roles", "task_roles"))
    # The passive registry exposes research as a role capability because it is
    # discovered from agent features rather than a model-selection command.
    if task_type == "research" and "research" in roles:
        return
    if not values:
        reasons.append("capability does not declare task type support")
    elif "*" not in values and task_type not in values:
        reasons.append(f"capability does not support task type {task_type!r}")


def _require_features(
    reasons: list[str],
    record: Mapping[str, Any],
    required_features: frozenset[str],
) -> None:
    if not required_features:
        return
    supported = _feature_set(
        _first(record, "features", "supported_features", "capabilities")
    )
    if "*" in supported:
        return
    missing = sorted(required_features - supported)
    if missing:
        reasons.append(f"capability lacks required features: {', '.join(missing)}")


def _declared_values(record: Mapping[str, Any], aliases: tuple[str, ...]) -> frozenset[str]:
    value = _first(record, *aliases)
    if not isinstance(value, Mapping):
        return _string_set(value)
    for key in ("supported_efforts", "efforts", "supported", "values", "items"):
        nested = value.get(key)
        values = _string_set(nested)
        if values:
            return values
    preferred = _optional_text(value.get("preferred_effort"))
    return frozenset({preferred}) if preferred is not None else frozenset()


def _feature_set(value: Any) -> frozenset[str]:
    if not isinstance(value, Mapping):
        return _string_set(value)
    return frozenset(
        _text(key)
        for key, enabled in value.items()
        if _truthy(enabled) and _text(key)
    )


def _tier_policy_reasons(
    reasons: list[str],
    record: Mapping[str, Any],
    request: Mapping[str, Any],
    role: RoutingV3Role,
    task_type: str,
    complexity: str,
    model: str,
) -> None:
    tier = _model_tier(model)
    if tier is None:
        reasons.append("unknown model family has no routing-v3 policy")
        return
    if tier == "sol":
        if role not in {"planner", "verifier", "control"}:
            reasons.append("Sol is reserved for planner, cross-module control, and final verifier roles")
        return
    if role in {"planner", "verifier", "control"}:
        reasons.append(f"{tier} cannot replace the Sol {role} control-plane role")
        return
    if tier == "spark":
        if role != "worker":
            reasons.append("Spark is only eligible for worker execution")
        if complexity != "low":
            reasons.append("Spark requires low complexity")
        if not _request_flag(request, "low_risk"):
            reasons.append("Spark requires an explicit low_risk task declaration")
        if not _request_flag(request, "short_task", "short"):
            reasons.append("Spark requires an explicit short_task declaration")
        if not _request_flag(request, "mechanically_verifiable", "mechanical"):
            reasons.append("Spark requires mechanically_verifiable work")
        return
    if tier == "luna":
        if role != "worker":
            reasons.append("Luna is reserved for bounded worker execution")
        if complexity not in {"low", "standard"}:
            reasons.append("Luna is limited to low or standard bounded work")
        if not _request_flag(request, "bounded", "bounded_scope"):
            reasons.append("Luna requires an explicit bounded scope")
        if task_type not in _WORKER_TASK_TYPES:
            reasons.append("Luna is not the architecture/review/challenge lane")
        return
    if tier == "terra":
        if role != "worker":
            reasons.append("Terra is reserved for independent implementation slices")
        if complexity not in {"standard", "high"}:
            reasons.append("Terra requires a standard or high-complexity slice")
        if not _request_flag(request, "independent_slice", "isolated_slice"):
            reasons.append("Terra requires an explicit independent_slice declaration")
        return
    if tier == "sonnet":
        if role not in {"worker", "challenge"}:
            reasons.append("Sonnet is not a planner, control, or final verifier lane")
        if task_type not in _WORKER_TASK_TYPES | {"review"}:
            reasons.append("Sonnet is limited to daily development, testing, debugging, docs, exploration, or review")
        return
    if tier == "opus":
        if role not in {"worker", "challenge"}:
            reasons.append("Opus is not a planner, control, or final verifier lane")
        if task_type not in _ARCHITECTURE_TASK_TYPES and complexity != "high":
            reasons.append("Opus requires complex reasoning, architecture/review, research, creative, or high complexity")
        return
    if tier == "fable":
        if role not in {"worker", "challenge"}:
            reasons.append("Fable is not a planner, control, or final verifier lane")
        if task_type not in _ARCHITECTURE_TASK_TYPES and role != "challenge":
            reasons.append("Fable is reserved for architecture, review, research, creative, or challenge work")


def _provider_runtime_available(
    snapshot: Mapping[str, Any],
    record: Mapping[str, Any],
    provider: str,
) -> bool:
    for key in ("provider_runtime", "provider_runtimes", "runtimes", "providers", "agents"):
        container = snapshot.get(key)
        if not isinstance(container, Mapping):
            continue
        entry = container.get(provider)
        if entry is None and provider in _CLAUDE_PROVIDERS:
            entry = container.get("claude") or container.get("anthropic")
        if entry is not None:
            return _availability(entry)
    # A per-capability runtime declaration remains sufficient for a compact
    # snapshot; catalog implementations may additionally provide a provider
    # runtime ledger above.
    return _truthy(_first(record, "provider_runtime_available", "runtime_available", "available", "runtime"))


def _capability_runtime_available(record: Mapping[str, Any], *, status: str) -> bool:
    explicit = _first(record, "runtime_available", "available", "runtime")
    if explicit is not None:
        return _truthy(explicit)
    # The passive catalog records an individual model only after the provider
    # CLI made it observable.  ``status=available`` is its explicit runtime
    # declaration; custom catalogs can instead use ``runtime_available``.
    return status in _ACTIVE_STATUSES


def _availability(value: Any) -> bool:
    if isinstance(value, Mapping):
        return _truthy(_first(value, "available", "runtime_available", "running", "healthy", "status"))
    return _truthy(value)


def _claude_quota_reasons(
    reasons: list[str],
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    record: Mapping[str, Any],
    model: str,
) -> None:
    quota_value = _first(request, "claude_quota", "quota")
    if quota_value is None:
        quota_value = _first(snapshot, "claude_quota", "quota")
    if quota_value is None:
        reasons.append("Claude quota and authentication provenance are unavailable")
        return
    quota = _as_mapping(quota_value, label="Claude quota")
    if not _truthy(_first(quota, "auth_ok", "authenticated", "native_subscription")):
        reasons.append("Claude native-subscription authentication is unavailable")
        return
    zone = _optional_text(quota.get("zone"))
    if zone in {"unknown", "protected", "red", "auth-unavailable"}:
        reasons.append(f"Claude quota zone {zone!r} does not permit a new turn")
        return
    family = _model_tier(model) or ""
    remaining = _claude_remaining_percent(quota, family, record)
    if remaining is None:
        reasons.append("Claude quota remaining percentage is unknown")
        return
    if remaining <= 20.0:
        reasons.append(f"Claude 20% hard reserve is active at {remaining:g}% remaining")
        return
    if remaining <= 25.0:
        reasons.append(f"Claude 25% stop line is active at {remaining:g}% remaining")
        return
    if zone == "yellow" and family != "sonnet":
        reasons.append("Claude yellow zone permits Sonnet only")


def _claude_remaining_percent(
    quota: Mapping[str, Any], family: str, record: Mapping[str, Any]
) -> float | None:
    pool = _optional_text(_first(record, "quota_pool", "pool"))
    if pool:
        pools = quota.get("pools")
        if isinstance(pools, Mapping) and pools.get(pool) is not None:
            pool_values = _remaining_values(_as_mapping(pools[pool], label="Claude quota pool"), family)
            if pool_values:
                return min(pool_values)
    values = _remaining_values(quota, family)
    return min(values) if values else None


def _remaining_values(quota: Mapping[str, Any], family: str) -> list[float]:
    keys = ["remaining_percent", "remaining", "five_hour_remaining", "weekly_all_remaining"]
    keys.append(f"weekly_{family}_remaining")
    values: list[float] = []
    for key in keys:
        parsed = _number(quota.get(key))
        if parsed is not None:
            values.append(parsed)
    return values


def _concurrency_reasons(
    reasons: list[str],
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    record: Mapping[str, Any],
    model: str,
    active_model_ids: frozenset[str],
) -> None:
    capacity, active, weight = _concurrency_values(
        snapshot, request, record, model, active_model_ids
    )
    if capacity <= 0:
        reasons.append("capability concurrency capacity is unavailable")
    elif active + weight > capacity:
        reasons.append(
            f"capability concurrency capacity reached ({active:g}/{capacity:g} active; {weight:g} required)"
        )


def _candidate_inputs(
    record: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    capability_id: str,
    quality_floor: float,
    acceptance_risk: str,
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    active_model_ids: frozenset[str],
) -> dict[str, Any]:
    quality = _record_quality(record)
    assert quality is not None  # guarded by _hard_gate_reasons
    cost = _metric(
        record,
        ("estimated_cost_units", "cost_units", "estimated_cost", "cost"),
        default=1_000_000.0,
        ordinal=_COST_ORDINAL,
        mapping_keys=("relative", "class"),
    )
    latency = _metric(
        record,
        ("estimated_latency_ms", "latency_ms", "estimated_latency", "latency"),
        default=1_000_000.0,
        ordinal=_LATENCY_ORDINAL,
        mapping_keys=("class", "relative"),
    )
    throughput = _metric(
        record,
        ("estimated_throughput", "throughput", "throughput_units"),
        default=_metric(
            record,
            ("concurrency",),
            default=0.0,
            ordinal=_THROUGHPUT_ORDINAL,
            mapping_keys=("class",),
        ),
    )
    capacity, active, weight = _concurrency_values(snapshot, request, record, model, active_model_ids)
    utilization = (active + weight) / capacity if capacity > 0 else 1.0
    capability_digest = _text(record.get("digest"), default=_digest_mapping(record))
    preferred_families = tuple(_ordered_strings(request.get("preferred_families", ())))
    family = _model_tier(model) or model
    preference_rank = (
        preferred_families.index(family)
        if family in preferred_families
        else len(preferred_families)
    )
    score = (
        -quality,
        preference_rank,
        cost,
        latency,
        -throughput,
        utilization,
        provider,
        model,
        capability_id,
    )
    return {
        "provider": provider,
        "model": model,
        "capability_id": capability_id,
        "capability_digest": capability_digest,
        "quality_score": quality,
        "quality_floor": quality_floor,
        "acceptance_risk": acceptance_risk,
        "estimated_cost_units": cost,
        "estimated_latency_ms": latency,
        "estimated_throughput": throughput,
        "concurrency_utilization": utilization,
        "preference_rank": preference_rank,
        "score": score,
        "reasons": (
            f"quality {quality:g} meets floor {quality_floor:g}",
            "quality is the primary routing-v3 rank; declared role fit, cost, and latency are tie-breakers",
        ),
    }


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[float | str, ...]:
    return tuple(candidate["score"])


def _parallel_provider_candidates(
    ranked: tuple[RankedCandidate, ...], request: Mapping[str, Any]
) -> tuple[RankedCandidate, ...]:
    if not _request_flag(request, "allow_parallel_providers", "parallel_providers"):
        return ()
    raw_limit = _number(_first(request, "parallel_provider_limit", "parallelism"))
    limit = max(1, int(raw_limit if raw_limit is not None else 2))
    selected: list[RankedCandidate] = []
    providers: set[str] = set()
    for candidate in ranked:
        if candidate.provider in providers:
            continue
        selected.append(candidate)
        providers.add(candidate.provider)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _concurrency_values(
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    record: Mapping[str, Any],
    model: str,
    active_model_ids: frozenset[str],
) -> tuple[float, float, float]:
    concurrency = record.get("concurrency")
    nested = _as_mapping(concurrency, label="capability concurrency") if isinstance(concurrency, Mapping) else {}
    provider, _, _ = _identity(record)
    runtime = _provider_capacity(snapshot, request, record, provider)
    capacity = _metric(
        nested or record,
        ("capacity", "max_concurrency", "concurrency_capacity"),
        default=_metric(
            runtime,
            ("capacity", "max_concurrency", "concurrency_capacity"),
            # No live capacity observation must not disable every freshly
            # probed model.  A single conservative slot preserves the hard
            # gate; service can pass measured provider capacity for parallel
            # dispatch.
            default=1.0,
        ),
    )
    explicit_active = _number(_first(nested or record, "active", "active_count", "active_units"))
    if explicit_active is None:
        explicit_active = _number(_first(runtime, "active", "active_count", "active_units"))
    active = explicit_active if explicit_active is not None else float(sum(value == model for value in active_model_ids))
    weight = _metric(nested or record, ("weight", "concurrency_weight", "requested_units"), default=1.0)
    return capacity, active, weight


def _provider_capacity(
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    record: Mapping[str, Any],
    provider: str,
) -> dict[str, Any]:
    pool = _optional_text(_first(record, "quota_pool", "pool"))
    for source in (request, snapshot):
        for key in ("provider_capacity", "provider_concurrency", "concurrency_capacity"):
            container = source.get(key)
            if not isinstance(container, Mapping):
                continue
            selected = container.get(pool) if pool else None
            selected = selected if selected is not None else container.get(provider)
            if selected is not None:
                return _as_mapping(selected, label="provider concurrency") if isinstance(selected, Mapping) else {"capacity": selected}
    return {}


def _record_quality(record: Mapping[str, Any]) -> float | None:
    raw = _first(record, "quality_score", "quality", "estimated_quality")
    numeric = _number(raw)
    if numeric is not None:
        return numeric
    if isinstance(raw, Mapping):
        value = _optional_text(_first(raw, "floor", "class", "relative"))
        if value is not None:
            return _QUALITY_ORDINAL.get(value, 0.0)
    return None


def _quality_number(value: Any, *, default: float) -> float:
    parsed = _number(value)
    if parsed is not None:
        return parsed
    if value is None:
        return default
    named = {
        "low": 0.0,
        "standard": 0.0,
        "high": 0.0,
        "critical": 0.0,
        **_QUALITY_ORDINAL,
    }
    text = _text(value)
    if text in named:
        return named[text]
    raise ValueError(f"quality_floor must be numeric, got {value!r}")


def _metric(
    record: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    default: float,
    ordinal: Mapping[str, float] | None = None,
    mapping_keys: tuple[str, ...] = (),
) -> float:
    value = _first(record, *keys)
    parsed = _number(value)
    if parsed is not None:
        return parsed
    if isinstance(value, Mapping) and ordinal is not None:
        text = _optional_text(_first(value, *mapping_keys))
        if text is not None:
            return ordinal.get(text, default)
    return default


def _number(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = _first(value, "score", "value", "estimated", "remaining")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _model_tier(model: str) -> str | None:
    lower = model.lower()
    for tier in ("spark", "luna", "terra", "sol", "fable", "opus", "sonnet"):
        if tier in lower:
            return tier
    return None


def _snapshot_id(snapshot: Mapping[str, Any]) -> str:
    return _text(_first(snapshot, "snapshot_id", "generation", "catalog_id", "id"), default="derived")


def _catalog_digest(snapshot: Mapping[str, Any]) -> str:
    return _text(_first(snapshot, "digest", "catalog_digest", "capability_digest"), default=_digest_mapping(snapshot))


def _digest_mapping(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(sorted((str(key), repr(item)) for key, item in value.items()))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip().lower() or default


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _string_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({_text(value)})
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return frozenset(_text(item) for item in value if _text(item))
    return frozenset({_text(value)})


def _ordered_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = tuple(value)
    else:
        values = (value,)
    ordered: list[str] = []
    for item in values:
        normalized = _text(item)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def _truthy(value: Any) -> bool:
    if isinstance(value, Mapping):
        return _truthy(_first(value, "available", "runtime_available", "running", "healthy", "value", "status"))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "active", "available", "running", "healthy", "verified", "current"}
    return bool(value)


def _request_flag(request: Mapping[str, Any], *aliases: str) -> bool:
    return _truthy(_first(request, *aliases))
