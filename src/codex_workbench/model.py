from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
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
CODEX_ASTRA_MODEL = "gpt-6-astra"
CODEX_CONTROL_PLANE_MODELS = frozenset({CODEX_SOL_MODEL, CODEX_ASTRA_MODEL})
CODEX_LONG_CONTEXT_WINDOW = 500_000
CODEX_LONG_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT = 450_000
CODEX_LONG_CONTEXT_MODELS = frozenset({
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    CODEX_ASTRA_MODEL,
})


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
RoutingProfile = Literal[
    "spark_worker",
    "luna_worker",
    "terra_worker",
    "sol_control_plane",
    "astra_control_plane",
]
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ExecutionLane = Literal["spark", "general", "control", "deterministic", "fixture"]

EXECUTION_LANES = frozenset({
    "spark",
    "general",
    "control",
    "deterministic",
    "fixture",
})

# These are deliberately derived from the selected model, rather than trusted
# from planner prose.  The values are persisted on normalized NodeSpec rows
# and are also used by the Codex executor to emit an explicit CLI override.
CODEX_MODEL_PROFILES: dict[str, RoutingProfile] = {
    "gpt-5.3-codex-spark": "spark_worker",
    "gpt-5.6-luna": "luna_worker",
    "gpt-5.6-terra": "terra_worker",
    "gpt-5.6-sol": "sol_control_plane",
    CODEX_ASTRA_MODEL: "astra_control_plane",
}
CODEX_MODEL_REASONING_EFFORTS: dict[str, ReasoningEffort] = {
    "gpt-5.3-codex-spark": "xhigh",
    "gpt-5.6-luna": "max",
    "gpt-5.6-terra": "max",
    "gpt-5.6-sol": "max",
    CODEX_ASTRA_MODEL: "max",
}


def is_codex_control_plane_model(model: object) -> bool:
    """Return whether ``model`` is an exact admitted Codex control-plane ID."""

    return str(model).strip().lower() in CODEX_CONTROL_PLANE_MODELS


def codex_model_profile(model: object) -> str | None:
    """Return the canonical execution profile for a known Codex model."""

    value = str(model).strip().lower()
    return CODEX_MODEL_PROFILES.get(value)


def codex_model_reasoning_effort(model: object) -> str | None:
    """Return the explicit reasoning effort required for a known Codex model."""

    value = str(model).strip().lower()
    return CODEX_MODEL_REASONING_EFFORTS.get(value)


def codex_model_long_context_overrides(model: object) -> tuple[str, ...]:
    """Return the configured long-context policy for exact compatible Codex IDs."""

    value = str(model).strip().lower()
    if value not in CODEX_LONG_CONTEXT_MODELS:
        return ()
    return (
        f"model_context_window={CODEX_LONG_CONTEXT_WINDOW}",
        "model_auto_compact_token_limit="
        f"{CODEX_LONG_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT}",
    )


def derive_execution_lane(
    executor: object,
    model: object,
    *,
    verifier: bool = False,
    role: str | None = None,
) -> ExecutionLane:
    """Derive the durable execution lane from trusted execution metadata.

    Planner text and planner-supplied lane labels are never inputs to this
    function.  ``role`` is supplied only by the deterministic routing
    decision; direct/legacy ``NodeSpec`` values use ``verifier`` and the
    exact Codex model to preserve the same fail-closed result.
    """

    normalized_executor = str(executor).strip().lower()
    normalized_model = str(model).strip().lower()
    if normalized_executor == "fixture":
        return "fixture"
    if normalized_executor == "deterministic":
        return "deterministic"
    if verifier or role in {"planner", "verifier", "control"}:
        return "control"
    if normalized_executor == "codex":
        if codex_model_profile(normalized_model) == "spark_worker":
            return "spark"
        if is_codex_control_plane_model(normalized_model):
            return "control"
    return "general"


def derive_quota_pool_id(
    executor: object,
    model: object,
    *,
    verifier: bool = False,
    role: str | None = None,
) -> str:
    """Return the stable pool identifier paired with ``derive_execution_lane``."""

    lane = derive_execution_lane(
        executor,
        model,
        verifier=verifier,
        role=role,
    )
    if lane == "spark":
        return "codex-spark"
    if lane == "control":
        return "codex-control"
    if lane == "deterministic":
        return "deterministic"
    if lane == "fixture":
        return "fixture"
    if str(executor).strip().lower() in {"claude", "anthropic"}:
        return "claude-shared"
    return "codex-general"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


# Capability snapshots are content-addressed records.  Accept the same raw
# hexadecimal form returned by ``canonical_hash`` and the explicit form used
# by artifact/evidence references, while rejecting ambiguous or unsafe values
# before they become part of a durable contract.
_CAPABILITY_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _validate_capability_snapshot_binding(
    snapshot_id: str | None,
    digest: str | None,
    owner: str,
) -> None:
    if (snapshot_id is None) != (digest is None):
        raise ValueError(
            f"{owner} capability_snapshot_id and capability_digest must be supplied together"
        )
    if snapshot_id is None:
        return
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError(f"{owner} capability_snapshot_id must be non-empty")
    if any(character.isspace() for character in snapshot_id):
        raise ValueError(f"{owner} capability_snapshot_id must not contain whitespace")
    if not isinstance(digest, str) or not _CAPABILITY_DIGEST_RE.fullmatch(digest):
        raise ValueError(
            f"{owner} capability_digest must be a raw SHA-256 hex digest or sha256:<64 hex>"
        )


def _validate_performance_snapshot_binding(
    snapshot_id: str | None,
    digest: str | None,
    owner: str,
) -> None:
    """Validate the optional performance-calibration pin as an atomic pair.

    Performance generations are content addressed by the registry.  The model
    layer intentionally does not require a particular generation prefix so
    imported/legacy contracts can retain their opaque identifiers, but it
    always requires the digest when an identifier is present.
    """

    if (snapshot_id is None) != (digest is None):
        raise ValueError(
            f"{owner} performance_snapshot_id and performance_digest must be supplied together"
        )
    if snapshot_id is None:
        return
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError(f"{owner} performance_snapshot_id must be non-empty")
    if any(character.isspace() for character in snapshot_id):
        raise ValueError(f"{owner} performance_snapshot_id must not contain whitespace")
    if not isinstance(digest, str) or not _CAPABILITY_DIGEST_RE.fullmatch(digest):
        raise ValueError(
            f"{owner} performance_digest must be a raw SHA-256 hex digest or sha256:<64 hex>"
        )


def _validate_performance_metadata(
    policy: str | None,
    status: str | None,
    owner: str,
) -> None:
    for name, value in (("performance_policy", policy), ("performance_status", status)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{owner} {name} must be a non-empty string when supplied")


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
    # Optional content-addressed capability catalog binding.  Both values are
    # persisted in the contract so routing/evidence can remain reproducible;
    # absent values preserve pre-capability-registry contracts.
    capability_snapshot_id: str | None = None
    capability_digest: str | None = None
    # Optional content-addressed performance-calibration binding.  A contract
    # may be created before a calibration generation exists; ``None`` keeps
    # legacy contracts valid and makes the binding fail closed when supplied
    # incompletely.
    performance_snapshot_id: str | None = None
    performance_digest: str | None = None
    performance_policy: str | None = None
    performance_status: str | None = None

    def __post_init__(self) -> None:
        # Sol remains the default control-plane model.  Astra is an explicit
        # peer selection; all other values retain the historic Sol fallback.
        # ``fixture`` remains available for offline demonstrations and tests.
        if self.planner_model != "fixture":
            object.__setattr__(
                self,
                "planner_model",
                str(self.planner_model).strip().lower()
                if is_codex_control_plane_model(self.planner_model)
                else CODEX_SOL_MODEL,
            )
        if self.verifier_model != "fixture":
            object.__setattr__(
                self,
                "verifier_model",
                str(self.verifier_model).strip().lower()
                if is_codex_control_plane_model(self.verifier_model)
                else CODEX_SOL_MODEL,
            )
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
        _validate_capability_snapshot_binding(
            self.capability_snapshot_id,
            self.capability_digest,
            "task contract",
        )
        _validate_performance_snapshot_binding(
            self.performance_snapshot_id,
            self.performance_digest,
            "task contract",
        )
        _validate_performance_metadata(
            self.performance_policy,
            self.performance_status,
            "task contract",
        )
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
    # Populated only by the normalized planner.  This durable metadata keeps
    # model/user prompt text from becoming an execution-control channel.
    archify: dict[str, Any] | None = None
    # Optional on input for compatibility with pre-v2 persisted plans; the
    # planner fills these fields from the task contract before persistence.
    routing_strategy: str | None = None
    task_type: RoutingTaskType | None = None
    complexity: RoutingComplexity | None = None
    parallelizable: bool | None = None
    claude_allowed: bool | None = None
    # Effective Codex invocation metadata.  These values are derived from the
    # selected model and make Luna's max-effort/profile semantics inspectable.
    model_profile: str | None = None
    model_reasoning_effort: str | None = None
    # Optional pinned capability metadata.  New planners may populate these
    # fields; legacy plans omit them and retain their existing routing.
    capability_snapshot_id: str | None = None
    capability_digest: str | None = None
    # Performance calibration is pinned alongside the capability catalog.  It
    # is derived by the normalizer from the task contract and final routing
    # receipt; model-generated plan values cannot widen or replace it.
    performance_snapshot_id: str | None = None
    performance_digest: str | None = None
    performance_policy: str | None = None
    performance_status: str | None = None
    performance_quality_source: str | None = None
    performance_lower_bound_95: float | None = None
    performance_runtime_sample_count: int = 0
    performance_first_pass_rate: float | None = None
    performance_rework_rate: float | None = None
    performance_latency_ms: float | None = None
    model_capability_id: str | None = None
    agent_capability_id: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    routing_policy_version: str | None = None
    # Durable scheduler metadata.  These fields are derived from the final
    # executor/model/role by the planner and are never a planner control
    # channel.  ``None`` remains accepted at the constructor boundary for
    # legacy persisted nodes; __post_init__ fills both values deterministically.
    execution_lane: ExecutionLane | None = None
    quota_pool_id: str | None = None

    def __post_init__(self) -> None:
        if self.execution_lane is None and self.quota_pool_id is None:
            object.__setattr__(
                self,
                "execution_lane",
                derive_execution_lane(
                    self.executor,
                    self.model,
                    verifier=self.verifier,
                ),
            )
            object.__setattr__(
                self,
                "quota_pool_id",
                derive_quota_pool_id(
                    self.executor,
                    self.model,
                    verifier=self.verifier,
                ),
            )
        if self.executor != "codex":
            return
        profile = codex_model_profile(self.model)
        effort = codex_model_reasoning_effort(self.model)
        if profile is not None and self.model_profile is None:
            object.__setattr__(self, "model_profile", profile)
        if effort is not None and self.model_reasoning_effort is None:
            object.__setattr__(self, "model_reasoning_effort", effort)

    def validate(self) -> None:
        if not self.node_id or not self.task_id:
            raise ValueError("node_id and task_id are required")
        if self.executor == "deterministic" and not self.command:
            raise ValueError("deterministic nodes require command")
        if self.executor in {"codex", "claude"} and not self.prompt.strip():
            raise ValueError("model nodes require prompt")
        if self.routing_strategy is not None:
            RoutingStrategy(
                version=self.routing_strategy,
                task_type=self.task_type or "implementation",
                complexity=self.complexity or "standard",
                parallelizable=True if self.parallelizable is None else self.parallelizable,
                claude_allowed=True if self.claude_allowed is None else self.claude_allowed,
            ).validate()
        if self.parallelizable is not None and not isinstance(self.parallelizable, bool):
            raise ValueError("node parallelizable must be a boolean")
        if self.claude_allowed is not None and not isinstance(self.claude_allowed, bool):
            raise ValueError("node claude_allowed must be a boolean")
        _validate_capability_snapshot_binding(
            self.capability_snapshot_id,
            self.capability_digest,
            "node",
        )
        _validate_performance_snapshot_binding(
            self.performance_snapshot_id,
            self.performance_digest,
            "node",
        )
        _validate_performance_metadata(
            self.performance_policy,
            self.performance_status,
            "node",
        )
        if self.performance_quality_source is not None and (
            not isinstance(self.performance_quality_source, str)
            or not self.performance_quality_source.strip()
        ):
            raise ValueError(
                "node performance_quality_source must be a non-empty string when supplied"
            )
        if (
            not isinstance(self.performance_runtime_sample_count, int)
            or isinstance(self.performance_runtime_sample_count, bool)
            or self.performance_runtime_sample_count < 0
        ):
            raise ValueError("node performance_runtime_sample_count must be non-negative")
        for field_name in (
            "performance_lower_bound_95",
            "performance_first_pass_rate",
            "performance_rework_rate",
            "performance_latency_ms",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"node {field_name} must be a finite non-negative number")
        if self.performance_lower_bound_95 is not None and self.performance_lower_bound_95 > 1:
            raise ValueError("node performance_lower_bound_95 must be between 0 and 1")
        for field_name in ("performance_first_pass_rate", "performance_rework_rate"):
            value = getattr(self, field_name)
            if value is not None and value > 1:
                raise ValueError(f"node {field_name} must be between 0 and 1")
        if (self.execution_lane is None) != (self.quota_pool_id is None):
            raise ValueError(
                "node execution_lane and quota_pool_id must be supplied together"
            )
        if self.execution_lane is not None:
            if self.execution_lane not in EXECUTION_LANES:
                raise ValueError(
                    f"node execution_lane {self.execution_lane!r} is unsupported"
                )
            expected_lane = derive_execution_lane(
                self.executor,
                self.model,
                verifier=self.verifier,
            )
            if self.execution_lane != expected_lane:
                raise ValueError(
                    f"node execution_lane {self.execution_lane!r} does not match "
                    f"executor/model/role (expected {expected_lane!r})"
                )
            expected_pool = derive_quota_pool_id(
                self.executor,
                self.model,
                verifier=self.verifier,
            )
            if self.quota_pool_id != expected_pool:
                raise ValueError(
                    f"node quota_pool_id {self.quota_pool_id!r} does not match "
                    f"execution lane (expected {expected_pool!r})"
                )
        for field_name in (
            "model_capability_id",
            "agent_capability_id",
            "agent_name",
            "agent_version",
            "routing_policy_version",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(
                    f"node {field_name} must be a non-empty string when supplied"
                )
        expected_profile = codex_model_profile(self.model)
        if expected_profile is not None and self.model_profile not in {None, expected_profile}:
            raise ValueError(
                f"node model_profile {self.model_profile!r} does not match {self.model!r}"
            )
        expected_effort = codex_model_reasoning_effort(self.model)
        if expected_effort is not None and self.model_reasoning_effort not in {
            None,
            expected_effort,
        }:
            raise ValueError(
                f"node model_reasoning_effort {self.model_reasoning_effort!r} does not match {self.model!r}"
            )
        if self.verifier and self.executor == "fixture":
            return

    @property
    def routing(self) -> RoutingStrategy | None:
        """Return node routing metadata when it was explicitly normalized."""

        if self.routing_strategy is None:
            return None
        return RoutingStrategy(
            version=self.routing_strategy,
            task_type=self.task_type or "implementation",
            complexity=self.complexity or "standard",
            parallelizable=True if self.parallelizable is None else self.parallelizable,
            claude_allowed=True if self.claude_allowed is None else self.claude_allowed,
        ).normalized()

    @property
    def profile(self) -> str | None:
        return self.model_profile

    @property
    def reasoning_effort(self) -> str | None:
        return self.model_reasoning_effort

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
    # Execution provenance is optional for legacy receipts.  Executors can
    # populate it when a capability catalog is active without changing the
    # existing result contract or SQLite schema.
    requested_model: str | None = None
    provider: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    capability_snapshot_id: str | None = None
    model_capability_id: str | None = None
    agent_capability_id: str | None = None

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


def retry_model(
    model: str,
    attempt: int,
    *,
    verifier: bool = False,
    routing_policy_version: str | None = None,
) -> str:
    """Escalate legacy repairs while keeping pinned v3 capabilities immutable.

    A routing-v3 node has already passed the capability, quota, role, and
    quality gates against one immutable catalog snapshot.  Silently replacing
    its model at claim time would bypass those gates and could promote an
    ordinary worker to the Sol control plane.  Such nodes therefore retry the
    same selected capability; a later planner repair may create a newly routed
    node when a different capability is genuinely required.
    """
    if verifier or attempt <= 1:
        return model
    if routing_policy_version == "model-routing-v3":
        return model
    lower = model.strip().lower()
    if lower == "gpt-5.3-codex-spark":
        if attempt == 2:
            return "gpt-5.6-luna"
        if attempt == 3:
            return "gpt-5.6-terra"
        return "gpt-5.6-sol"
    if lower == "gpt-5.6-luna":
        return "gpt-5.6-terra" if attempt == 2 else "gpt-5.6-sol"
    if lower == "gpt-5.6-terra":
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
        # Claude /usage exposes the all-model pool plus one model-specific
        # pool depending on the subscription. Missing model-specific pools
        # fall back to the shared all-model ceiling.
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
