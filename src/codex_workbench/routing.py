"""Deterministic model routing for planner-generated task graphs.

The planner remains responsible for decomposing a request into a DAG.  This
module owns the provider/model decision for each role so a model cannot widen
the routing policy merely by returning a different JSON value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal

from .model import (
    CODEX_SOL_MODEL,
    DEFAULT_QUOTA_TTL_SECONDS,
    LEGACY_ROUTING_STRATEGY_VERSION,
    QuotaSnapshot,
    RoutingStrategy,
    TaskContract,
    ROUTING_STRATEGY_VERSION,
    codex_model_profile,
    codex_model_reasoning_effort,
    retry_model,
)
from .routing_v3 import ROUTING_V3_POLICY_VERSION, RoutingV3Decision, route_capability_snapshot


CODEX_LUNA_MODEL = "gpt-5.6-luna"
CODEX_SPARK_MODEL = "gpt-5.3-codex-spark"
CODEX_TERRA_MODEL = "gpt-5.6-terra"
CLAUDE_FAMILIES = ("opus", "sonnet", "fable")
RoutingRole = Literal["planner", "worker", "verifier", "challenge", "control"]


@dataclass(frozen=True)
class RoutingDecision:
    """The complete, serializable result of one routing decision."""

    role: RoutingRole
    executor: Literal["codex", "claude", "deterministic", "fixture"]
    model: str
    strategy_version: str
    reason: str
    claude_eligible: bool = False
    model_profile: str | None = None
    model_reasoning_effort: str | None = None
    # Capability routing is optional so persisted routing-v1/v2 decisions and
    # fixtures retain their original shape/semantics.  A v3 result carries the
    # exact immutable catalog identity used to choose it.
    capability_snapshot_id: str | None = None
    capability_digest: str | None = None
    model_capability_id: str | None = None
    agent_capability_id: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    routing_policy_version: str | None = None
    # Performance calibration receipt.  These fields remain optional for
    # routing-v1/v2 and legacy fixtures, but are populated for an exact v3
    # performance generation or an explicitly pinned task contract.
    performance_snapshot_id: str | None = None
    performance_digest: str | None = None
    performance_status: str | None = None
    quality_source: str = "declared-policy"
    performance_routing_receipt: dict[str, Any] | None = None
    performance_lower_bound_95: float | None = None
    runtime_sample_count: int = 0
    performance_first_pass_rate: float | None = None
    performance_rework_rate: float | None = None
    performance_latency_ms: float | None = None

    def __post_init__(self) -> None:
        if (self.performance_snapshot_id is None) != (self.performance_digest is None):
            raise ValueError(
                "routing performance_snapshot_id and performance_digest must be supplied together"
            )
        if self.executor != "codex":
            return
        expected_profile = codex_model_profile(self.model)
        expected_effort = codex_model_reasoning_effort(self.model)
        if expected_profile is not None:
            if self.model_profile is None:
                object.__setattr__(self, "model_profile", expected_profile)
            elif self.model_profile != expected_profile:
                raise ValueError(
                    f"routing model_profile {self.model_profile!r} does not match {self.model!r}"
                )
        if expected_effort is not None:
            if self.model_reasoning_effort is None:
                object.__setattr__(self, "model_reasoning_effort", expected_effort)
            elif self.model_reasoning_effort != expected_effort:
                raise ValueError(
                    f"routing model_reasoning_effort {self.model_reasoning_effort!r} does not match {self.model!r}"
                )

    @property
    def provider(self) -> str:
        return self.executor

    @property
    def policy_version(self) -> str:
        return self.routing_policy_version or self.strategy_version

    @property
    def selected_model(self) -> str:
        return self.model

    @property
    def selected_executor(self) -> str:
        return self.executor

    @property
    def lower_bound_95(self) -> float | None:
        return self.performance_lower_bound_95

    @property
    def performance_quality_source(self) -> str:
        return self.quality_source

    @property
    def performance_runtime_sample_count(self) -> int:
        return self.runtime_sample_count

    @property
    def family(self) -> str:
        lower = self.model.lower()
        for family in CLAUDE_FAMILIES:
            if family in lower:
                return family
        if "luna" in lower:
            return "luna"
        if "terra" in lower:
            return "terra"
        if "sol" in lower:
            return "sol"
        return self.model

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The shorter name is convenient for callers and preserves the terminology
# used in the original design.
RouteDecision = RoutingDecision


def _family(model: str) -> str | None:
    lower = model.lower()
    for family in CLAUDE_FAMILIES:
        if family in lower:
            return family
    return None


def _first_available(families: Iterable[str], available: Iterable[str]) -> str | None:
    available_models = tuple(str(model) for model in available)
    for desired in families:
        for model in available_models:
            if _family(model) == desired:
                return model
    return None


def _claude_permitted(
    model: str,
    *,
    claude_models_available: tuple[str, ...],
    quota_snapshot: QuotaSnapshot | None,
    active_models: tuple[str, ...],
    max_age_seconds: int | None,
    shared_capacity: bool,
) -> tuple[bool, str]:
    family = _family(model)
    if family is None:
        return False, "requested model is not a Claude family"
    if not any(_family(candidate) == family for candidate in claude_models_available):
        return False, f"Claude {family} is not admitted by the current strategy context"
    if quota_snapshot is None:
        return False, "Claude quota provenance is unavailable"
    quota_decision = quota_snapshot.dispatch_decision(
        family,
        active_models,
        max_age_seconds=max_age_seconds,
        shared_capacity=shared_capacity,
    )
    if quota_decision.action != "claude":
        return False, quota_decision.reason
    return True, quota_decision.reason


def _strategy_for(
    contract: TaskContract,
    strategy: RoutingStrategy | dict[str, Any] | None,
) -> RoutingStrategy:
    if strategy is None:
        return contract.strategy
    if isinstance(strategy, dict):
        return RoutingStrategy.from_dict(strategy)
    return strategy.normalized()


def _contract_performance_kwargs(contract: TaskContract) -> dict[str, Any]:
    """Return the task's immutable performance binding for legacy receipts."""

    return {
        "performance_snapshot_id": contract.performance_snapshot_id,
        "performance_digest": contract.performance_digest,
        "performance_status": contract.performance_status,
    }


def strategy_for_node(
    contract: TaskContract,
    node: Any,
    strategy: RoutingStrategy | dict[str, Any] | None = None,
) -> RoutingStrategy:
    """Resolve a node's typed routing metadata over the task defaults.

    Older plans have no node metadata and inherit the contract unchanged.  A
    newer plan may specialize task type, complexity, parallelizability, or
    Claude admission for one DAG node.  The policy version itself cannot be
    downgraded by a node, so a mismatched explicit version is rejected.
    """

    base = _strategy_for(contract, strategy)
    raw = node.to_dict() if hasattr(node, "to_dict") else dict(node)
    fields = {
        name: raw[name]
        for name in ("routing_strategy", "task_type", "complexity", "parallelizable", "claude_allowed")
        if name in raw and raw[name] is not None
    }
    if not base.claude_allowed and fields.get("claude_allowed") is True:
        raise ValueError(
            "node claude_allowed=True cannot widen task contract with claude_allowed=False"
        )
    version = fields.get("routing_strategy", base.version)
    # Normalize legacy spellings (v1/v2) before comparing the immutable
    # contract version, while still rejecting an actual policy downgrade.
    normalized_version = RoutingStrategy(
        version=version,
        task_type=fields.get("task_type", base.task_type),
        complexity=fields.get("complexity", base.complexity),
        parallelizable=fields.get("parallelizable", base.parallelizable),
        claude_allowed=fields.get("claude_allowed", base.claude_allowed),
    ).normalized()
    if normalized_version.version != base.version:
        raise ValueError(
            f"node routing_strategy {version!r} must match task strategy {base.version!r}"
        )
    return normalized_version


def _codex_fallback_base_model(strategy: RoutingStrategy) -> tuple[str, str]:
    """Select the first Codex worker tier without consuming retry budget.

    v1 intentionally keeps its former implementation-only Luna preference.
    v2 assigns bounded low, mechanically verifiable work to Spark and the next
    inexpensive tier to standard, splittable production work;
    architecture, review, creative, non-splittable, and high-complexity work
    start at Terra when Claude is not currently usable.
    """

    task_type = strategy.task_type
    complexity = strategy.complexity
    if strategy.version == LEGACY_ROUTING_STRATEGY_VERSION:
        if (
            task_type == "implementation"
            and complexity in {"low", "standard"}
            and strategy.parallelizable
        ):
            return CODEX_LUNA_MODEL, "low-risk/splittable implementation"
        if complexity == "low" and task_type in {"implementation", "tests", "docs"}:
            return CODEX_LUNA_MODEL, "bounded low-complexity work"
        return CODEX_TERRA_MODEL, "complex or non-mechanical work"

    if complexity == "low":
        if task_type in {"architecture", "review", "creative"}:
            return CODEX_TERRA_MODEL, "low-complexity control/challenge work stays off the Spark pool"
        return CODEX_SPARK_MODEL, "bounded low-complexity work uses the independent Spark pool"
    if (
        complexity == "standard"
        and strategy.parallelizable
        and task_type in {"implementation", "debugging", "tests", "docs", "exploration"}
    ):
        return CODEX_LUNA_MODEL, "standard splittable production work"
    return CODEX_TERRA_MODEL, "complex, high-risk, or non-splittable work"


def codex_fallback_model(
    contract: TaskContract,
    *,
    strategy: RoutingStrategy | dict[str, Any] | None = None,
    attempt: int = 1,
) -> str:
    """Return the durable Codex fallback for this attempt.

    The first attempt follows the versioned routing tier. Legacy policies use
    Spark -> Luna -> Terra -> Sol or Luna -> Terra -> Sol escalation without
    downgrading a high-complexity first attempt.  Routing-v3 keeps the pinned
    capability immutable; a planner repair must route a replacement node when
    a different capability is required.
    """

    if attempt <= 0:
        raise ValueError("routing attempt must be positive")
    selected_strategy = _strategy_for(contract, strategy)
    base_model, _ = _codex_fallback_base_model(selected_strategy)
    if base_model == CODEX_SPARK_MODEL and not _is_explicit_spark_lane(
        contract,
        selected_strategy,
        None,
    ):
        base_model = CODEX_LUNA_MODEL
    return retry_model(base_model, attempt)


def _snapshot_text(snapshot: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = snapshot.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _catalog_identity(snapshot: Mapping[str, Any]) -> tuple[str, str]:
    snapshot_id = _snapshot_text(snapshot, "catalog_id", "snapshot_id", "generation", "id")
    digest = _snapshot_text(snapshot, "digest", "catalog_digest", "capability_digest")
    if snapshot_id is None or digest is None:
        raise ValueError("capability catalog must declare catalog_id and digest")
    return snapshot_id, digest


def _catalog_records(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    for name in ("models", "capabilities", "records"):
        value = snapshot.get(name)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            return tuple(dict(item) for item in value if isinstance(item, Mapping))
    return ()


def _model_family(model: object) -> str | None:
    value = str(model).lower()
    for family in ("spark", "luna", "terra", "sol", "fable", "opus", "sonnet"):
        if family in value:
            return family
    return None


def _snapshot_has_routable_family(
    snapshot: Mapping[str, Any], families: Iterable[str]
) -> bool:
    requested = frozenset(families)
    for record in _catalog_records(snapshot):
        if str(record.get("provider", "")).lower() not in {"claude", "anthropic"}:
            continue
        model = record.get("model_id", record.get("model", record.get("id", "")))
        if _model_family(model) not in requested:
            continue
        if record.get("status") == "available" and record.get("routable") is True:
            return True
    return False


def _snapshot_has_routable_model_family(
    snapshot: Mapping[str, Any], family: str, *, provider: str | None = None
) -> bool:
    for record in _catalog_records(snapshot):
        if provider is not None and str(record.get("provider", "")).lower() != provider:
            continue
        model = record.get("model_id", record.get("model", record.get("id", "")))
        if _model_family(model) != family:
            continue
        if record.get("status") == "available" and record.get("routable") is True:
            return True
    return False


def _restricted_catalog_families(
    snapshot: Mapping[str, Any], families: frozenset[str]
) -> dict[str, Any]:
    """Narrow one explicit lane without altering the immutable catalog."""

    result = dict(snapshot)
    for name in ("models", "capabilities", "records"):
        value = snapshot.get(name)
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
            continue
        records: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            record = dict(raw)
            model = record.get("model_id", record.get("model", record.get("id", "")))
            if _model_family(model) not in families:
                record["routable"] = False
            records.append(record)
        result[name] = records
        break
    return result


def _is_explicit_spark_lane(
    contract: TaskContract,
    strategy: RoutingStrategy,
    node_context: Mapping[str, Any] | None,
) -> bool:
    raw = dict(node_context or {})
    writes = raw.get("write_scopes")
    write_count = len(writes) if isinstance(writes, (tuple, list)) else 0
    # A ready node may depend on several completed nodes.  Dependency count is
    # a graph-readiness property, not a measure of whether the node itself is a
    # short mechanical action, so it must not suppress the dedicated Spark
    # lane.
    task_text = " ".join(
        [
            str(contract.objective),
            str(strategy.task_type),
            *(str(raw.get(field, "")) for field in ("title", "prompt", "task_type", "role", "routing_role")),
        ]
    ).lower()
    forbidden_markers = (
        "architecture",
        "architectural",
        "review",
        "security",
        "migration",
        "release",
        "cross-module",
        "cross module",
        "跨模块",
        "架构",
        "审核",
        "安全",
        "迁移",
        "发布",
    )
    if raw.get("verifier") is True or any(marker in task_text for marker in forbidden_markers):
        return False
    command = raw.get("command")
    has_node_command = isinstance(command, (tuple, list)) and any(
        isinstance(value, str) and value.strip() for value in command
    )
    has_task_command = any(
        isinstance(value, str) and value.strip()
        for value in contract.acceptance_commands
    )
    return (
        strategy.complexity == "low"
        and strategy.parallelizable
        and (has_task_command or has_node_command)
        and write_count <= 1
    )


def _filtered_catalog_for_admission(
    snapshot: Mapping[str, Any],
    *,
    claude_allowed: bool,
    claude_models_available: Iterable[str],
) -> dict[str, Any]:
    """Return a routing view without mutating the pinned catalog.

    The immutable catalog says which models are safe *in principle*.  Current
    Claude authentication/quota admission is a separate, short-lived input;
    only the transient routing view is narrowed by it.
    """

    admitted = frozenset(
        family
        for model in claude_models_available
        if (family := _family(str(model))) is not None
    )
    result = dict(snapshot)
    for name in ("models", "capabilities", "records"):
        value = snapshot.get(name)
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
            continue
        records: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            record = dict(raw)
            provider = str(record.get("provider", "")).lower()
            if provider in {"claude", "anthropic"}:
                model = record.get("model_id", record.get("model", record.get("id", "")))
                if not claude_allowed or _model_family(model) not in admitted:
                    record["routable"] = False
            records.append(record)
        result[name] = records
        break
    return result


def _quota_payload(
    snapshot: QuotaSnapshot | None,
    *,
    max_age_seconds: int | None,
) -> dict[str, Any]:
    """Translate the durable quota receipt into routing-v3's neutral shape."""

    if snapshot is None:
        return {
            "auth_ok": False,
            "authenticated": False,
            "native_subscription": False,
            "zone": "unknown",
        }
    fresh = max_age_seconds is None or snapshot.is_fresh(max_age_seconds=max_age_seconds)
    compatible = snapshot.has_compatible_subscription_provenance()
    values = [
        value
        for value in (
            snapshot.five_hour_remaining,
            snapshot.weekly_all_remaining,
            snapshot.weekly_sonnet_remaining,
            snapshot.weekly_fable_remaining,
        )
        if value is not None
    ]
    minimum = min(values) if values else None
    if not fresh or not compatible or not snapshot.auth_ok or snapshot.auth_method != "native-subscription":
        zone = "unknown"
    elif minimum is None:
        zone = "unknown"
    elif minimum <= 25:
        zone = "protected"
    elif minimum < 30:
        zone = "red"
    elif minimum <= 40:
        zone = "yellow"
    else:
        zone = "green"
    return {
        "auth_ok": snapshot.auth_ok and fresh and compatible,
        "authenticated": snapshot.auth_ok and fresh and compatible,
        "native_subscription": snapshot.auth_method == "native-subscription",
        "zone": zone,
        "five_hour_remaining": snapshot.five_hour_remaining,
        "weekly_all_remaining": snapshot.weekly_all_remaining,
        "weekly_sonnet_remaining": snapshot.weekly_sonnet_remaining,
        "weekly_fable_remaining": snapshot.weekly_fable_remaining,
        "observed_at": snapshot.observed_at,
    }


def _provider_capacity(
    quota_snapshot: QuotaSnapshot | None,
    *,
    active_models: tuple[str, ...],
    supplied: Mapping[str, Any] | None,
    max_age_seconds: int | None,
) -> dict[str, dict[str, float]]:
    """Supply conservative planning capacity; service remains the final gate."""

    result: dict[str, dict[str, float]] = {
        # The catalog's weights describe provider pressure, not coordinator
        # worker slots.  Four keeps every known Codex tier individually legal;
        # the service claim path still owns actual concurrency.
        "codex": {"capacity": 4.0, "active": 0.0},
        "claude": {"capacity": 2.0, "active": 0.0},
    }
    if isinstance(supplied, Mapping):
        for provider, raw in supplied.items():
            if not isinstance(raw, Mapping):
                continue
            capacity = raw.get("capacity", raw.get("max_concurrency"))
            active = raw.get("active", raw.get("active_count", raw.get("active_units")))
            try:
                parsed_capacity = float(capacity) if capacity is not None else None
            except (TypeError, ValueError):
                parsed_capacity = None
            try:
                parsed_active = float(active) if active is not None else None
            except (TypeError, ValueError):
                parsed_active = None
            target = result.setdefault(str(provider).lower(), {"capacity": 1.0, "active": 0.0})
            if parsed_capacity is not None:
                target["capacity"] = max(parsed_capacity, 0.0)
            if parsed_active is not None:
                target["active"] = max(parsed_active, 0.0)
    result["codex"]["capacity"] = max(result["codex"]["capacity"], 4.0)
    if quota_snapshot is not None:
        quota_decision = quota_snapshot.dispatch_decision(
            "sonnet",
            active_models,
            max_age_seconds=max_age_seconds,
        )
        if quota_decision.capacity_units:
            result["claude"]["capacity"] = min(
                result["claude"]["capacity"], float(quota_decision.capacity_units)
            )
        result["claude"]["active"] = float(quota_decision.active_units)
    return result


def _v3_preferred_families(strategy: RoutingStrategy, *, role: RoutingRole) -> tuple[str, ...]:
    task_type = strategy.task_type
    complexity = strategy.complexity
    if role == "challenge":
        if task_type == "review":
            return ("opus", "fable", "sonnet")
        if task_type in {"architecture", "creative"} or (
            task_type == "exploration" and complexity == "high"
        ):
            return ("fable", "opus", "terra")
    if complexity == "low":
        return ("spark", "luna")
    if task_type in {"tests", "docs"}:
        return ("luna", "sonnet", "terra")
    if task_type in {"implementation", "debugging"} and complexity == "standard":
        return ("sonnet", "luna", "terra")
    if task_type == "exploration" and complexity == "standard":
        return ("sonnet", "luna", "terra")
    return ("terra", "luna", "sonnet")


def _v3_role_for(strategy: RoutingStrategy, *, requested_role: RoutingRole) -> RoutingRole:
    if requested_role != "worker":
        return requested_role
    if strategy.task_type in {"architecture", "review", "creative"}:
        return "challenge"
    if strategy.task_type == "exploration" and strategy.complexity == "high":
        return "challenge"
    return "worker"


def _v3_request(
    contract: TaskContract,
    strategy: RoutingStrategy,
    *,
    role: RoutingRole,
    node_context: Mapping[str, Any] | None,
    quota_snapshot: QuotaSnapshot | None,
    active_models: tuple[str, ...],
    max_age_seconds: int | None,
    provider_capacity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(node_context or {})
    writes = raw.get("write_scopes")
    write_count = len(writes) if isinstance(writes, (tuple, list)) else 0
    node_command = raw.get("command")
    mechanical = any(
        isinstance(value, str) and value.strip()
        for value in contract.acceptance_commands
    ) or (
        isinstance(node_command, (tuple, list))
        and any(isinstance(value, str) and value.strip() for value in node_command)
    )
    is_control_plane = role in {"planner", "verifier", "control"}
    # Current passive Sol metadata intentionally advertises architecture and
    # exploration control work rather than every worker task type.  Planning
    # and verification therefore use its control-plane contract, not the
    # worker's task type.
    task_type = "architecture" if is_control_plane else strategy.task_type
    low = strategy.complexity == "low"
    bounded = bool(contract.allowed_scope) and strategy.complexity in {"low", "standard"}
    independent = strategy.parallelizable and bool(contract.allowed_scope)
    return {
        "role": role,
        "control_plane_model": (
            (contract.verifier_model if role == "verifier" else contract.planner_model)
            or CODEX_SOL_MODEL
        ) if is_control_plane else None,
        "task_type": task_type,
        "complexity": strategy.complexity,
        "quality_floor": 60 if low and mechanical else 80,
        "acceptance_risk": strategy.complexity,
        "low_risk": low,
        "short_task": low and strategy.parallelizable and write_count <= 1 and mechanical,
        "mechanically_verifiable": mechanical,
        "bounded": bounded,
        "independent_slice": independent,
        "allow_parallel_providers": strategy.parallelizable and not is_control_plane,
        "preferred_families": _v3_preferred_families(strategy, role=role),
        "performance_snapshot_id": contract.performance_snapshot_id,
        "performance_digest": contract.performance_digest,
        "performance_status": contract.performance_status,
        "performance_calibration": raw.get("performance_calibration"),
        "claude_quota": _quota_payload(quota_snapshot, max_age_seconds=max_age_seconds),
        "provider_capacity": _provider_capacity(
            quota_snapshot,
            active_models=active_models,
            supplied=provider_capacity,
            max_age_seconds=max_age_seconds,
        ),
    }


def _preferred_effort(snapshot: Mapping[str, Any], provider: str, model: str) -> str | None:
    for record in _catalog_records(snapshot):
        record_provider = str(record.get("provider", "")).lower()
        record_model = str(record.get("model_id", record.get("model", record.get("id", ""))))
        if record_provider != provider or record_model != model:
            continue
        reasoning = record.get("reasoning")
        if isinstance(reasoning, Mapping):
            value = reasoning.get("preferred_effort")
            return str(value) if isinstance(value, str) and value else None
    return None


def _agent_capability_id(snapshot: Mapping[str, Any], provider: str) -> str:
    agents = snapshot.get("agents")
    if isinstance(agents, Mapping):
        agent = agents.get(provider)
        if isinstance(agent, Mapping):
            declared = agent.get("capability_id")
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
            version = agent.get("cli_version")
            if isinstance(version, str) and version.strip():
                return f"{provider}-cli:{version.strip()}"
    return f"{provider}-cli:observed"


def _agent_identity(snapshot: Mapping[str, Any], provider: str) -> tuple[str, str | None]:
    agents = snapshot.get("agents")
    if isinstance(agents, Mapping):
        agent = agents.get(provider)
        if isinstance(agent, Mapping):
            name = agent.get("name", agent.get("agent_name", f"{provider}-cli"))
            version = agent.get("cli_version", agent.get("version"))
            return (
                str(name) if isinstance(name, str) and name else f"{provider}-cli",
                str(version) if isinstance(version, str) and version else None,
            )
    return f"{provider}-cli", None


def _decision_from_v3(
    decision: RoutingV3Decision,
    *,
    contract: TaskContract,
    role: RoutingRole,
    strategy: RoutingStrategy,
    snapshot: Mapping[str, Any],
    reason_prefix: str = "",
) -> RoutingDecision:
    if not decision.accepted or decision.selected is None:
        raise ValueError(
            "routing-v3 has no legal worker in pinned catalog "
            f"{decision.catalog_snapshot_id}: {decision.reason}"
        )
    selected = decision.selected
    provider = "claude" if selected.provider in {"claude", "anthropic"} else selected.provider
    if provider not in {"codex", "claude"}:
        raise ValueError(f"routing-v3 selected unsupported executor provider {selected.provider!r}")
    agent_name, agent_version = _agent_identity(snapshot, provider)
    performance_routing_receipt = {
        "policy_version": decision.policy_version,
        "ranking_algorithm_version": decision.ranking_algorithm_version,
        "performance_semantic_status": selected.performance_semantic_status,
        "empirical_ranking_status": selected.empirical_ranking_status,
        "empirical_ranking_reason": selected.empirical_ranking_reason,
        "public_evidence_summary": dict(decision.public_evidence_summary),
        # Existing v3 source labels are intentionally preserved: local-runtime
        # is observed evidence; declared-policy is a policy-only fallback.
        "source": selected.quality_source,
    }
    return RoutingDecision(
        role=role,
        executor=provider,
        model=selected.model,
        strategy_version=strategy.version,
        reason=f"{reason_prefix}{decision.reason}",
        claude_eligible=any(candidate.provider in {"claude", "anthropic"} for candidate in decision.ranked_candidates),
        model_reasoning_effort=_preferred_effort(snapshot, selected.provider, selected.model),
        capability_snapshot_id=decision.catalog_snapshot_id,
        capability_digest=decision.catalog_digest,
        model_capability_id=selected.capability_id,
        agent_capability_id=_agent_capability_id(snapshot, provider),
        agent_name=agent_name,
        agent_version=agent_version,
        routing_policy_version=decision.policy_version,
        performance_snapshot_id=(
            decision.performance_snapshot_id or contract.performance_snapshot_id
        ),
        performance_digest=(decision.performance_digest or contract.performance_digest),
        performance_status=(decision.performance_status or contract.performance_status),
        quality_source=selected.quality_source,
        performance_routing_receipt=performance_routing_receipt,
        performance_lower_bound_95=selected.performance_lower_bound_95,
        runtime_sample_count=selected.runtime_sample_count,
        performance_first_pass_rate=selected.performance_first_pass_rate,
        performance_rework_rate=selected.performance_rework_rate,
        performance_latency_ms=selected.performance_latency_ms,
    )


def _quota_blocks_challenge(
    quota_snapshot: QuotaSnapshot | None,
    *,
    families: tuple[str, ...],
    active_models: tuple[str, ...],
    max_age_seconds: int | None,
) -> bool:
    if quota_snapshot is None:
        return True
    decisions = [
        quota_snapshot.dispatch_decision(
            family,
            active_models,
            max_age_seconds=max_age_seconds,
        )
        for family in families
    ]
    # A full shared pool is a scheduling wait, not a reason to turn a worker
    # into Sol.  Authentication, freshness, reserve, and quota-zone refusal
    # are the explicit control-plane fallback condition.
    return bool(decisions) and all(
        decision.action != "claude" and decision.zone in {"unknown", "auth-unavailable", "protected", "red", "yellow"}
        for decision in decisions
    )


def _route_catalog_task(
    contract: TaskContract,
    *,
    strategy: RoutingStrategy,
    role: RoutingRole,
    capability_snapshot: Mapping[str, Any],
    claude_models_available: tuple[str, ...],
    quota_snapshot: QuotaSnapshot | None,
    active_models: tuple[str, ...],
    max_age_seconds: int | None,
    node_context: Mapping[str, Any] | None,
    provider_capacity: Mapping[str, Any] | None,
    performance_calibration: Mapping[str, Any] | None,
) -> RoutingDecision:
    snapshot_id, digest = _catalog_identity(capability_snapshot)
    if contract.capability_snapshot_id is not None and contract.capability_snapshot_id != snapshot_id:
        raise ValueError("pinned task capability catalog does not match the routing catalog")
    if contract.capability_digest is not None and contract.capability_digest != digest:
        raise ValueError("pinned task capability digest does not match the routing catalog")

    selected_role = _v3_role_for(strategy, requested_role=role)
    routing_view = _filtered_catalog_for_admission(
        capability_snapshot,
        claude_allowed=strategy.claude_allowed,
        claude_models_available=claude_models_available,
    )
    # Spark is an explicit short/mechanical lane, not a cheap replacement for
    # work with ambiguous acceptance.  Once a node has passed that strict
    # classification and the independent pool is observed, route only within
    # that lane; otherwise the normal quality-first candidate set remains.
    if (
        selected_role == "worker"
        and _is_explicit_spark_lane(contract, strategy, node_context)
        and _snapshot_has_routable_model_family(
            capability_snapshot, "spark", provider="codex"
        )
    ):
        routing_view = _restricted_catalog_families(routing_view, frozenset({"spark"}))
    request = _v3_request(
        contract,
        strategy,
        role=selected_role,
        node_context=node_context,
        quota_snapshot=quota_snapshot,
        active_models=active_models,
        max_age_seconds=max_age_seconds,
        provider_capacity=provider_capacity,
    )
    if performance_calibration is not None:
        request["performance_calibration"] = dict(performance_calibration)
    decision = route_capability_snapshot(
        routing_view,
        request,
        active_model_ids=active_models,
        policy_version=ROUTING_V3_POLICY_VERSION,
    )
    if decision.accepted:
        return _decision_from_v3(
            decision,
            contract=contract,
            role=selected_role,
            strategy=strategy,
            snapshot=capability_snapshot,
        )

    # Claude challenge capacity is deliberately optional.  For cross-module
    # architecture/review/research work, a genuine Claude auth/quota refusal
    # may fall back to the exact, catalog-proven Sol control plane.  This is
    # not an ordinary worker retry and never routes an unknown model.
    control_types = {"architecture", "review"}
    if strategy.task_type == "exploration" and strategy.complexity == "high":
        control_types.add("exploration")
    if selected_role == "challenge" and strategy.task_type in control_types:
        challenge_families = _v3_preferred_families(strategy, role="challenge")
        claude_families = tuple(
            family for family in challenge_families if family in CLAUDE_FAMILIES
        )
        control_reason: str | None = None
        if not strategy.claude_allowed:
            control_reason = (
                "Claude is disabled by the immutable task contract; "
                "using the exact Sol cross-module control plane: "
            )
        elif (
            _snapshot_has_routable_family(capability_snapshot, claude_families)
            and _quota_blocks_challenge(
                quota_snapshot,
                families=claude_families,
                active_models=active_models,
                max_age_seconds=max_age_seconds,
            )
        ):
            control_reason = (
                "Claude challenge was unavailable due to authenticated quota admission; "
                "using the exact Sol cross-module control plane: "
            )
        if control_reason is not None:
            control_request = _v3_request(
                contract,
                strategy,
                role="control",
                node_context=node_context,
                quota_snapshot=quota_snapshot,
                active_models=active_models,
                max_age_seconds=max_age_seconds,
                provider_capacity=provider_capacity,
            )
            control_decision = route_capability_snapshot(
                routing_view,
                control_request,
                active_model_ids=active_models,
                policy_version=ROUTING_V3_POLICY_VERSION,
            )
            if control_decision.accepted:
                return _decision_from_v3(
                    control_decision,
                    contract=contract,
                    role="control",
                    strategy=strategy,
                    snapshot=capability_snapshot,
                    reason_prefix=control_reason,
                )
    raise ValueError(
        "routing-v3 has no legal worker in the pinned catalog; "
        f"{decision.reason}"
    )


def route_task(
    contract: TaskContract,
    claude_models_available: tuple[str, ...] = (),
    *,
    role: RoutingRole = "worker",
    quota_snapshot: QuotaSnapshot | None = None,
    active_models: tuple[str, ...] = (),
    max_age_seconds: int | None = DEFAULT_QUOTA_TTL_SECONDS,
    strategy: RoutingStrategy | dict[str, Any] | None = None,
    available_claude_models: tuple[str, ...] | None = None,
    capability_snapshot: Mapping[str, Any] | None = None,
    node_context: Mapping[str, Any] | None = None,
    provider_capacity: Mapping[str, Any] | None = None,
    performance_calibration: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    """Select a model from immutable contract inputs.

    ``claude_models_available`` is an admission result, not a hint.  If a
    quota snapshot is also supplied, it is checked again here, making this
    function safe to use at the planner boundary and in deterministic tests.
    """

    contract.validate()
    selected_strategy = _strategy_for(contract, strategy)
    if available_claude_models is not None:
        claude_models_available = tuple(available_claude_models)
    version = selected_strategy.version

    if capability_snapshot is not None:
        return _route_catalog_task(
            contract,
            strategy=selected_strategy,
            role=role,
            capability_snapshot=capability_snapshot,
            claude_models_available=claude_models_available,
            quota_snapshot=quota_snapshot,
            active_models=active_models,
            max_age_seconds=max_age_seconds,
            node_context=node_context,
            provider_capacity=provider_capacity,
            performance_calibration=performance_calibration,
        )

    if role in {"planner", "verifier"}:
        return RoutingDecision(
            role=role,
            executor="codex",
            model=(contract.verifier_model if role == "verifier" else contract.planner_model) or CODEX_SOL_MODEL,
            strategy_version=version,
            reason=f"{role} role uses the exact control-plane model in the task contract",
            **_contract_performance_kwargs(contract),
        )

    if role == "control":
        return RoutingDecision(
            role=role,
            executor="codex",
            model=contract.planner_model or CODEX_SOL_MODEL,
            strategy_version=version,
            reason="control role uses the exact cross-module model in the task contract",
            **_contract_performance_kwargs(contract),
        )

    if role not in {"worker", "challenge"}:
        raise ValueError(f"unsupported routing role: {role!r}")

    task_type = selected_strategy.task_type
    complexity = selected_strategy.complexity
    if version == LEGACY_ROUTING_STRATEGY_VERSION:
        claude_eligible = selected_strategy.claude_allowed and (
            complexity == "high" or task_type in {"architecture", "review"}
        )
    else:
        # v2 treats paid Claude capacity as a productive worker pool while
        # retaining the independent Spark pool for the cheapest bounded work.
        # Quota admission and concurrency remain enforced below and again
        # immediately before run.
        claude_eligible = (
            selected_strategy.claude_allowed
            and complexity != "low"
            and (
                complexity == "high"
                or task_type in {"architecture", "review", "creative"}
                or (
                    complexity == "standard"
                    and task_type
                    in {"implementation", "debugging", "tests", "docs", "exploration"}
                )
            )
        )

    if claude_eligible:
        if version == LEGACY_ROUTING_STRATEGY_VERSION and task_type == "creative":
            candidates = ("fable", "opus")
        elif task_type == "architecture":
            candidates = (
                ("opus", "sonnet")
                if version == LEGACY_ROUTING_STRATEGY_VERSION
                else ("opus", "fable", "sonnet")
            )
        elif task_type == "review":
            candidates = (
                ("sonnet", "opus")
                if version == LEGACY_ROUTING_STRATEGY_VERSION
                else ("opus", "fable", "sonnet")
            )
        elif task_type == "creative":
            candidates = ("fable", "opus", "sonnet")
        elif version != LEGACY_ROUTING_STRATEGY_VERSION and complexity == "standard":
            candidates = ("sonnet",)
        else:
            candidates = ("opus", "sonnet")
        fallback_reason = "no eligible Claude family is admitted"
        for candidate_family in candidates:
            candidate = _first_available((candidate_family,), claude_models_available)
            if candidate is None:
                continue
            permitted, quota_reason = _claude_permitted(
                candidate,
                claude_models_available=claude_models_available,
                quota_snapshot=quota_snapshot,
                active_models=active_models,
                max_age_seconds=max_age_seconds,
                shared_capacity=version != LEGACY_ROUTING_STRATEGY_VERSION,
            )
            if permitted:
                return RoutingDecision(
                    role=role,
                    executor="claude",
                    model=candidate,
                    strategy_version=version,
                    reason=(
                        f"declared {complexity} {task_type} work is eligible for "
                        f"admitted Claude {candidate} ({quota_reason})"
                    ),
                    claude_eligible=True,
                    **_contract_performance_kwargs(contract),
                )
            fallback_reason = quota_reason
    else:
        fallback_reason = (
            "task is not admitted to Claude by the versioned routing strategy"
        )

    fallback_model, fallback_label = _codex_fallback_base_model(selected_strategy)
    if fallback_model == CODEX_SPARK_MODEL and (
        role != "worker"
        or not _is_explicit_spark_lane(
            contract,
            selected_strategy,
            node_context,
        )
    ):
        fallback_model = CODEX_LUNA_MODEL
        fallback_label = "low-complexity work lacks a mechanical Spark contract"
    return RoutingDecision(
        role=role,
        executor="codex",
        model=fallback_model,
        strategy_version=version,
        reason=f"{fallback_label} uses Codex {fallback_model}; {fallback_reason}",
        claude_eligible=claude_eligible,
        **_contract_performance_kwargs(contract),
    )


def route_node(
    contract: TaskContract,
    node: Any,
    claude_models_available: tuple[str, ...] = (),
    *,
    quota_snapshot: QuotaSnapshot | None = None,
    active_models: tuple[str, ...] = (),
    max_age_seconds: int | None = DEFAULT_QUOTA_TTL_SECONDS,
    strategy: RoutingStrategy | dict[str, Any] | None = None,
    capability_snapshot: Mapping[str, Any] | None = None,
    provider_capacity: Mapping[str, Any] | None = None,
    performance_calibration: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    """Route one planner node while preserving deterministic executors."""

    raw = node.to_dict() if hasattr(node, "to_dict") else dict(node)
    node_strategy = strategy_for_node(contract, raw, strategy)
    if raw.get("verifier"):
        return route_task(
            contract,
            claude_models_available,
            role="verifier",
            quota_snapshot=quota_snapshot,
            active_models=active_models,
            max_age_seconds=max_age_seconds,
            strategy=node_strategy,
            capability_snapshot=capability_snapshot,
            node_context=raw,
            provider_capacity=provider_capacity,
            performance_calibration=performance_calibration,
        )
    executor = raw.get("executor")
    if executor in {"deterministic", "fixture"}:
        return RoutingDecision(
            role="worker",
            executor=executor,
            model=str(raw.get("model") or executor),
            strategy_version=node_strategy.version,
            reason=f"{executor} is an explicit non-model execution node",
            **_contract_performance_kwargs(contract),
        )
    return route_task(
        contract,
        claude_models_available,
        role="worker",
        quota_snapshot=quota_snapshot,
        active_models=active_models,
        max_age_seconds=max_age_seconds,
        strategy=node_strategy,
        capability_snapshot=capability_snapshot,
        node_context=raw,
        provider_capacity=provider_capacity,
        performance_calibration=performance_calibration,
    )


class ModelRoutingPolicy:
    """Reusable policy object for callers that route several nodes."""

    def __init__(
        self,
        *,
        claude_models_available: tuple[str, ...] = (),
        quota_snapshot: QuotaSnapshot | None = None,
        active_models: tuple[str, ...] = (),
        max_age_seconds: int | None = DEFAULT_QUOTA_TTL_SECONDS,
        capability_snapshot: Mapping[str, Any] | None = None,
        provider_capacity: Mapping[str, Any] | None = None,
        performance_calibration: Mapping[str, Any] | None = None,
    ) -> None:
        self.claude_models_available = tuple(claude_models_available)
        self.quota_snapshot = quota_snapshot
        self.active_models = tuple(active_models)
        self.max_age_seconds = max_age_seconds
        self.capability_snapshot = capability_snapshot
        self.provider_capacity = provider_capacity
        self.performance_calibration = performance_calibration

    def route(
        self,
        contract: TaskContract,
        *,
        role: RoutingRole = "worker",
        strategy: RoutingStrategy | dict[str, Any] | None = None,
    ) -> RoutingDecision:
        return route_task(
            contract,
            self.claude_models_available,
            role=role,
            quota_snapshot=self.quota_snapshot,
            active_models=self.active_models,
            max_age_seconds=self.max_age_seconds,
            strategy=strategy,
            capability_snapshot=self.capability_snapshot,
            provider_capacity=self.provider_capacity,
            performance_calibration=self.performance_calibration,
        )


# Compatibility names for integrations that call this a router or policy.
RoutingPolicy = ModelRoutingPolicy
ModelRoutingStrategy = RoutingStrategy
route_model = route_task
select_route = route_task


__all__ = [
    "CLAUDE_FAMILIES",
    "CODEX_LUNA_MODEL",
    "CODEX_SPARK_MODEL",
    "CODEX_TERRA_MODEL",
    "codex_fallback_model",
    "ModelRoutingPolicy",
    "ModelRoutingStrategy",
    "ROUTING_STRATEGY_VERSION",
    "RouteDecision",
    "RoutingDecision",
    "RoutingPolicy",
    "route_model",
    "route_node",
    "route_task",
    "strategy_for_node",
    "select_route",
]
