"""Pure policy for bounded recovery of an Authority-blocked node.

This module only classifies supplied evidence and returns a proposed next
action.  It does not inspect the store, run a command, acquire a lease, or
contact a model or provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Mapping


Action = Literal[
    "observe_readiness",
    "materialize_dependencies",
    "narrow_validation",
    "source_only_recovery",
    "request_repair",
    "resume_node",
    "repair_source",
]
Category = Literal[
    "capacity",
    "auth",
    "network",
    "environment",
    "tooling_bug",
    "validation_failure",
    "unknown_effects",
    "user_pause",
    "dependency",
    "unknown",
]
RecoveryState = Literal["waiting", "ready", "needs_action", "resolved", "suspended"]

ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "observe_readiness",
        "materialize_dependencies",
        "narrow_validation",
        "source_only_recovery",
        "request_repair",
        "resume_node",
        "repair_source",
    }
)
VALIDATION_PROFILES: Final[frozenset[str]] = frozenset(
    {
        "dsh-b-ipc-v1",
        "dsh-b-pairing-check-v1",
        "dsh-b-pairing-write-v1",
    }
)
CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "capacity",
        "auth",
        "network",
        "environment",
        "tooling_bug",
        "validation_failure",
        "unknown_effects",
        "user_pause",
        "dependency",
        "unknown",
    }
)

_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "enabled",
        "allowed_actions",
        "validation_profiles",
        "max_action_attempts",
        "time_budget_seconds",
        "backoff_seconds",
        "max_backoff_seconds",
        "repair_repository",
        "repair_allowed_scopes",
    }
)

_READINESS_DEPENDENCY_CODES: Final[frozenset[str]] = frozenset(
    {
        "missing-dependency",
        "dependency-unreadable",
        "dependency-kind-mismatch",
        "invalid-dependency",
        "source-unresolved",
        "source-outside-worktree",
        "pnpm:linker",
    }
)
_READINESS_ENVIRONMENT_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid-worktree",
        "missing-tool",
        "toolchain-mismatch",
        "source-probe-invalid",
        "probe-limit-exceeded",
        "probe-budget-exhausted",
        "probe-timeout",
        "probe-error",
        "probe-failed",
    }
)
_READINESS_DEPENDENCY_CHECK_IDS: Final[frozenset[str]] = frozenset({"pnpm:linker"})
_NETWORK_CODES: Final[frozenset[str]] = frozenset(
    {
        "network",
        "network-unavailable",
        "transport",
        "transport-failed",
        "transport-timeout",
        "authority-unavailable",
        "ssh-unreachable",
    }
)
_AUTH_CODES: Final[frozenset[str]] = frozenset(
    {"auth", "authentication-required", "authorization-required", "permission-denied"}
)
_VALIDATION_CODES: Final[frozenset[str]] = frozenset(
    {"validation-failure", "validation_failed", "check-failed", "assertion-failed"}
)
_TOOLING_CODES: Final[frozenset[str]] = frozenset(
    {"tooling-bug", "tooling_bug", "harness-bug", "runner-bug"}
)


def _require_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains forbidden control characters")
    return value


def _read_tuple(raw: object, name: str) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    values = tuple(_require_text(item, f"{name} item") for item in raw)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class RecoveryPolicy:
    """Explicit task-scoped recovery limits and authorized action set."""

    enabled: bool = False
    allowed_actions: tuple[str, ...] = ("observe_readiness",)
    validation_profiles: tuple[str, ...] = ()
    max_action_attempts: int = 3
    time_budget_seconds: int = 900
    backoff_seconds: int = 30
    max_backoff_seconds: int = 300
    repair_repository: str | None = None
    repair_allowed_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        actions = _read_tuple(self.allowed_actions, "allowed_actions")
        profiles = _read_tuple(self.validation_profiles, "validation_profiles")
        scopes = _read_tuple(self.repair_allowed_scopes, "repair_allowed_scopes")
        unsupported_actions = set(actions) - ALLOWED_ACTIONS
        if unsupported_actions:
            raise ValueError(f"unsupported allowed action(s): {sorted(unsupported_actions)!r}")
        unsupported_profiles = set(profiles) - VALIDATION_PROFILES
        if unsupported_profiles:
            raise ValueError(f"unsupported validation profile(s): {sorted(unsupported_profiles)!r}")
        _bounded_int(self.max_action_attempts, "max_action_attempts", 1, 3)
        _bounded_int(self.time_budget_seconds, "time_budget_seconds", 1, 3_600)
        _bounded_int(self.backoff_seconds, "backoff_seconds", 1, 300)
        _bounded_int(self.max_backoff_seconds, "max_backoff_seconds", 1, 3_600)
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least backoff_seconds")
        repository = self.repair_repository
        if repository is not None:
            repository = _require_text(repository, "repair_repository")
            if not repository.startswith("/"):
                raise ValueError("repair_repository must be an absolute path")
        if "request_repair" in actions and (repository is None or not scopes):
            raise ValueError("request_repair requires repair_repository and repair_allowed_scopes")
        object.__setattr__(self, "allowed_actions", actions)
        object.__setattr__(self, "validation_profiles", profiles)
        object.__setattr__(self, "repair_allowed_scopes", scopes)
        object.__setattr__(self, "repair_repository", repository)

    @classmethod
    def from_dict(cls, raw: object) -> "RecoveryPolicy":
        """Parse one strict JSON/config mapping, applying only field defaults."""

        if not isinstance(raw, Mapping):
            raise ValueError("recovery policy must be an object")
        fields = {str(key) for key in raw}
        unexpected = fields - _POLICY_FIELDS
        if unexpected:
            raise ValueError(f"recovery policy has unsupported field(s): {sorted(unexpected)!r}")
        return cls(
            enabled=raw.get("enabled", False),
            allowed_actions=raw.get("allowed_actions", ("observe_readiness",)),
            validation_profiles=raw.get("validation_profiles", ()),
            max_action_attempts=raw.get("max_action_attempts", 3),
            time_budget_seconds=raw.get("time_budget_seconds", 900),
            backoff_seconds=raw.get("backoff_seconds", 30),
            max_backoff_seconds=raw.get("max_backoff_seconds", 300),
            repair_repository=raw.get("repair_repository"),
            repair_allowed_scopes=raw.get("repair_allowed_scopes", ()),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the canonical JSON-safe policy mapping."""

        return {
            "enabled": self.enabled,
            "allowed_actions": list(self.allowed_actions),
            "validation_profiles": list(self.validation_profiles),
            "max_action_attempts": self.max_action_attempts,
            "time_budget_seconds": self.time_budget_seconds,
            "backoff_seconds": self.backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "repair_repository": self.repair_repository,
            "repair_allowed_scopes": list(self.repair_allowed_scopes),
        }


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _source_kind(evidence: Mapping[str, object]) -> str | None:
    source = evidence.get("source")
    if isinstance(source, Mapping):
        source = source.get("kind")
    return _text(source)


def _explicit_origin_category(origin: str | None) -> Category | None:
    return {
        "capacity": "capacity",
        "quota": "capacity",
        "auth": "auth",
        "authentication": "auth",
        "network": "network",
        "transport": "network",
        "environment": "environment",
        "dependency": "dependency",
        "tooling_bug": "tooling_bug",
        "tooling-bug": "tooling_bug",
        "validation_failure": "validation_failure",
        "verification": "validation_failure",
        "unknown_effects": "unknown_effects",
        "user_pause": "user_pause",
        "pause": "user_pause",
        "cancel": "user_pause",
    }.get(origin)  # type: ignore[return-value]


def classify_failure(evidence: Mapping[str, object]) -> str:
    """Classify only explicit Authority evidence; never inspect a summary string."""

    if not isinstance(evidence, Mapping):
        return "unknown"
    source = _source_kind(evidence)
    origin = _text(evidence.get("origin"))
    code = _text(evidence.get("code"))
    check_id = _text(evidence.get("check_id"))
    if source is None or source == "unknown":
        return "unknown"

    # Readiness owns these exact codes/check IDs.  They remain dependency
    # failures even when the broader readiness report calls their origin
    # environmental.
    if source == "authority_readiness":
        if code in _READINESS_DEPENDENCY_CODES or check_id in _READINESS_DEPENDENCY_CHECK_IDS:
            return "dependency"
        if code in _READINESS_ENVIRONMENT_CODES:
            return "environment"

    explicit = _explicit_origin_category(origin)
    if explicit is not None:
        if source == "direct_attribution":
            return explicit
        if source in {"authority_readiness", "authority_validation"}:
            return explicit
        return "unknown"

    if source == "authority_readiness":
        return "unknown"

    if source == "authority_validation":
        if code in _NETWORK_CODES:
            return "network"
        if code in _AUTH_CODES:
            return "auth"
        if code in _VALIDATION_CODES:
            return "validation_failure"
        if code in _TOOLING_CODES:
            return "tooling_bug"
        if code in _READINESS_DEPENDENCY_CODES:
            return "dependency"
        return "unknown"

    if source == "direct_attribution":
        # A direct attribution with no recognized origin is still not a code
        # failure.  In particular, model and unknown origins remain unknown.
        return "unknown"
    return "unknown"


def _parse_now(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _wakeup(now: datetime | None, seconds: int | float) -> str | None:
    if now is None:
        return None
    return (now + timedelta(seconds=seconds)).isoformat()


def _result(
    *,
    category: str,
    action: str | None,
    state: RecoveryState,
    reason_kind: str,
    owner: str,
    requires_authorization: bool,
    next_wakeup_at: str | None,
) -> dict[str, object]:
    return {
        "category": category,
        "action": action,
        "state": state,
        "reason_kind": reason_kind,
        "owner": owner,
        "requires_authorization": requires_authorization,
        "next_wakeup_at": next_wakeup_at,
    }


def _ready_action(
    policy: RecoveryPolicy,
    observation: Mapping[str, object],
    *,
    category: str,
    action: Action,
    reason_kind: str,
    owner: str = "authority",
) -> dict[str, object]:
    attempts = observation.get("action_attempts", 0)
    attempts = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    now = _parse_now(observation.get("now"))
    if observation.get("last_action") == action and attempts > 0:
        delay = min(policy.backoff_seconds * (2 ** (attempts - 1)), policy.max_backoff_seconds)
        retry_due = _parse_now(observation.get("retry_due_at"))
        last_action_at = _parse_now(observation.get("last_action_at"))
        due = retry_due
        if due is None and last_action_at is not None:
            due = last_action_at + timedelta(seconds=delay)
        # A DAO-supplied deadline is stable across observations.  If the DAO
        # has not supplied one yet, do not manufacture a new deadline on every
        # heartbeat; the action remains eligible until the durable deadline is
        # available.
        if due is not None:
            try:
                before_due = now is None or now < due
            except TypeError:
                before_due = True
            if before_due:
                return _result(
                    category=category,
                    action=None,
                    state="waiting",
                    reason_kind="backoff",
                    owner=owner,
                    requires_authorization=False,
                    next_wakeup_at=due.isoformat(),
                )
    return _result(
        category=category,
        action=action,
        state="ready",
        reason_kind=reason_kind,
        owner=owner,
        requires_authorization=False,
        next_wakeup_at=None,
    )


def plan_recovery(policy: RecoveryPolicy, observation: dict[str, object]) -> dict[str, object]:
    """Return one bounded recovery proposal from an Authority observation."""

    if not isinstance(policy, RecoveryPolicy):
        raise TypeError("policy must be RecoveryPolicy")
    if not isinstance(observation, Mapping):
        raise TypeError("observation must be a mapping")
    category = _text(observation.get("category"))
    if category not in CATEGORIES:
        category = "unknown"
    task_state = (_text(observation.get("task_state")) or "").lower()
    node_state = (_text(observation.get("node_state")) or "").lower()
    now = _parse_now(observation.get("now"))
    if not policy.enabled:
        return _result(
            category=category,
            action=None,
            state="suspended",
            reason_kind="policy_disabled",
            owner="authority",
            requires_authorization=False,
            next_wakeup_at=None,
        )
    if task_state in {"paused", "pause", "cancelled", "canceled"} or node_state in {"paused", "cancelled", "canceled"}:
        return _result(
            category=category,
            action=None,
            state="suspended",
            reason_kind="user_pause",
            owner="user",
            requires_authorization=False,
            next_wakeup_at=None,
        )
    if observation.get("approval_denied") is True:
        return _result(
            category=category,
            action=None,
            state="suspended",
            reason_kind="approval_denied",
            owner="user",
            requires_authorization=True,
            next_wakeup_at=None,
        )
    if node_state == "indeterminate":
        return _result(
            category=category,
            action=None,
            state="suspended",
            reason_kind="indeterminate",
            owner="user",
            requires_authorization=True,
            next_wakeup_at=None,
        )
    if task_state == "accepted" or node_state in {"accepted", "succeeded", "completed"}:
        return _result(
            category=category,
            action=None,
            state="resolved",
            reason_kind="accepted",
            owner="authority",
            requires_authorization=False,
            next_wakeup_at=None,
        )
    if category in {"auth", "unknown_effects"} or task_state in {"indeterminate", "unknown_effects"}:
        return _result(
            category=category,
            action=None,
            state="needs_action",
            reason_kind="authorization_required" if category == "auth" else "unknown_effects",
            owner="user",
            requires_authorization=True,
            next_wakeup_at=None,
        )

    attempts = observation.get("action_attempts", 0)
    elapsed = observation.get("elapsed_seconds", 0)
    attempts_number = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    elapsed_number = elapsed if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else 0
    if attempts_number >= policy.max_action_attempts or elapsed_number >= policy.time_budget_seconds:
        return _result(
            category=category,
            action=None,
            state="needs_action",
            reason_kind="budget_exhausted",
            owner="user",
            requires_authorization=False,
            next_wakeup_at=None,
        )

    repair_linked = observation.get("repair_linked") is True
    repair_deployed = (
        observation.get("repair_deployed_verified") is True
        or observation.get("repair_deployed") is True
    )
    readiness_ready = observation.get("readiness_ready") is True
    if repair_linked and not repair_deployed and observation.get("repair_wait_state") == "needs_action":
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind=str(observation.get("repair_wait_kind", "repair_delivery_needs_decision")),
            owner=str(observation.get("repair_wait_owner", "user")),
            requires_authorization=observation.get("repair_wait_requires_authorization") is True,
            next_wakeup_at=None,
        )
    if repair_linked and not repair_deployed and observation.get("repair_fresh_readiness_required") is True:
        if "observe_readiness" in policy.allowed_actions:
            return _ready_action(policy, observation, category=category, action="observe_readiness",
                                 reason_kind="fresh_authority_readiness_required")
    if repair_linked and not repair_deployed:
        return _result(
            category=category,
            action=None,
            state="waiting",
            reason_kind=str(observation.get("repair_wait_kind") or "repair_deployment_wait"),
            owner="repair",
            requires_authorization=False,
            next_wakeup_at=_wakeup(now, policy.backoff_seconds),
        )
    if repair_deployed and not readiness_ready:
        if "observe_readiness" in policy.allowed_actions:
            return _ready_action(
                policy, observation, category=category, action="observe_readiness",
                reason_kind="fresh_readiness_required",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="readiness_not_authorized", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if (
        readiness_ready
        and category == "validation_failure"
        and node_state == "blocked"
        and observation.get("node_is_verifier") is False
        and observation.get("validation_succeeded") is not True
        and "repair_source" in policy.allowed_actions
    ):
        return _ready_action(
            policy, observation, category=category, action="repair_source",
            reason_kind="blocked_source_repair_authorized",
        )

    if readiness_ready and category == "validation_failure":
        if observation.get("implementation_ready") is not True:
            return _result(
                category=category, action=None, state="needs_action",
                reason_kind="implementation_not_ready", owner="user",
                requires_authorization=True, next_wakeup_at=None,
            )
        if observation.get("validation_succeeded") is True:
            if "source_only_recovery" in policy.allowed_actions:
                return _ready_action(
                    policy, observation, category=category, action="source_only_recovery",
                    reason_kind="source_only_after_validation",
                )
            return _result(
                category=category, action=None, state="needs_action",
                reason_kind="source_recovery_not_authorized", owner="user",
                requires_authorization=True, next_wakeup_at=None,
            )
        profile = _text(observation.get("validation_profile"))
        if profile in policy.validation_profiles and "narrow_validation" in policy.allowed_actions:
            return _ready_action(
                policy, observation, category=category, action="narrow_validation",
                reason_kind="validation_authorized",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="validation_authorization_required", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if readiness_ready and (
        (_text(observation.get("phase")) or "") == "pre_execution"
        and category in {"dependency", "environment", "network", "tooling_bug"}
        and (observation.get("executor_not_started") is True
             or (category == "dependency" and observation.get("only_missing_dependency") is True))
    ):
        if "resume_node" in policy.allowed_actions:
            return _ready_action(
                policy, observation, category=category, action="resume_node",
                reason_kind="resume_after_readiness",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="resume_not_authorized", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if readiness_ready and category in {"dependency", "environment", "network"}:
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="readiness_already_satisfied", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if readiness_ready and repair_deployed and category == "tooling_bug":
        if observation.get("validation_succeeded") is True and "source_only_recovery" in policy.allowed_actions:
            return _ready_action(policy, observation, category=category, action="source_only_recovery",
                                 reason_kind="source_only_after_repair_validation")
        if observation.get("validation_profile") in policy.validation_profiles and "narrow_validation" in policy.allowed_actions:
            return _ready_action(policy, observation, category=category, action="narrow_validation",
                                 reason_kind="repair_requires_parent_validation")
        return _result(category=category, action=None, state="needs_action", owner="user",
                       reason_kind="parent_validation_authorization_required",
                       requires_authorization=True, next_wakeup_at=None)

    if category in {"dependency", "environment", "network"}:
        if category == "dependency" and observation.get("dependencies_ready") is False:
            action: Action = "materialize_dependencies"
            reason = "dependency_materialization"
        else:
            action = "observe_readiness"
            reason = "readiness_observation"
        if action in policy.allowed_actions:
            return _ready_action(policy, observation, category=category, action=action, reason_kind=reason)
        if action != "observe_readiness" and "observe_readiness" in policy.allowed_actions:
            return _ready_action(
                policy, observation, category=category, action="observe_readiness",
                reason_kind="readiness_observation",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="action_not_authorized", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if category == "validation_failure":
        if observation.get("implementation_ready") is not True:
            if "observe_readiness" in policy.allowed_actions:
                return _ready_action(
                    policy, observation, category=category, action="observe_readiness",
                    reason_kind="implementation_readiness_required",
                )
            return _result(
                category=category, action=None, state="needs_action",
                reason_kind="implementation_not_ready", owner="user",
                requires_authorization=True, next_wakeup_at=None,
            )
        if observation.get("validation_succeeded") is True:
            if "source_only_recovery" in policy.allowed_actions:
                return _ready_action(
                    policy, observation, category=category, action="source_only_recovery",
                    reason_kind="source_only_after_validation",
                )
            return _result(
                category=category, action=None, state="needs_action",
                reason_kind="source_recovery_not_authorized", owner="user",
                requires_authorization=True, next_wakeup_at=None,
            )
        profile = _text(observation.get("validation_profile"))
        if profile in policy.validation_profiles and "narrow_validation" in policy.allowed_actions:
            return _ready_action(
                policy, observation, category=category, action="narrow_validation",
                reason_kind="validation_authorized",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="validation_authorization_required", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if category == "tooling_bug":
        if observation.get("repair_requested") is True:
            return _result(
                category=category, action=None, state="waiting",
                reason_kind="repair_request_pending", owner="repair",
                requires_authorization=False, next_wakeup_at=_wakeup(now, policy.backoff_seconds),
            )
        if "request_repair" in policy.allowed_actions and policy.repair_repository and policy.repair_allowed_scopes:
            return _ready_action(
                policy, observation, category=category, action="request_repair",
                reason_kind="repair_authorized",
            )
        return _result(
            category=category, action=None, state="needs_action",
            reason_kind="repair_authorization_required", owner="user",
            requires_authorization=True, next_wakeup_at=None,
        )

    if category == "capacity":
        return _result(
            category=category, action=None, state="waiting",
            reason_kind="capacity_wait", owner="authority",
            requires_authorization=False, next_wakeup_at=_wakeup(now, policy.backoff_seconds),
        )
    return _result(
        category=category, action=None, state="needs_action",
        reason_kind="unknown_evidence", owner="user",
        requires_authorization=True, next_wakeup_at=None,
    )
