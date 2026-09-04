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
    # Declared quality is the hard-gate contract.  Calibrated quality is a
    # separate conservative ranking signal so a weak prior cannot appear to
    # violate a floor that the capability already passed.
    quality_score: float
    ranking_quality_score: float
    quality_floor: float
    acceptance_risk: str
    estimated_cost_units: float
    estimated_latency_ms: float
    estimated_throughput: float
    concurrency_utilization: float
    preference_rank: int
    score: tuple[float | str, ...]
    reasons: tuple[str, ...]
    # Performance calibration is advisory metadata.  It is copied from the
    # immutable performance generation (when an exact model/agent/task bucket
    # exists) so a route receipt can explain both its quality input and its
    # absence.  ``None`` is intentional for unmeasured models such as Spark.
    performance_snapshot_id: str | None = None
    performance_digest: str | None = None
    quality_source: str = "declared"
    performance_lower_bound_95: float | None = None
    runtime_sample_count: int = 0
    performance_first_pass_rate: float | None = None
    performance_rework_rate: float | None = None
    performance_latency_ms: float | None = None

    @property
    def lower_bound_95(self) -> float | None:
        """Compatibility spelling for the calibrated posterior lower bound."""

        return self.performance_lower_bound_95

    @property
    def performance_quality_source(self) -> str:
        return self.quality_source

    @property
    def performance_runtime_sample_count(self) -> int:
        return self.runtime_sample_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "provider": self.provider,
            "model": self.model,
            "capability_id": self.capability_id,
            "capability_digest": self.capability_digest,
            "quality_score": self.quality_score,
            "ranking_quality_score": self.ranking_quality_score,
            "quality_floor": self.quality_floor,
            "acceptance_risk": self.acceptance_risk,
            "estimated_cost_units": self.estimated_cost_units,
            "estimated_latency_ms": self.estimated_latency_ms,
            "estimated_throughput": self.estimated_throughput,
            "concurrency_utilization": self.concurrency_utilization,
            "preference_rank": self.preference_rank,
            "score": list(self.score),
            "reasons": list(self.reasons),
            "performance_snapshot_id": self.performance_snapshot_id,
            "performance_digest": self.performance_digest,
            "quality_source": self.quality_source,
            "performance_lower_bound_95": self.performance_lower_bound_95,
            # Keep the short posterior spelling in receipts for consumers that
            # read PerformanceRegistry.calibrate directly.
            "lower_bound_95": self.performance_lower_bound_95,
            "runtime_sample_count": self.runtime_sample_count,
            "performance_first_pass_rate": self.performance_first_pass_rate,
            "performance_rework_rate": self.performance_rework_rate,
            "performance_latency_ms": self.performance_latency_ms,
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
    performance_snapshot_id: str | None = None
    performance_digest: str | None = None
    performance_status: str | None = None

    def __post_init__(self) -> None:
        if (self.performance_snapshot_id is None) != (self.performance_digest is None):
            raise ValueError(
                "routing-v3 performance_snapshot_id and performance_digest must be supplied together"
            )

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
            "performance_snapshot_id": self.performance_snapshot_id,
            "performance_digest": self.performance_digest,
            "performance_status": self.performance_status,
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
    performance_calibration: Mapping[str, Any] | None = None,
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
    if performance_calibration is not None:
        if (
            requested.get("performance_calibration") is not None
            and requested.get("performance_calibration") != performance_calibration
        ):
            raise ValueError("conflicting performance calibrations were supplied")
        requested["performance_calibration"] = dict(performance_calibration)
    role = _request_role(requested)
    task_type = _text(requested.get("task_type"), default="implementation")
    complexity = _text(requested.get("complexity"), default="standard")
    performance = _performance_context(
        catalog,
        requested,
        task_type=task_type,
        complexity=complexity,
    )
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
                task_type=task_type,
                complexity=complexity,
                performance=performance,
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
            f"rank {selected.ranking_quality_score:g} is ranked before cost and latency"
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
        performance_snapshot_id=performance[1],
        performance_digest=performance[2],
        performance_status=performance[3],
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


def _performance_context(
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    task_type: str,
    complexity: str,
) -> tuple[Mapping[str, Any] | None, str | None, str | None, str | None]:
    """Return the immutable performance calibration and its route binding.

    ``PerformanceRegistry.calibrate`` is intentionally passed as a nested
    advisory object.  The routing catalog remains the capability authority;
    this helper only validates the optional generation binding and prepares an
    exact lookup context for each candidate.
    """

    raw = request.get("performance_calibration")
    if raw is None:
        raw = snapshot.get("performance_calibration")
    calibration: Mapping[str, Any] | None = None
    if raw is not None:
        calibration = _as_mapping(raw, label="performance calibration")

    requested_id = _optional_text(
        _first(request, "performance_snapshot_id", "performance_generation_id")
    )
    requested_digest = _optional_text(
        _first(request, "performance_digest", "performance_snapshot_digest")
    )
    calibration_id = (
        _optional_text(
            _first(
                calibration or {},
                "performance_snapshot_id",
                "snapshot_id",
                "generation",
            )
        )
        if calibration is not None
        else None
    )
    calibration_digest = (
        _optional_text(
            _first(
                calibration or {},
                "performance_digest",
                "digest",
                "snapshot_digest",
            )
        )
        if calibration is not None
        else None
    )
    contexts_present = calibration is not None and "contexts" in calibration
    selected_context: Mapping[str, Any] | None = None
    if contexts_present:
        raw_contexts = calibration.get("contexts")
        if not isinstance(raw_contexts, Iterable) or isinstance(raw_contexts, (str, bytes, Mapping)):
            raise TypeError("performance calibration contexts must be a sequence")
        for raw_context in raw_contexts:
            context = _as_mapping(raw_context, label="performance calibration context")
            context_task_type = _optional_text(context.get("task_type"))
            context_complexity = _optional_text(context.get("complexity"))
            if context_task_type == task_type and context_complexity == complexity:
                if selected_context is not None:
                    raise ValueError(
                        "performance calibration has duplicate task_type and complexity context"
                    )
                selected_context = context

    selected_id = (
        _optional_text(
            _first(
                selected_context or {},
                "performance_snapshot_id",
                "snapshot_id",
                "generation",
            )
        )
        if selected_context is not None
        else None
    )
    selected_digest = (
        _optional_text(
            _first(
                selected_context or {},
                "performance_digest",
                "digest",
                "snapshot_digest",
            )
        )
        if selected_context is not None
        else None
    )
    if calibration_id is not None and selected_id is not None and calibration_id != selected_id:
        raise ValueError("performance calibration context snapshot does not match calibration")
    if (
        calibration_digest is not None
        and selected_digest is not None
        and calibration_digest != selected_digest
    ):
        raise ValueError("performance calibration context digest does not match calibration")
    # A context may omit the immutable identity because it inherits the
    # task-level generation.  Conversely, a context-only payload can carry the
    # identity itself.  Resolve both sides before validating the pair so that
    # either shape remains compatible while an explicitly conflicting value is
    # rejected above.
    calibration_id = calibration_id or selected_id
    calibration_digest = calibration_digest or selected_digest
    # Older PerformanceRegistry.calibrate payloads carry the active generation
    # ID but not its digest.  A submission contract pins both values, so it is
    # safe to complete the advisory payload from that immutable request pin;
    # without the request digest we still fail closed rather than emit an
    # unverifiable performance receipt.
    if calibration_id is not None and calibration_digest is None and requested_digest is not None:
        calibration_digest = requested_digest
    _validate_performance_binding(
        requested_id,
        requested_digest,
        "routing request",
    )
    _validate_performance_binding(
        calibration_id,
        calibration_digest,
        "performance calibration",
    )
    if requested_id is not None and calibration_id is not None and requested_id != calibration_id:
        raise ValueError("routing request performance snapshot does not match calibration")
    if (
        requested_digest is not None
        and calibration_digest is not None
        and requested_digest != calibration_digest
    ):
        raise ValueError("routing request performance digest does not match calibration")

    performance_id = calibration_id or requested_id
    performance_digest = calibration_digest or requested_digest
    status = (
        _optional_text(_first(calibration or {}, "status", "performance_status"))
        if calibration is not None
        else None
    )
    if status is None and selected_context is not None:
        status = _optional_text(_first(selected_context, "status", "performance_status"))
    if status is None:
        status = _optional_text(request.get("performance_status"))
    # The status is descriptive only.  A cold-start generation may still carry
    # benchmark priors, while a missing generation must never be fabricated.
    if status is None and performance_id is not None:
        status = "unknown"
    if calibration is not None and contexts_present:
        # A task-level calibration can contain several exact DAG buckets.  The
        # top-level task_type/complexity describes the calibration call, not a
        # neighboring node; only the selected context may contribute scores.
        calibration = selected_context
    elif calibration is not None:
        cal_task_type = _optional_text(calibration.get("task_type"))
        cal_complexity = _optional_text(calibration.get("complexity"))
        if (
            (cal_task_type is not None and cal_task_type != task_type)
            or (cal_complexity is not None and cal_complexity != complexity)
        ):
            # One task-level calibration is commonly reused while the planner
            # specializes a DAG node.  Keep the immutable generation receipt,
            # but discard its quality candidates for this non-exact bucket;
            # never transfer a neighboring task/complexity score.
            calibration = None
    return calibration, performance_id, performance_digest, status


def _validate_performance_binding(
    snapshot_id: str | None,
    digest: str | None,
    owner: str,
) -> None:
    if (snapshot_id is None) != (digest is None):
        raise ValueError(
            f"{owner} performance_snapshot_id and performance_digest must be supplied together"
        )
    if snapshot_id is not None and (
        not isinstance(snapshot_id, str)
        or not snapshot_id.strip()
        or any(character.isspace() for character in snapshot_id)
    ):
        raise ValueError(f"{owner} performance_snapshot_id must be non-empty and safe")
    if digest is not None:
        normalized = digest.removeprefix("sha256:")
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized.lower()):
            raise ValueError(f"{owner} performance_digest must be a SHA-256 digest")


def _record_agent_version(
    snapshot: Mapping[str, Any],
    record: Mapping[str, Any],
    provider: str,
) -> str:
    direct = _first(record, "agent_version", "agent_cli_version", "cli_version")
    if direct is not None:
        return _text(direct, default="unattested")
    nested = record.get("agent")
    if isinstance(nested, Mapping):
        value = _first(nested, "version", "cli_version", "agent_version")
        if value is not None:
            return _text(value, default="unattested")
    agents = snapshot.get("agents")
    if isinstance(agents, Mapping):
        agent = agents.get(provider)
        if agent is None and provider in _CLAUDE_PROVIDERS:
            agent = agents.get("claude") or agents.get("anthropic")
        if isinstance(agent, Mapping):
            value = _first(agent, "cli_version", "version", "agent_version")
            if value is not None:
                return _text(value, default="unattested")
    return "unattested"


def _performance_candidate(
    calibration: Mapping[str, Any] | None,
    *,
    provider: str,
    model: str,
    agent_version: str,
    reasoning_effort: str | None,
    task_type: str,
    complexity: str,
) -> dict[str, Any] | None:
    """Find one exact PerformanceRegistry bucket; family transfer is forbidden."""

    if calibration is None:
        return None
    calibration_task = _optional_text(calibration.get("task_type"))
    calibration_complexity = _optional_text(calibration.get("complexity"))
    if calibration_task not in {None, task_type} or calibration_complexity not in {
        None,
        complexity,
    }:
        return None
    candidates = calibration.get("candidates")
    if not isinstance(candidates, Iterable) or isinstance(candidates, (str, bytes, Mapping)):
        return None
    for raw in candidates:
        if not isinstance(raw, Mapping):
            continue
        candidate_provider = _text(raw.get("provider"), default="")
        candidate_model = _text(
            _first(raw, "model_id", "model", "model_name"),
            default="",
        )
        candidate_agent = _text(
            _first(raw, "agent_version", "agent_cli_version", "cli_version"),
            default="unattested",
        )
        candidate_effort = _optional_text(raw.get("reasoning_effort"))
        candidate_task = _optional_text(raw.get("task_type"))
        candidate_complexity = _optional_text(raw.get("complexity"))
        if (
            candidate_provider != provider
            or candidate_model != model
            or candidate_agent != agent_version
            or candidate_effort != reasoning_effort
            or candidate_task not in {None, task_type}
            or candidate_complexity not in {None, complexity}
        ):
            continue
        return dict(raw)
    return None


def _performance_inputs(
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract only measured/prior-backed values, never a synthetic Spark score."""

    empty = {
        "quality_source": "declared",
        "lower_bound_95": None,
        "runtime_sample_count": 0,
        "first_pass_rate": None,
        "rework_rate": None,
        "latency_ms": None,
    }
    if candidate is None:
        return empty
    quality = candidate.get("quality")
    quality_mapping = quality if isinstance(quality, Mapping) else {}
    posterior = quality_mapping.get("posterior")
    if not isinstance(posterior, Mapping):
        posterior = candidate.get("posterior")
    posterior = posterior if isinstance(posterior, Mapping) else {}
    prior = quality_mapping.get("prior")
    prior = prior if isinstance(prior, Mapping) else {}
    lower = _number(_first(posterior, "lower_bound_95", "lower_bound", "quality_lower_bound_95"))
    samples = _nonnegative_int(
        _first(posterior, "runtime_sample_count", "sample_count", "runtime_samples")
    )
    prior_available = _text(prior.get("evidence_status"), default="") == "available"
    # A generic-conservative prior is not evidence and must not masquerade as a
    # Spark quality estimate.  Runtime observations remain valid even for a
    # model with no public benchmark score.
    usable = lower is not None and 0 <= lower <= 1 and (samples > 0 or prior_available)
    runtime = candidate.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    first_pass = _rate_value(
        _first(candidate, "first_pass", "first_pass_rate")
        if _first(candidate, "first_pass", "first_pass_rate") is not None
        else runtime.get("first_pass")
    )
    rework = _rate_value(
        _first(candidate, "rework_rate", "rework")
        if _first(candidate, "rework_rate", "rework") is not None
        else runtime.get("rework_rate")
    )
    if rework is None:
        rework_count = _number(
            _first(runtime, "quality_rework_count", "rework_count")
        )
        denominator = _number(runtime.get("attempt_count"))
        if rework_count is not None and denominator is not None and denominator > 0:
            rework = max(0.0, rework_count / denominator)
    latency = _runtime_latency_ms(candidate, runtime)
    return {
        "quality_source": "calibrated" if usable else "declared",
        "lower_bound_95": lower if usable else None,
        "runtime_sample_count": samples if usable else 0,
        "first_pass_rate": first_pass,
        "rework_rate": rework,
        "latency_ms": latency,
    }


def _nonnegative_int(value: Any) -> int:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return 0
    return int(number)


def _rate_value(value: Any) -> float | None:
    if isinstance(value, Mapping):
        accepted = _number(value.get("accepted"))
        total = _number(value.get("total"))
        if accepted is not None and total is not None and total > 0:
            return max(0.0, min(1.0, accepted / total))
        value = _first(value, "rate", "value", "score")
    number = _number(value)
    if number is None:
        return None
    # Runtime rates are fractions; tolerate percentage receipts at the
    # boundary, but never allow an out-of-range value into the sort key.
    if number > 1 and number <= 100:
        number /= 100
    return number if 0 <= number <= 1 else None


def _runtime_latency_ms(candidate: Mapping[str, Any], runtime: Mapping[str, Any]) -> float | None:
    value = _first(candidate, "performance_latency_ms", "latency_ms", "runtime_latency_ms")
    if value is None:
        duration = runtime.get("duration_seconds")
        if isinstance(duration, Mapping):
            value = _first(duration, "p50", "median", "mean")
            number = _number(value)
            return number * 1000 if number is not None and number >= 0 else None
        value = _first(runtime, "latency_ms", "duration_ms")
    number = _number(value)
    return number if number is not None and number >= 0 else None


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
    task_type: str,
    complexity: str,
    performance: tuple[Mapping[str, Any] | None, str | None, str | None, str | None],
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
    agent_version = _record_agent_version(snapshot, record, provider)
    reasoning_effort = _record_reasoning_effort(record, request)
    performance_calibration, performance_snapshot_id, performance_digest, _ = performance
    performance_candidate = _performance_candidate(
        performance_calibration,
        provider=provider,
        model=model,
        agent_version=agent_version,
        reasoning_effort=reasoning_effort,
        task_type=task_type,
        complexity=complexity,
    )
    performance_values = _performance_inputs(performance_candidate)
    quality_source = performance_values["quality_source"]
    performance_lower_bound = performance_values["lower_bound_95"]
    quality_for_ranking = (
        performance_lower_bound * 100
        if performance_lower_bound is not None
        else quality
    )
    preferred_families = tuple(_ordered_strings(request.get("preferred_families", ())))
    family = _model_tier(model) or model
    preference_rank = (
        preferred_families.index(family)
        if family in preferred_families
        else len(preferred_families)
    )
    score = (
        -quality_for_ranking,
        # A measured first-pass signal breaks otherwise equal conservative
        # bounds.  Missing observations sort after observed values.
        -(performance_values["first_pass_rate"] if performance_values["first_pass_rate"] is not None else -1.0),
        performance_values["rework_rate"] if performance_values["rework_rate"] is not None else 1_000_000.0,
        performance_values["latency_ms"] if performance_values["latency_ms"] is not None else 1_000_000.0,
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
        "ranking_quality_score": quality_for_ranking,
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
            (
                f"ranking quality source {quality_source}; posterior lower_bound_95="
                f"{performance_lower_bound:g}"
                if performance_lower_bound is not None
                else "quality source declared; no exact calibrated posterior was available"
            ),
            "quality is the primary routing-v3 rank; first-pass, rework, latency, cost, and throughput are tie-breakers",
        ),
        "performance_snapshot_id": performance_snapshot_id,
        "performance_digest": performance_digest,
        "quality_source": quality_source,
        "performance_lower_bound_95": performance_lower_bound,
        "runtime_sample_count": performance_values["runtime_sample_count"],
        "performance_first_pass_rate": performance_values["first_pass_rate"],
        "performance_rework_rate": performance_values["rework_rate"],
        "performance_latency_ms": performance_values["latency_ms"],
    }


def _record_reasoning_effort(
    record: Mapping[str, Any],
    request: Mapping[str, Any],
) -> str | None:
    requested = _optional_text(request.get("reasoning_effort"))
    if requested is not None:
        return requested
    reasoning = _first(record, "reasoning", "reasoning_efforts", "supported_reasoning_efforts")
    if isinstance(reasoning, Mapping):
        for key in ("preferred_effort", "default_effort"):
            value = _optional_text(reasoning.get(key))
            if value is not None:
                return value
        supported = _declared_values(record, ("reasoning", "reasoning_efforts", "supported_reasoning_efforts"))
    else:
        supported = _string_set(reasoning)
    return next(iter(supported)) if len(supported) == 1 else None


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
