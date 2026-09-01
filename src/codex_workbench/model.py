from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Literal

from .governance import (
    CODE_AS_HARNESS_PROFILE,
    DEFAULT_VERIFICATION_TIER,
    VerificationTier,
    governance_identity,
)


TaskState = Literal[
    "inbox",
    "planning",
    "ready",
    "queued",
    "running",
    "verifying",
    "accepted",
    "needs_fix",
    "blocked",
    "needs_approval",
    "paused",
    "cancelled",
]

NodeState = Literal[
    "pending",
    "queued",
    "running",
    "verifying",
    "accepted",
    "failed",
    "blocked",
    "indeterminate",
    "cancelled",
]

ClaudeQuotaZone = Literal[
    "green",
    "yellow",
    "red",
    "protected",
    "unknown",
    "auth-unavailable",
]
ClaudeDispatchAction = Literal["claude", "codex", "defer"]

TERMINAL_TASK_STATES = {"accepted", "blocked", "cancelled"}
TERMINAL_NODE_STATES = {"accepted", "failed", "blocked", "indeterminate", "cancelled"}
DEFAULT_QUOTA_TTL_SECONDS = 15 * 60
LEGACY_ROUTING_STRATEGY_VERSION = "model-routing-v1"
ROUTING_STRATEGY_VERSION = "model-routing-v2"
CODEX_SOL_MODEL = "gpt-5.6-sol"


RoutingTaskType = Literal[
    "implementation",
    "debugging",
    "architecture",
    "review",
    "tests",
    "docs",
    "creative",
    "exploration",
]
RoutingComplexity = Literal["low", "standard", "high"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class RoutingStrategy:
    """Versioned, serializable inputs that control model selection.

    The planner may describe a graph, but it cannot choose a provider outside
    this contract.  Keeping these fields small also makes a strategy part of a
    task's durable request hash.
    """

    version: str = ROUTING_STRATEGY_VERSION
    task_type: RoutingTaskType = "implementation"
    complexity: RoutingComplexity = "standard"
    parallelizable: bool = True
    claude_allowed: bool = True

    def normalized(self) -> "RoutingStrategy":
        if not isinstance(self.version, str):
            raise ValueError("routing strategy version must be a string")
        if not isinstance(self.task_type, str):
            raise ValueError("routing task_type must be a string")
        if not isinstance(self.complexity, str):
            raise ValueError("routing complexity must be a string")
        version = {
            "v1": LEGACY_ROUTING_STRATEGY_VERSION,
            "routing-v1": LEGACY_ROUTING_STRATEGY_VERSION,
            "routing.v1": LEGACY_ROUTING_STRATEGY_VERSION,
            LEGACY_ROUTING_STRATEGY_VERSION: LEGACY_ROUTING_STRATEGY_VERSION,
            "v2": ROUTING_STRATEGY_VERSION,
            "routing-v2": ROUTING_STRATEGY_VERSION,
            "routing.v2": ROUTING_STRATEGY_VERSION,
            ROUTING_STRATEGY_VERSION: ROUTING_STRATEGY_VERSION,
        }.get(self.version)
        if version is None:
            raise ValueError(f"unsupported routing strategy version: {self.version!r}")
        task_type = {
            "feature": "implementation",
            "fix": "debugging",
            "bugfix": "debugging",
            "documentation": "docs",
        }.get(self.task_type, self.task_type)
        complexity = {
            "medium": "standard",
            "normal": "standard",
            "default": "standard",
        }.get(self.complexity, self.complexity)
        if task_type not in {
            "implementation",
            "debugging",
            "architecture",
            "review",
            "tests",
            "docs",
            "creative",
            "exploration",
        }:
            raise ValueError(f"unsupported routing task_type: {self.task_type!r}")
        if complexity not in {"low", "standard", "high"}:
            raise ValueError(f"unsupported routing complexity: {self.complexity!r}")
        if not isinstance(self.parallelizable, bool):
            raise ValueError("parallelizable must be a boolean")
        if not isinstance(self.claude_allowed, bool):
            raise ValueError("claude_allowed must be a boolean")
        return RoutingStrategy(
            version=version,
            task_type=task_type,  # type: ignore[arg-type]
            complexity=complexity,  # type: ignore[arg-type]
            parallelizable=self.parallelizable,
            claude_allowed=self.claude_allowed,
        )

    def validate(self) -> None:
        self.normalized()

    @property
    def strategy_version(self) -> str:
        return self.normalized().version

    @property
    def task_kind(self) -> str:
        return self.normalized().task_type

    @property
    def risk_level(self) -> str:
        return self.normalized().complexity

    @property
    def allow_claude(self) -> bool:
        return self.normalized().claude_allowed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoutingStrategy":
        aliases = {
            "strategy_version": "version",
            "work_type": "task_type",
            "task_kind": "task_type",
            "kind": "task_type",
            "task_category": "task_type",
            "complexity_level": "complexity",
            "risk": "complexity",
            "risk_level": "complexity",
            "allow_claude": "claude_allowed",
            "claude_enabled": "claude_allowed",
            "claude": "claude_allowed",
            "parallel": "parallelizable",
        }
        normalized = {
            aliases.get(key, key): value
            for key, value in raw.items()
        }
        return cls(**normalized).normalized()


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    repository: str
    base_sha: str
    objective: str
    allowed_scope: tuple[str, ...]
    forbidden_scope: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    acceptance_commands: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ("diff", "test-log", "verdict")
    planner_model: str = "gpt-5.6-sol"
    executor_model: str = "gpt-5.6-luna"
    verifier_model: str = "gpt-5.6-sol"
    quota_class: str = "codex-subscription"
    timeout_seconds: int = 3600
    retry_limit: int = 3
    external_write_permission: bool = False
    destructive_action_permission: bool = False
    routing_strategy: str = ROUTING_STRATEGY_VERSION
    task_type: RoutingTaskType = "implementation"
    complexity: RoutingComplexity = "standard"
    parallelizable: bool = True
    claude_allowed: bool = True
    task_points: float = 1.0
    governance_profile: str = CODE_AS_HARNESS_PROFILE
    verification_tier: VerificationTier = DEFAULT_VERIFICATION_TIER
    source_thread_id: str | None = None
    context_bundle_ref: str | None = None

    def __post_init__(self) -> None:
        # Sol is a role invariant.  ``fixture`` remains available for the
        # repository's offline demonstrations and tests.
        if self.planner_model != "fixture":
            object.__setattr__(self, "planner_model", CODEX_SOL_MODEL)
        if self.verifier_model != "fixture":
            object.__setattr__(self, "verifier_model", CODEX_SOL_MODEL)
        if isinstance(self.routing_strategy, dict):
            strategy = RoutingStrategy.from_dict(self.routing_strategy)
            object.__setattr__(self, "routing_strategy", strategy.version)
            object.__setattr__(self, "task_type", strategy.task_type)
            object.__setattr__(self, "complexity", strategy.complexity)
            object.__setattr__(self, "parallelizable", strategy.parallelizable)
            object.__setattr__(self, "claude_allowed", strategy.claude_allowed)
        elif isinstance(self.routing_strategy, RoutingStrategy):
            strategy = self.routing_strategy.normalized()
            object.__setattr__(self, "routing_strategy", strategy.version)
            object.__setattr__(self, "task_type", strategy.task_type)
            object.__setattr__(self, "complexity", strategy.complexity)
            object.__setattr__(self, "parallelizable", strategy.parallelizable)
            object.__setattr__(self, "claude_allowed", strategy.claude_allowed)
        else:
            strategy = RoutingStrategy(
                version=self.routing_strategy,
                task_type=self.task_type,
                complexity=self.complexity,
                parallelizable=self.parallelizable,
                claude_allowed=self.claude_allowed,
            ).normalized()
            object.__setattr__(self, "routing_strategy", strategy.version)
            object.__setattr__(self, "task_type", strategy.task_type)
            object.__setattr__(self, "complexity", strategy.complexity)

    def validate(self) -> None:
        if not self.task_id or any(ch.isspace() for ch in self.task_id):
            raise ValueError("task_id must be non-empty and contain no whitespace")
        repo = Path(self.repository).expanduser()
        if not repo.is_absolute():
            raise ValueError("repository must be an absolute path")
        if not self.base_sha:
            raise ValueError("base_sha is required")
        if not self.objective.strip():
            raise ValueError("objective is required")
        if not self.allowed_scope:
            raise ValueError("allowed_scope must not be empty")
        if bool(self.source_thread_id) != bool(self.context_bundle_ref):
            raise ValueError(
                "source_thread_id and context_bundle_ref must be supplied together"
            )
        if self.source_thread_id and any(ch.isspace() for ch in self.source_thread_id):
            raise ValueError("source_thread_id must contain no whitespace")
        if self.context_bundle_ref and not self.context_bundle_ref.startswith("sha256:"):
            raise ValueError("context_bundle_ref must be a content-addressed artifact ref")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= self.retry_limit <= 3:
            raise ValueError("retry_limit must be between 0 and 3")
        if not isinstance(self.task_points, (int, float)) or not math.isfinite(
            float(self.task_points)
        ) or float(self.task_points) <= 0:
            raise ValueError("task_points must be a positive finite number")
        governance_identity(asdict(self))
        self.strategy.validate()

    @property
    def strategy(self) -> RoutingStrategy:
        return RoutingStrategy(
            version=self.routing_strategy,
            task_type=self.task_type,
            complexity=self.complexity,
            parallelizable=self.parallelizable,
            claude_allowed=self.claude_allowed,
        ).normalized()

    @property
    def routing(self) -> RoutingStrategy:
        """Compatibility alias for callers that call the policy ``routing``."""
        return self.strategy

    @property
    def strategy_version(self) -> str:
        return self.strategy.version

    @property
    def task_kind(self) -> str:
        return self.task_type

    @property
    def risk_level(self) -> str:
        return self.complexity

    @property
    def allow_claude(self) -> bool:
        return self.claude_allowed

    @property
    def digest(self) -> str:
        return canonical_hash(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskContract":
        raw = dict(raw)
        nested_strategy = raw.pop("strategy", raw.pop("routing", None))
        aliases = {
            "strategy_version": "routing_strategy",
            "work_type": "task_type",
            "task_kind": "task_type",
            "kind": "task_type",
            "task_category": "task_type",
            "complexity_level": "complexity",
            "risk": "complexity",
            "risk_level": "complexity",
            "allow_claude": "claude_allowed",
            "claude_enabled": "claude_allowed",
            "claude": "claude_allowed",
            "parallel": "parallelizable",
            "governance_tier": "verification_tier",
        }
        normalized = {
            aliases.get(key, key): value
            for key, value in raw.items()
        }
        if nested_strategy is not None:
            if not isinstance(nested_strategy, dict):
                raise ValueError("strategy must be an object")
            strategy = RoutingStrategy.from_dict(nested_strategy)
            normalized.setdefault("routing_strategy", strategy.version)
            normalized.setdefault("task_type", strategy.task_type)
            normalized.setdefault("complexity", strategy.complexity)
            normalized.setdefault("parallelizable", strategy.parallelizable)
            normalized.setdefault("claude_allowed", strategy.claude_allowed)
        tuple_fields = {
            "allowed_scope",
            "forbidden_scope",
            "dependencies",
            "acceptance_commands",
            "required_artifacts",
        }
        normalized = {
            key: tuple(value) if key in tuple_fields else value
            for key, value in normalized.items()
        }
        contract = cls(**normalized)
        contract.validate()
        return contract


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    task_id: str
    title: str
    executor: Literal["codex", "claude", "deterministic", "fixture"]
    model: str
    prompt: str = ""
    command: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    read_scopes: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    verifier: bool = False
    ordinal: int = 0

    def validate(self) -> None:
        if not self.node_id or not self.task_id:
            raise ValueError("node_id and task_id are required")
        if self.executor == "deterministic" and not self.command:
            raise ValueError("deterministic nodes require command")
        if self.executor in {"codex", "claude"} and not self.prompt.strip():
            raise ValueError("model nodes require prompt")
        if self.verifier and self.executor == "fixture":
            return

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NodeSpec":
        tuple_fields = {"command", "depends_on", "read_scopes", "write_scopes"}
        normalized = {
            key: tuple(value) if key in tuple_fields else value
            for key, value in raw.items()
        }
        node = cls(**normalized)
        node.validate()
        return node


@dataclass(frozen=True)
class NodeResult:
    status: Literal["succeeded", "failed", "blocked", "indeterminate"]
    summary: str
    artifacts: dict[str, str] = field(default_factory=dict)
    actual_model: str | None = None
    exit_code: int | None = None
    retryable: bool = False
    result_kind: Literal["worker", "verifier"] | None = None
    changed_paths: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    verdict: Literal["accepted", "needs_fix", "blocked"] | None = None
    governance_profile: str = CODE_AS_HARNESS_PROFILE
    verification_tier: VerificationTier = DEFAULT_VERIFICATION_TIER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NodeResult":
        tuple_fields = {"changed_paths", "checks", "evidence"}
        normalized = {
            key: tuple(value) if key in tuple_fields else value
            for key, value in raw.items()
        }
        return cls(**normalized)


def retry_model(model: str, attempt: int, *, verifier: bool = False) -> str:
    """Escalate bounded Codex repair attempts without changing provider families."""
    if verifier or attempt <= 1:
        return model
    lower = model.lower()
    if "codex-spark" in lower:
        if attempt == 2:
            return "gpt-5.6-luna"
        if attempt == 3:
            return "gpt-5.6-terra"
        return "gpt-5.6-sol"
    if "luna" in lower:
        return "gpt-5.6-terra" if attempt == 2 else "gpt-5.6-sol"
    if "terra" in lower:
        return "gpt-5.6-sol"
    return model


@dataclass(frozen=True)
class ClaudeDispatchDecision:
    action: ClaudeDispatchAction
    zone: ClaudeQuotaZone
    reason: str
    max_concurrency: int
    # ``max_concurrency`` remains the maximum simultaneous requests of the
    # selected model family.  v2 also exposes the shared weighted capacity so
    # callers do not mistake two Sonnet slots for an additional Opus slot.
    capacity_units: int = 0
    active_units: int = 0
    requested_units: int = 0
    available_units: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuotaSnapshot:
    observed_at: str
    auth_ok: bool
    auth_method: str
    five_hour_remaining: float | None
    weekly_all_remaining: float | None
    weekly_sonnet_remaining: float | None
    source: str
    weekly_fable_remaining: float | None = None
    five_hour_window_id: str | None = None
    weekly_window_id: str | None = None
    producer: str | None = None
    producer_schema_version: int | None = None
    claude_version: str | None = None

    def validate(self) -> None:
        if not self.source.strip():
            raise ValueError("quota source is required")
        percentages = {
            "five_hour_remaining": self.five_hour_remaining,
            "weekly_all_remaining": self.weekly_all_remaining,
            "weekly_sonnet_remaining": self.weekly_sonnet_remaining,
            "weekly_fable_remaining": self.weekly_fable_remaining,
        }
        for name, value in percentages.items():
            if value is not None and not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")

    def remaining_for(self, model: str) -> tuple[float | None, ...]:
        values: list[float | None] = [self.five_hour_remaining, self.weekly_all_remaining]
        lower = model.lower()
        if "sonnet" in lower:
            values.append(self.weekly_sonnet_remaining)
        # Claude /usage currently exposes an all-model weekly pool and a
        # Sonnet-only pool, but no Fable-only pool.  Fable is therefore gated
        # by the shared all-model pool unless a future producer supplies an
        # additional Fable-specific ceiling.
        if "fable" in lower and self.weekly_fable_remaining is not None:
            values.append(self.weekly_fable_remaining)
        return tuple(values)

    def age(self, *, current_time: datetime | None = None) -> timedelta | None:
        try:
            observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        current = current_time or datetime.now(UTC)
        return current.astimezone(UTC) - observed.astimezone(UTC)

    def is_fresh(
        self,
        *,
        max_age_seconds: int = DEFAULT_QUOTA_TTL_SECONDS,
        current_time: datetime | None = None,
    ) -> bool:
        age = self.age(current_time=current_time)
        return age is not None and timedelta(0) <= age <= timedelta(seconds=max_age_seconds)

    def quota_zone(self, model: str) -> tuple[ClaudeQuotaZone, float | None]:
        if not self.auth_ok or self.auth_method != "native-subscription":
            return "auth-unavailable", None
        values = self.remaining_for(model)
        if any(value is None for value in values):
            return "unknown", None
        minimum = min(value for value in values if value is not None)
        if minimum <= 25:
            return "protected", minimum
        if minimum < 30:
            return "red", minimum
        if minimum <= 40:
            return "yellow", minimum
        return "green", minimum

    def has_compatible_subscription_provenance(self) -> bool:
        """Whether this is the only producer formal Claude dispatch trusts."""

        # A local import keeps the model type usable by the collector while
        # retaining one canonical provenance contract for collection, routing,
        # and acceptance.
        from .claude_quota import (
            COMPATIBLE_SOURCE,
            PRODUCER,
            PRODUCER_SCHEMA_VERSION,
            SUPPORTED_USAGE_VERSION,
        )

        return (
            self.producer == PRODUCER
            and self.producer_schema_version == PRODUCER_SCHEMA_VERSION
            and self.source == COMPATIBLE_SOURCE
            and self.claude_version == SUPPORTED_USAGE_VERSION
        )

    def dispatch_decision(
        self,
        model: str,
        active_models: tuple[str, ...] = (),
        *,
        max_age_seconds: int | None = None,
        current_time: datetime | None = None,
        shared_capacity: bool = True,
    ) -> ClaudeDispatchDecision:
        requested_units = self._claude_capacity_units(model)

        def decision(
            action: ClaudeDispatchAction,
            zone: ClaudeQuotaZone,
            reason: str,
            *,
            capacity_units: int = 0,
            active_units: int = 0,
        ) -> ClaudeDispatchDecision:
            available_units = max(capacity_units - active_units, 0)
            max_concurrency = (
                capacity_units // requested_units if requested_units else 0
            )
            return ClaudeDispatchDecision(
                action,
                zone,
                reason,
                max_concurrency,
                capacity_units=capacity_units,
                active_units=active_units,
                requested_units=requested_units,
                available_units=available_units,
            )

        if max_age_seconds is not None and not self.is_fresh(
            max_age_seconds=max_age_seconds,
            current_time=current_time,
        ):
            age = self.age(current_time=current_time)
            age_text = "invalid" if age is None else f"{max(age.total_seconds(), 0):.0f}s old"
            return decision(
                "codex",
                "unknown",
                f"Claude quota snapshot is stale ({age_text}; TTL {max_age_seconds}s)",
            )
        if not self.auth_ok or self.auth_method != "native-subscription":
            return decision(
                "codex",
                "auth-unavailable",
                "Claude native-subscription authentication is unavailable",
            )
        if not self.has_compatible_subscription_provenance():
            return decision(
                "codex",
                "unknown",
                "Claude quota provenance is not the compatible native subscription producer",
            )
        zone, minimum = self.quota_zone(model)
        if zone == "unknown":
            return decision("codex", zone, "Claude quota is unknown")
        assert minimum is not None
        if zone == "protected":
            return decision(
                "codex",
                zone,
                f"Claude quota protection active at {minimum:.1f}% remaining",
            )
        if zone == "red":
            return decision(
                "codex",
                zone,
                f"Claude quota red zone at {minimum:.1f}% remaining; new Claude turns are disabled",
            )

        lower = model.lower()
        if zone == "yellow" and "sonnet" not in lower:
            return decision(
                "codex",
                zone,
                f"Claude quota yellow zone at {minimum:.1f}% remaining; only Sonnet may start",
                capacity_units=1,
            )
        if not shared_capacity:
            sonnet = "sonnet" in lower
            capacity_units = 1 if zone == "yellow" else 2 if sonnet else 1
            requested_units = 1
            active_units = sum(
                ("sonnet" in active_model.lower()) == sonnet
                for active_model in active_models
            )
        else:
            capacity_units = 1 if zone == "yellow" else 2
            active_units = sum(
                self._claude_capacity_units(active_model) for active_model in active_models
            )
        if requested_units > capacity_units - active_units:
            return decision(
                "defer",
                zone,
                (
                    f"Claude {zone} shared concurrency capacity reached: "
                    f"{active_units}/{capacity_units} units active; "
                    f"{requested_units} units required"
                ),
                capacity_units=capacity_units,
                active_units=active_units,
            )
        return decision(
            "claude",
            zone,
            (
                f"Claude {zone} shared capacity permits {requested_units}-unit "
                f"request: {active_units}/{capacity_units} units active"
            ),
            capacity_units=capacity_units,
            active_units=active_units,
        )

    @staticmethod
    def _claude_capacity_units(model: str) -> int:
        """Return v2's shared capacity cost, conservatively for unknown models."""

        lower = model.lower()
        return 1 if "sonnet" in lower else 2

    def permits(self, model: str, stop_line: float = 25.0) -> tuple[bool, str]:
        if stop_line != 25.0:
            raise ValueError("Claude quota stop line is fixed at 25%")
        decision = self.dispatch_decision(model)
        return decision.action == "claude", decision.reason

    def policy_summary(
        self,
        *,
        max_age_seconds: int | None = None,
        current_time: datetime | None = None,
    ) -> dict[str, Any]:
        models = {
            model: self.dispatch_decision(
                model,
                max_age_seconds=max_age_seconds,
                current_time=current_time,
            ).to_dict()
            for model in ("opus", "sonnet", "fable")
        }
        zones = {model: decision["zone"] for model, decision in models.items()}
        return {
            "zone": next(iter(set(zones.values()))) if len(set(zones.values())) == 1 else "mixed",
            "zones": zones,
            "thresholds": {
                "hard_reserve": 20,
                "stop_line": 25,
                "red_upper_exclusive": 30,
                "yellow_upper_inclusive": 40,
            },
            "models": models,
            "snapshot_fresh": (
                self.is_fresh(
                    max_age_seconds=max_age_seconds,
                    current_time=current_time,
                )
                if max_age_seconds is not None
                else None
            ),
            "snapshot_ttl_seconds": max_age_seconds,
        }
