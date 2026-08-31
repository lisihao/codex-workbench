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
    QuotaSnapshot,
    RoutingStrategy,
    TaskContract,
    ROUTING_STRATEGY_VERSION,
)


CODEX_LUNA_MODEL = "gpt-5.6-luna"
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
) -> tuple[bool, str]:
    family = _family(model)
    if family is None:
        return False, "requested model is not a Claude family"
    if not any(_family(candidate) == family for candidate in claude_models_available):
        return False, f"Claude {family} is not admitted by the current strategy context"
    if quota_snapshot is None:
        # Submission computes this list only after native authentication and
        # quota admission.  A caller that supplies it directly is providing
        # that already-admitted context.
        return True, f"Claude {family} is pre-admitted by the strategy context"
    quota_decision = quota_snapshot.dispatch_decision(
        family,
        active_models,
        max_age_seconds=max_age_seconds,
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
    claude_eligible = selected_strategy.claude_allowed and (
        complexity == "high" or task_type in {"architecture", "review"}
    )

    if claude_eligible:
        if task_type == "architecture":
            candidates = ("opus", "sonnet")
        elif task_type == "review":
            candidates = ("sonnet", "opus")
        elif task_type == "creative":
            candidates = ("fable", "opus")
        else:
            candidates = ("opus", "sonnet")
        candidate = None
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
        candidate = None
    else:
        fallback_reason = "task is not an explicitly high-complexity, architecture, or review route"

    if (
        task_type == "implementation"
        and complexity in {"low", "standard"}
        and selected_strategy.parallelizable
    ):
        fallback_model = CODEX_LUNA_MODEL
        fallback_label = "low-risk/splittable implementation"
    elif complexity == "low" and task_type in {"implementation", "tests", "docs"}:
        fallback_model = CODEX_LUNA_MODEL
        fallback_label = "bounded low-complexity work"
    else:
        fallback_model = CODEX_TERRA_MODEL
        fallback_label = "complex or non-mechanical work"
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
    "CODEX_TERRA_MODEL",
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
