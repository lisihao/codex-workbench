from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal


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


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


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
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= self.retry_limit <= 3:
            raise ValueError("retry_limit must be between 0 and 3")

    @property
    def digest(self) -> str:
        return canonical_hash(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskContract":
        tuple_fields = {
            "allowed_scope",
            "forbidden_scope",
            "dependencies",
            "acceptance_commands",
            "required_artifacts",
        }
        normalized = {
            key: tuple(value) if key in tuple_fields else value
            for key, value in raw.items()
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NodeResult":
        return cls(**raw)


@dataclass(frozen=True)
class ClaudeDispatchDecision:
    action: ClaudeDispatchAction
    zone: ClaudeQuotaZone
    reason: str
    max_concurrency: int

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
        if "fable" in lower:
            values.append(self.weekly_fable_remaining)
        return tuple(values)

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

    def dispatch_decision(
        self,
        model: str,
        active_models: tuple[str, ...] = (),
    ) -> ClaudeDispatchDecision:
        zone, minimum = self.quota_zone(model)
        if zone == "auth-unavailable":
            return ClaudeDispatchDecision(
                "codex",
                zone,
                "Claude native-subscription authentication is unavailable",
                0,
            )
        if zone == "unknown":
            return ClaudeDispatchDecision("codex", zone, "Claude quota is unknown", 0)
        assert minimum is not None
        if zone == "protected":
            return ClaudeDispatchDecision(
                "codex",
                zone,
                f"Claude quota protection active at {minimum:.1f}% remaining",
                0,
            )
        if zone == "red":
            return ClaudeDispatchDecision(
                "codex",
                zone,
                f"Claude quota red zone at {minimum:.1f}% remaining; new Claude turns are disabled",
                0,
            )

        lower = model.lower()
        if zone == "yellow" and "sonnet" not in lower:
            return ClaudeDispatchDecision(
                "codex",
                zone,
                f"Claude quota yellow zone at {minimum:.1f}% remaining; only Sonnet may start",
                0,
            )
        family = "sonnet" if "sonnet" in lower else "high"
        cap = 1 if zone == "yellow" else 2 if family == "sonnet" else 1
        active = sum(
            "sonnet" in active_model.lower()
            if family == "sonnet"
            else "sonnet" not in active_model.lower()
            for active_model in active_models
        )
        if active >= cap:
            return ClaudeDispatchDecision(
                "defer",
                zone,
                f"Claude {zone} zone concurrency cap reached for {family}: {active}/{cap}",
                cap,
            )
        return ClaudeDispatchDecision(
            "claude",
            zone,
            f"Claude {zone} zone permits {family}: {active}/{cap} active",
            cap,
        )

    def permits(self, model: str, stop_line: float = 25.0) -> tuple[bool, str]:
        if stop_line != 25.0:
            raise ValueError("Claude quota stop line is fixed at 25%")
        decision = self.dispatch_decision(model)
        return decision.action == "claude", decision.reason

    def policy_summary(self) -> dict[str, Any]:
        models = {
            model: self.dispatch_decision(model).to_dict()
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
        }
