"""Deterministic model routing for planner-generated task graphs.

The planner remains responsible for decomposing a request into a DAG.  This
module owns the provider/model decision for each role so a model cannot widen
the routing policy merely by returning a different JSON value.
"""

from __future__ import annotations

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
    retry_model,
)


CODEX_LUNA_MODEL = "gpt-5.6-luna"
CODEX_SPARK_MODEL = "gpt-5.3-codex-spark"
CODEX_TERRA_MODEL = "gpt-5.6-terra"
CLAUDE_FAMILIES = ("opus", "sonnet", "fable")
RoutingRole = Literal["planner", "worker", "verifier", "challenge"]


@dataclass(frozen=True)
class RoutingDecision:
    """The complete, serializable result of one routing decision."""

    role: RoutingRole
    executor: Literal["codex", "claude", "deterministic", "fixture"]
    model: str
    strategy_version: str
    reason: str
    claude_eligible: bool = False

    @property
    def provider(self) -> str:
        return self.executor

    @property
    def policy_version(self) -> str:
        return self.strategy_version

    @property
    def selected_model(self) -> str:
        return self.model

    @property
    def selected_executor(self) -> str:
        return self.executor

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


def _codex_fallback_base_model(strategy: RoutingStrategy) -> tuple[str, str]:
    """Select the first Codex worker tier without consuming retry budget.

    v1 intentionally keeps its former implementation-only Luna preference.
    v2 assigns bounded low work to Spark and the next inexpensive tier to
    standard, splittable production work;
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

    The first attempt follows the versioned routing tier. Later attempts use
    Spark -> Luna -> Terra -> Sol or Luna -> Terra -> Sol escalation without
    changing a high-complexity first attempt from Terra into a cheaper tier.
    """

    if attempt <= 0:
        raise ValueError("routing attempt must be positive")
    selected_strategy = _strategy_for(contract, strategy)
    base_model, _ = _codex_fallback_base_model(selected_strategy)
    return retry_model(base_model, attempt)


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

    if role in {"planner", "verifier"}:
        return RoutingDecision(
            role=role,
            executor="codex",
            model=CODEX_SOL_MODEL,
            strategy_version=version,
            reason=f"{role} role is fixed to the independent Codex Sol control plane",
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
                )
            fallback_reason = quota_reason
    else:
        fallback_reason = (
            "task is not admitted to Claude by the versioned routing strategy"
        )

    fallback_model, fallback_label = _codex_fallback_base_model(selected_strategy)
    return RoutingDecision(
        role=role,
        executor="codex",
        model=fallback_model,
        strategy_version=version,
        reason=f"{fallback_label} uses Codex {fallback_model}; {fallback_reason}",
        claude_eligible=claude_eligible,
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
) -> RoutingDecision:
    """Route one planner node while preserving deterministic executors."""

    raw = node.to_dict() if hasattr(node, "to_dict") else dict(node)
    if raw.get("verifier"):
        return route_task(
            contract,
            claude_models_available,
            role="verifier",
            quota_snapshot=quota_snapshot,
            active_models=active_models,
            max_age_seconds=max_age_seconds,
            strategy=strategy,
        )
    executor = raw.get("executor")
    if executor in {"deterministic", "fixture"}:
        return RoutingDecision(
            role="worker",
            executor=executor,
            model=str(raw.get("model") or executor),
            strategy_version=_strategy_for(contract, strategy).version,
            reason=f"{executor} is an explicit non-model execution node",
        )
    return route_task(
        contract,
        claude_models_available,
        role="worker",
        quota_snapshot=quota_snapshot,
        active_models=active_models,
        max_age_seconds=max_age_seconds,
        strategy=strategy,
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
    ) -> None:
        self.claude_models_available = tuple(claude_models_available)
        self.quota_snapshot = quota_snapshot
        self.active_models = tuple(active_models)
        self.max_age_seconds = max_age_seconds

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
    "select_route",
]
