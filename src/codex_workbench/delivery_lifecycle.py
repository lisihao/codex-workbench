"""Typed, durable vocabulary for one end-to-end delivery objective.

The scheduler remains the existing coordinator.  This module only validates
the immutable request and the state that the SQLite store persists for it, so
an older binary can safely ignore the new tables during a rollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from datetime import datetime
import json
import math
import threading
from typing import TYPE_CHECKING, Any, Mapping, Protocol

if TYPE_CHECKING:
    from .store import WorkbenchStore


DELIVERY_STAGES = (
    "plan",
    "implement",
    "verify",
    "integrate",
    "ci",
    "publish",
    "deploy",
    "live-verify",
)
DELIVERY_STAGE_INDEX = {stage: index for index, stage in enumerate(DELIVERY_STAGES)}
DELIVERY_OBJECTIVE_STATES = frozenset(
    {"active", "waiting", "needs_decision", "complete", "cancelled"}
)
DELIVERY_RECEIPT_STATES = frozenset(
    {"succeeded", "failed", "blocked", "indeterminate", "denied"}
)
DELIVERY_FAILURE_KINDS = frozenset(
    {
        "missing-artifact-input",
        "invalid-planning-scopes",
        "stale-base-ref",
        "execution-environment",
        "provider-unavailable-quota",
        "result-envelope-rejected",
        "verification-failure",
        "unknown-effects",
        "permission-denied",
        "missing-essential-user-choice",
    }
)
MANUAL_DECISION_FAILURE_KINDS = frozenset(
    {"unknown-effects", "permission-denied", "missing-essential-user-choice"}
)
# A deployment receipt is not enough to establish what actually ran.  Keep
# the host toolchain identities alongside the source/build/deploy/runtime
# chain so a live check cannot accidentally bless a process from a different
# Node/pnpm environment.
IDENTITY_FIELDS = ("node", "pnpm", "source", "build", "deploy", "runtime")
EXTERNAL_DELIVERY_STAGES = frozenset({"integrate", "ci", "publish", "deploy", "live-verify"})

# A retry bit alone is too weak to make an interrupted delivery actionable.
# Keep the recovery vocabulary deliberately small and tied to an observed
# failure kind.  The coordinator can execute only the actions already inside
# its authority; these values never grant a worker a new delivery permission.
_FAILURE_RECOVERY_ACTIONS = {
    "missing-artifact-input": "stage_and_hash_artifact_input",
    "invalid-planning-scopes": "repair_planning_scopes_idempotently",
    "stale-base-ref": "refresh_authoritative_ref_preserve_provenance",
    "execution-environment": "discover_authorized_host_test_capability",
    "provider-unavailable-quota": "wait_for_provider_quota",
    "result-envelope-rejected": "repair_result_envelope",
    # A failing check must receive a bounded diagnosis/repair pass.  It is
    # intentionally not represented as an unchanged provider retry.
    "verification-failure": "diagnose_and_repair",
    "unknown-effects": "reconcile_authoritatively",
    "permission-denied": "grant_scope_limited_authorization",
    "missing-essential-user-choice": "provide_essential_user_choice",
}


def _json_safe(value: Any, label: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-safe") from error
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def normalize_timestamp(value: object, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    text = _nonempty_text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def normalize_stage(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("delivery stage must be a string")
    normalized = value.strip().lower().replace("_", "-")
    if normalized not in DELIVERY_STAGE_INDEX:
        raise ValueError(f"unsupported delivery stage {value!r}")
    return normalized


def normalize_next_action(value: object, *, stage: str) -> dict[str, Any]:
    if isinstance(value, str):
        action = _nonempty_text(value, "next_action")
        return {"action": action, "stage": stage}
    if not isinstance(value, dict):
        raise ValueError("next_action must be a string or object")
    normalized = dict(value)
    normalized["action"] = _nonempty_text(normalized.get("action"), "next_action.action")
    declared_stage = normalized.get("stage", stage)
    normalized["stage"] = normalize_stage(declared_stage)
    if normalized["stage"] != stage:
        raise ValueError("next_action stage must match the objective stage")
    return _json_safe(normalized, "next_action")


def _normalize_explicit_value(value: object, label: str) -> Any:
    if isinstance(value, str):
        return _nonempty_text(value, label)
    if isinstance(value, dict):
        if not value:
            raise ValueError(f"{label} must not be empty")
        if any(not isinstance(key, str) or not key.strip() for key in value):
            raise ValueError(f"{label} keys must be non-empty strings")
        return _json_safe(dict(value), label)
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"{label} must not be empty")
        return _json_safe(list(value), label)
    raise ValueError(f"{label} must be an explicit string, object, or list")


def normalize_budget(value: object | None) -> dict[str, Any]:
    """Normalize bounded retry/time/cost accounting without inventing spend."""

    raw = {} if value is None else value
    if not isinstance(raw, dict):
        raise ValueError("budget must be an object")
    aliases = {
        "max_attempts": "attempt_limit",
        "max_time_seconds": "time_budget_seconds",
        "max_cost": "cost_budget",
        "retry_backoff_seconds": "base_backoff_seconds",
    }
    normalized = {aliases.get(key, key): item for key, item in raw.items()}

    result: dict[str, Any] = {
        "attempt_limit": normalized.get("attempt_limit", 3),
        "attempts_used": normalized.get("attempts_used", 0),
        "time_budget_seconds": normalized.get("time_budget_seconds", 3600),
        "elapsed_seconds": normalized.get("elapsed_seconds", 0),
        "cost_budget": normalized.get("cost_budget", 0.0),
        "cost_used": normalized.get("cost_used", 0.0),
        "base_backoff_seconds": normalized.get("base_backoff_seconds", 5),
        "max_backoff_seconds": normalized.get("max_backoff_seconds", 300),
    }
    for name in (
        "attempt_limit",
        "attempts_used",
        "time_budget_seconds",
        "base_backoff_seconds",
        "max_backoff_seconds",
    ):
        item = result[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"budget {name} must be a non-negative integer")
    if result["max_backoff_seconds"] < result["base_backoff_seconds"]:
        raise ValueError("budget max_backoff_seconds must be at least base_backoff_seconds")
    for name in ("elapsed_seconds", "cost_budget", "cost_used"):
        item = result[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise ValueError(f"budget {name} must be a finite non-negative number")
        result[name] = float(item)
    # Actual attempts/cost may arrive after a provider action. Preserve an
    # overrun as evidence for a decision request instead of rejecting it and
    # losing the recovery boundary.
    return result


def normalize_identities(value: object | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("identities must be an object")
    aliases = {
        "node_version": "node",
        "node_identity": "node",
        "pnpm_version": "pnpm",
        "pnpm_identity": "pnpm",
        "source_sha": "source",
        "source_identity": "source",
        "build_id": "build",
        "build_identity": "build",
        "deployment_id": "deploy",
        "deploy_id": "deploy",
        "deployment_identity": "deploy",
        "runtime_id": "runtime",
        "runtime_identity": "runtime",
    }
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        name = aliases.get(key, key)
        if name not in IDENTITY_FIELDS:
            raise ValueError(f"unsupported identity field {key!r}")
        if item is None:
            continue
        if isinstance(item, str) and not item.strip():
            raise ValueError(f"identity {name} must not be empty")
        normalized[name] = _json_safe(item, f"identity {name}")
    return normalized


def merge_identities(existing: object, incoming: object | None) -> dict[str, Any]:
    current = normalize_identities(existing)
    additions = normalize_identities(incoming)
    for name, item in additions.items():
        if name in current and current[name] != item:
            raise ValueError(f"{name} identity does not match the frozen delivery objective")
        current[name] = item
    return current


def normalize_wait_reason(value: object, *, default_kind: str | None = None) -> dict[str, Any]:
    if isinstance(value, str):
        detail = _nonempty_text(value, "wait_reason")
        kind = default_kind or "execution-environment"
        return {"kind": kind, "detail": detail}
    if not isinstance(value, dict):
        raise ValueError("wait_reason must be a string or object")
    normalized = dict(value)
    kind = normalized.get("kind", default_kind)
    if kind not in DELIVERY_FAILURE_KINDS:
        raise ValueError("wait_reason.kind must be a recognized delivery failure kind")
    normalized["kind"] = str(kind)
    normalized["detail"] = _nonempty_text(
        normalized.get("detail", normalized.get("reason")), "wait_reason.detail"
    )
    resolution = normalized.get("resolution")
    if resolution is not None:
        normalized["resolution"] = _nonempty_text(resolution, "wait_reason.resolution")
    return _json_safe(normalized, "wait_reason")


def recovery_action_for_failure(value: object) -> dict[str, Any]:
    """Return the durable, least-authority next action for a classified failure.

    ``unknown-effects`` is deliberately reconciliation-only: expiry, a
    restarted process, or a process-exists observation can never turn it into
    a success.  Permission and essential-choice failures are likewise kept as
    explicit human-decision boundaries.  Other failures have an actionable
    coordinator recovery route, but whether the bounded budget permits that
    route is decided by the store at receipt time.
    """

    failure = normalize_wait_reason(value)
    kind = failure["kind"]
    action = _FAILURE_RECOVERY_ACTIONS[kind]
    return {
        "action": action,
        "kind": kind,
        "automatic": kind not in MANUAL_DECISION_FAILURE_KINDS,
        "requires_authoritative_reconciliation": kind == "unknown-effects",
        "requires_human_decision": kind in MANUAL_DECISION_FAILURE_KINDS,
    }


def _delivery_endpoints(request: Mapping[str, Any]) -> dict[str, Any]:
    endpoints = request.get("requested_endpoints", request.get("endpoints"))
    if not isinstance(endpoints, dict) or not endpoints:
        raise ValueError("requested_endpoints must be a non-empty object")
    return dict(endpoints)


def _configured_check_names(value: object, label: str) -> tuple[str, ...]:
    """Turn an explicit health/functional configuration into stable names."""

    if isinstance(value, str):
        return (_nonempty_text(value, label),)
    if isinstance(value, Mapping):
        if "name" in value or "id" in value:
            return (_nonempty_text(value.get("name", value.get("id")), label),)
        names = tuple(_nonempty_text(name, label) for name in value)
        if names:
            return names
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(_nonempty_text(item, label))
            elif isinstance(item, Mapping):
                names.append(_nonempty_text(item.get("name", item.get("id")), label))
            else:
                raise ValueError(f"{label} entries must be names or objects with a name")
        if names:
            return tuple(names)
    raise ValueError(f"{label} must contain at least one configured check")


def live_verification_requirements(request: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return the exact health and functional checks requested for a rollout.

    A process listing, a passed TTL, or an unspecified probe is not a live
    verification receipt.  The request has to name both classes of checks up
    front so a later coordinator cannot weaken the delivery contract.
    """

    deployment = _delivery_endpoints(request).get("deployment")
    if deployment is None:
        return {}
    if not isinstance(deployment, Mapping):
        raise ValueError("requested_endpoints.deployment must be an object")
    health = deployment.get("health_checks", deployment.get("health"))
    functional = deployment.get("functional_checks", deployment.get("functional"))
    return {
        "health": _configured_check_names(health, "deployment health_checks"),
        "functional": _configured_check_names(functional, "deployment functional_checks"),
    }


def required_delivery_stages(request: Mapping[str, Any]) -> tuple[str, ...]:
    """Derive the immutable stage envelope from explicit requested endpoints.

    Planning, implementation, and verification always exist.  GitHub
    integration/CI and publication are separate, as are deployment and live
    verification.  This keeps a request for a PR from silently becoming a
    release or a rollout.
    """

    endpoints = _delivery_endpoints(request)
    stages: list[str] = ["plan", "implement", "verify"]
    github = endpoints.get("github")
    if github is not None:
        if not isinstance(github, Mapping):
            raise ValueError("requested_endpoints.github must be an object")
        remote = github.get("remote", "origin")
        base_branch = github.get("base_branch")
        merge = github.get("merge", False)
        release_tag = github.get("release_tag")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("requested_endpoints.github.remote must be a non-empty string")
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise ValueError("requested_endpoints.github.base_branch is required")
        if not isinstance(merge, bool):
            raise ValueError("requested_endpoints.github.merge must be a boolean")
        if release_tag is not None and (not isinstance(release_tag, str) or not release_tag.strip()):
            raise ValueError("requested_endpoints.github.release_tag must be a non-empty string or null")
        if "publish" in github and not isinstance(github["publish"], bool):
            raise ValueError("requested_endpoints.github.publish must be a boolean")
        runtime_identities = github.get("required_runtime_identities", [])
        if (not isinstance(runtime_identities, list)
                or any(not isinstance(item, str) or item not in {"node", "pnpm"} for item in runtime_identities)):
            raise ValueError("GitHub required_runtime_identities must list node and/or pnpm")
        stages.extend(("integrate", "ci"))
        publication = endpoints.get("publish")
        publish_requested = (
            bool(github.get("publish"))
            or bool(github.get("merge"))
            or github.get("release_tag") is not None
            or publication is True
            or isinstance(publication, Mapping)
        )
        if publish_requested:
            stages.append("publish")
    deployment = endpoints.get("deployment")
    if deployment is not None:
        if not isinstance(deployment, Mapping):
            raise ValueError("requested_endpoints.deployment must be an object")
        target = deployment.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("requested_endpoints.deployment.target is required")
        # Validate the request now; live verification cannot be retrofitted
        # with a weaker probe after deployment has already happened.
        live_verification_requirements(request)
        stages.extend(("deploy", "live-verify"))
    return tuple(stages)


def required_completion_identities(request: Mapping[str, Any]) -> tuple[str, ...]:
    """Return identities that must form one continuous accepted delivery."""

    stages = required_delivery_stages(request)
    if "live-verify" in stages:
        return IDENTITY_FIELDS
    if "ci" in stages:
        github = _delivery_endpoints(request)["github"]
        runtimes = github.get("required_runtime_identities", [])
        return tuple(name for name in ("node", "pnpm") if name in runtimes) + ("source", "build")
    return ("source",)


def _check_passed(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"ok", "passed", "healthy", "success", "succeeded"}
    if isinstance(value, Mapping):
        if value.get("ok") is True or value.get("passed") is True:
            return True
        return _check_passed(value.get("status", value.get("state")))
    return False


def _observed_checks(value: object, label: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        if "name" in value or "id" in value:
            return {_nonempty_text(value.get("name", value.get("id")), label): value}
        return {_nonempty_text(name, label): item for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        observed: dict[str, object] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError(f"{label} results must be objects with a name")
            name = _nonempty_text(item.get("name", item.get("id")), label)
            if name in observed:
                raise ValueError(f"{label} contains duplicate result {name!r}")
            observed[name] = item
        return observed
    raise ValueError(f"{label} results are required")


def validate_live_verification_receipt(
    request: Mapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    """Reject a live success that lacks every configured health/function proof."""

    requirements = live_verification_requirements(request)
    if not requirements:
        return
    checks = receipt.get("checks", receipt)
    if not isinstance(checks, Mapping):
        raise ValueError("live verification receipt checks must be an object")
    for kind, required_names in requirements.items():
        raw = checks.get(kind, receipt.get(f"{kind}_checks"))
        observed = _observed_checks(raw, f"{kind} check")
        missing = [name for name in required_names if name not in observed]
        failed = [name for name in required_names if name in observed and not _check_passed(observed[name])]
        if missing or failed:
            detail: list[str] = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if failed:
                detail.append("not passed " + ", ".join(failed))
            raise ValueError(f"live verification {kind} checks are incomplete: {'; '.join(detail)}")


def rollback_policy_is_preauthorized(request: Mapping[str, Any]) -> bool:
    """Whether the immutable deployment endpoint permits a reversible rollback."""

    deployment = _delivery_endpoints(request).get("deployment")
    if not isinstance(deployment, Mapping):
        return False
    policy = deployment.get("rollback_policy", deployment.get("rollback"))
    return isinstance(policy, Mapping) and policy.get("preauthorized") is True and policy.get("reversible") is True


def normalize_objective_request(value: object) -> dict[str, Any]:
    """Validate the immutable, user-visible delivery request envelope."""

    if not isinstance(value, dict):
        raise ValueError("delivery objective request must be an object")
    raw = dict(value)
    endpoints = raw.get("requested_endpoints", raw.get("endpoints"))
    scope = raw.get("scope", raw.get("requested_scope"))
    authority = raw.get("authority", raw.get("requested_authority"))
    if endpoints is None or scope is None or authority is None:
        raise ValueError("delivery objective requires requested_endpoints, scope, and authority")
    evidence_fingerprints = raw.get("evidence_fingerprints", {})
    if not isinstance(evidence_fingerprints, dict):
        raise ValueError("evidence_fingerprints must be an object")
    recognized = {
        "requested_endpoints",
        "endpoints",
        "scope",
        "requested_scope",
        "authority",
        "requested_authority",
        "budget",
        "budgets",
        "due_at",
        "next_wakeup_at",
        "identities",
        "evidence_fingerprints",
        "next_action",
    }
    normalized = {
        "requested_endpoints": _normalize_explicit_value(endpoints, "requested_endpoints"),
        "scope": _normalize_explicit_value(scope, "scope"),
        "authority": _normalize_explicit_value(authority, "authority"),
        "budget": normalize_budget(raw.get("budget", raw.get("budgets"))),
        "due_at": normalize_timestamp(raw.get("due_at"), "due_at"),
        "next_wakeup_at": normalize_timestamp(raw.get("next_wakeup_at"), "next_wakeup_at"),
        "identities": normalize_identities(raw.get("identities")),
        "evidence_fingerprints": _json_safe(
            dict(evidence_fingerprints), "evidence_fingerprints"
        ),
        "metadata": _json_safe(
            {key: item for key, item in raw.items() if key not in recognized},
            "delivery objective metadata",
        ),
    }
    # Derive these before persisting the immutable request.  They are not
    # duplicated into the request itself; computing from the frozen endpoint
    # envelope keeps older rows readable and prevents a mutable stage list.
    required_delivery_stages(normalized)
    next_action = raw.get("next_action", {"action": "plan", "stage": "plan"})
    normalized["next_action"] = normalize_next_action(next_action, stage="plan")
    return normalized


@dataclass(frozen=True)
class DeliveryStageContext:
    """One fenced, explicitly authorized lifecycle-stage invocation."""

    objective: dict[str, Any]
    task: dict[str, Any] | None
    stage: str
    attempt: int
    dispatch_id: str
    authorization: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeliveryStageOutcome:
    """A fixture-safe adapter result that can become a durable stage receipt."""

    receipt_id: str
    status: str = "succeeded"
    receipt: Mapping[str, Any] = field(default_factory=dict)
    evidence_fingerprint: str | None = None
    identities: Mapping[str, Any] = field(default_factory=dict)
    failure: Mapping[str, Any] | None = None
    retry_eligible: bool = True
    next_wakeup_at: str | None = None
    cost_delta: float = 0.0

    def __post_init__(self) -> None:
        _nonempty_text(self.receipt_id, "delivery stage receipt_id")
        if self.status not in DELIVERY_RECEIPT_STATES | {"deferred"}:
            raise ValueError(f"unsupported delivery stage outcome {self.status!r}")
        if not isinstance(self.receipt, Mapping):
            raise ValueError("delivery stage outcome receipt must be an object")
        if not isinstance(self.identities, Mapping):
            raise ValueError("delivery stage outcome identities must be an object")
        if self.status == "succeeded" and (
            not isinstance(self.evidence_fingerprint, str) or not self.evidence_fingerprint.strip()
        ):
            raise ValueError("successful delivery stage outcome requires an Evidence fingerprint")
        if self.status != "succeeded" and not isinstance(self.failure, Mapping):
            raise ValueError("non-successful delivery stage outcome requires a classified failure")
        if not isinstance(self.retry_eligible, bool):
            raise ValueError("delivery stage outcome retry_eligible must be a boolean")
        if isinstance(self.cost_delta, bool) or not isinstance(self.cost_delta, (int, float)):
            raise ValueError("delivery stage outcome cost_delta must be a finite non-negative number")
        if not math.isfinite(float(self.cost_delta)) or self.cost_delta < 0:
            raise ValueError("delivery stage outcome cost_delta must be a finite non-negative number")


class DeliveryStageAdapter(Protocol):
    """Authority-owned adapter.  Tests inject fixture implementations only."""

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome: ...


class CompositeDeliveryStageAdapter:
    """Route each stage to a bounded authority-owned adapter.

    A GitHub adapter can therefore own only integration/publication while a
    deployment adapter owns rollout, checks, and rollback.  Missing stages
    fail closed in the reconciler instead of borrowing another adapter's
    authority.
    """

    def __init__(self, adapters: Mapping[str, DeliveryStageAdapter]):
        normalized: dict[str, DeliveryStageAdapter] = {}
        for stage, adapter in adapters.items():
            normalized_stage = normalize_stage(stage)
            if not hasattr(adapter, "execute_stage"):
                raise ValueError(f"delivery stage {normalized_stage} has no executable adapter")
            normalized[normalized_stage] = adapter
        self.adapters = normalized

    def _adapter(self, context: DeliveryStageContext) -> DeliveryStageAdapter:
        try:
            return self.adapters[context.stage]
        except KeyError as error:
            raise ValueError(f"no authority-owned adapter is configured for {context.stage}") from error

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        return self._adapter(context).execute_stage(context)

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        reconcile = getattr(self._adapter(context), "reconcile_stage", None)
        if not callable(reconcile):
            raise ValueError(f"no authoritative reconciliation adapter is configured for {context.stage}")
        return reconcile(context)

    def rollback_deployment(
        self, context: DeliveryStageContext, outcome: DeliveryStageOutcome
    ) -> Mapping[str, Any]:
        rollback = getattr(self._adapter(context), "rollback_deployment", None)
        if not callable(rollback):
            raise ValueError("no preauthorized reversible rollback adapter is configured")
        return rollback(context, outcome)


class FixtureDeliveryStageAdapter:
    """Deterministic scripted adapter used by lifecycle fixtures.

    It deliberately has no subprocess, network, login, or deployment hooks.
    A restart can use ``reconcile_stage`` to return a previously scripted
    receipt without replaying the execution method.
    """

    def __init__(
        self,
        outcomes: Mapping[object, DeliveryStageOutcome | Exception],
        *,
        rollback_outcomes: Mapping[object, Mapping[str, Any] | Exception] | None = None,
    ):
        self.outcomes = dict(outcomes)
        self.rollback_outcomes = dict(rollback_outcomes or {})
        self.calls: list[tuple[str, str, int, str]] = []
        self.rollback_calls: list[tuple[str, str, int, str]] = []

    @staticmethod
    def _lookup(mapping: Mapping[object, Any], context: DeliveryStageContext) -> Any:
        for key in ((context.stage, context.attempt), context.stage):
            if key in mapping:
                value = mapping[key]
                if isinstance(value, Exception):
                    raise value
                return value
        raise KeyError(f"no fixture outcome for {context.stage} attempt {context.attempt}")

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        self.calls.append(("execute", context.stage, context.attempt, context.dispatch_id))
        value = self._lookup(self.outcomes, context)
        if not isinstance(value, DeliveryStageOutcome):
            raise TypeError("fixture stage outcome must be DeliveryStageOutcome")
        return value

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        self.calls.append(("reconcile", context.stage, context.attempt, context.dispatch_id))
        value = self._lookup(self.outcomes, context)
        if not isinstance(value, DeliveryStageOutcome):
            raise TypeError("fixture stage outcome must be DeliveryStageOutcome")
        return value

    def rollback_deployment(
        self, context: DeliveryStageContext, _outcome: DeliveryStageOutcome
    ) -> Mapping[str, Any]:
        self.rollback_calls.append(("rollback", context.stage, context.attempt, context.dispatch_id))
        value = self._lookup(self.rollback_outcomes, context)
        if not isinstance(value, Mapping):
            raise TypeError("fixture rollback outcome must be an object")
        return dict(value)


class DeliveryAdmissionBusy(RuntimeError):
    """A worker or another rollout won the atomic deployment admission race."""


class DeliveryLifecycleReconciler:
    """Run at most one fenced stage for each due durable delivery objective.

    The existing SQLite coordinator remains the owner of leases, wakeups,
    receipts, and events.  This class only translates an authority-owned
    adapter result into those existing durable boundaries.  It never supplies
    a production adapter itself, so constructing it cannot create a GitHub,
    deployment, model, or process-management side effect.
    """

    def __init__(
        self,
        store: "WorkbenchStore",
        *,
        owner_id: str,
        coordinator_epoch: int,
        adapter: DeliveryStageAdapter | None,
        lease_seconds: int = 60,
        heartbeat_seconds: float = 20,
    ):
        self.store = store
        self.owner_id = _nonempty_text(owner_id, "delivery lifecycle owner_id")
        if isinstance(coordinator_epoch, bool) or not isinstance(coordinator_epoch, int) or coordinator_epoch <= 0:
            raise ValueError("delivery lifecycle coordinator_epoch must be positive")
        self.coordinator_epoch = coordinator_epoch
        self.adapter = adapter
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery lease_seconds must be between 1 and 3600")
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("delivery heartbeat must be positive and shorter than its lease")
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def reconcile_once(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Claim due work once; repeated calls are the bounded reconciliation loop."""

        self.store.reconcile_delivery_safe_point_waits()
        results: list[dict[str, Any]] = []
        for objective in self.store.list_due_delivery_objectives(
            limit=limit,
            owner_id=self.owner_id,
            coordinator_epoch=self.coordinator_epoch,
        ):
            try:
                claimed = self.store.claim_delivery_objective(
                    objective["objective_id"],
                    self.owner_id,
                    self.coordinator_epoch,
                    expected_revision=objective["state_revision"],
                    lease_seconds=self.lease_seconds,
                )
            except Exception as error:
                # A competing coordinator or late CAS is expected under
                # restart/duplicate delivery; it must not turn into a second
                # adapter call.
                results.append(
                    {
                        "objective_id": objective["objective_id"],
                        "status": "not_claimed",
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
                continue
            if claimed is None:
                continue
            results.append(self._reconcile_claimed(claimed))
        return results

    def _reconcile_claimed(self, objective: dict[str, Any]) -> dict[str, Any]:
        stage = str(objective["stage"])
        attempt = int(objective["stage_attempt"])
        if self._deadline_expired(objective):
            return self._record_outcome(
                objective,
                DeliveryStageOutcome(
                    receipt_id=f"{objective['objective_id']}:{stage}:{attempt}:deadline",
                    status="blocked",
                    receipt={"due_at": objective["due_at"], "stage": stage, "attempt": attempt},
                    failure={
                        "kind": "execution-environment",
                        "detail": "delivery objective due time elapsed before its next durable action",
                    },
                    retry_eligible=False,
                ),
                dispatch_id=None,
            )
        try:
            task = self.store.get_task(str(objective["task_id"]))
        except KeyError:
            task = None

        authorization: dict[str, Any] | None = None
        if stage in EXTERNAL_DELIVERY_STAGES:
            authorization_status = self.store.delivery_stage_authorization(
                str(objective["objective_id"]), stage
            )
            if not authorization_status["authorized"]:
                existing_dispatch = self.store.get_delivery_stage_dispatch(
                    str(objective["objective_id"]), stage, attempt
                )
                if existing_dispatch is not None and existing_dispatch["state"] == "started":
                    return self._record_outcome(
                        objective,
                        DeliveryStageOutcome(
                            receipt_id=f"{existing_dispatch['dispatch_id']}:authority-lost",
                            status="indeterminate", retry_eligible=False,
                            receipt={"authorization": authorization_status, "prior_dispatch": existing_dispatch},
                            failure={"kind": "unknown-effects", "detail": "authorization was lost after dispatch; reconcile the recorded effect before any new action"},
                        ),
                        dispatch_id=str(existing_dispatch["dispatch_id"]),
                    )
                return self._record_outcome(
                    objective,
                    DeliveryStageOutcome(
                        receipt_id=f"{objective['objective_id']}:{stage}:{attempt}:authorization",
                        status="denied",
                        receipt={"authorization": authorization_status},
                        failure={
                            "kind": "permission-denied",
                            "detail": str(authorization_status["reason"]),
                        },
                        retry_eligible=False,
                    ),
                    dispatch_id=None,
                )
            authorization = authorization_status.get("authorization")

        if stage == "deploy":
            gate = self.store.delivery_admission_gate()
            if gate is not None and gate["objective_id"] != objective["objective_id"]:
                return self._defer_rollout_admission(objective, "another authority rollout is in progress")
            blockers = self.store.delivery_deployment_blockers(str(objective["objective_id"]))
            if blockers:
                deferred = self.store.defer_delivery_deployment_safe_point(
                    str(objective["objective_id"]),
                    expected_revision=int(objective["state_revision"]),
                    coordinator_epoch=self.coordinator_epoch,
                    lease_epoch=int(objective["lease"]["lease_epoch"]),
                    blockers=blockers,
                )
                return {
                    "objective_id": objective["objective_id"],
                    "status": "waiting_for_safe_point",
                    "objective": deferred,
                }

        try:
            dispatch = self.store.begin_delivery_stage_dispatch(
                str(objective["objective_id"]), stage=stage, attempt=attempt,
                expected_revision=int(objective["state_revision"]), coordinator_epoch=self.coordinator_epoch,
                lease_epoch=int(objective["lease"]["lease_epoch"]),
                adapter_name=(type(self.adapter).__name__ if self.adapter is not None else "unconfigured"),
            )
        except DeliveryAdmissionBusy:
            return self._defer_rollout_admission(objective, "authority work must drain before rollout dispatch")
        context = DeliveryStageContext(
            objective=objective,
            task=task,
            stage=stage,
            attempt=attempt,
            dispatch_id=str(dispatch["dispatch_id"]),
            authorization=authorization,
        )
        with self._keep_stage_lease(objective) as lease:
            outcome = self._invoke_adapter(context, dispatch)
            if stage == "deploy":
                outcome = self._apply_preauthorized_rollback(context, dispatch, outcome)
        return self._record_outcome(lease["objective"], outcome, dispatch_id=context.dispatch_id)

    @contextmanager
    def _keep_stage_lease(self, objective: dict[str, Any]):
        """Renew only the currently owned stage while its adapter is in flight.

        A lost lease leaves the dispatch unsettled for authoritative recovery;
        the old caller cannot stamp a late result onto a newer owner.
        """
        stop = threading.Event()
        lease = {"objective": objective}
        errors: list[Exception] = []

        def renew() -> None:
            while not stop.wait(self.heartbeat_seconds):
                current = lease["objective"]
                try:
                    lease["objective"] = self.store.renew_delivery_objective_lease(
                        str(current["objective_id"]), coordinator_epoch=self.coordinator_epoch,
                        lease_epoch=int(current["lease"]["lease_epoch"]),
                        expected_revision=int(current["state_revision"]),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception as error:
                    errors.append(error)
                    return

        heartbeat = threading.Thread(target=renew, name="delivery-lease-heartbeat", daemon=True)
        heartbeat.start()
        try:
            yield lease
        finally:
            stop.set()
            heartbeat.join()
        if errors:
            raise errors[0]

    def _defer_rollout_admission(self, objective: dict[str, Any], detail: str) -> dict[str, Any]:
        deferred = self.store.defer_delivery_observation(
            str(objective["objective_id"]), expected_revision=int(objective["state_revision"]),
            coordinator_epoch=self.coordinator_epoch, lease_epoch=int(objective["lease"]["lease_epoch"]),
            reason={"kind": "execution-environment", "detail": detail}, next_wakeup_at=None, dispatch_id=None,
        )
        return {"objective_id": objective["objective_id"], "status": "deferred", "objective": deferred}

    @staticmethod
    def _deadline_expired(objective: Mapping[str, Any]) -> bool:
        due_at = objective.get("due_at")
        if not isinstance(due_at, str):
            return True
        try:
            parsed = datetime.fromisoformat(due_at)
        except ValueError:
            return True
        if parsed.tzinfo is None:
            return True
        return datetime.now(parsed.tzinfo) >= parsed

    def _invoke_adapter(
        self, context: DeliveryStageContext, dispatch: Mapping[str, Any]
    ) -> DeliveryStageOutcome:
        if self.adapter is None:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:adapter-unconfigured",
                status="blocked",
                receipt={"dispatch_id": context.dispatch_id},
                failure={
                    "kind": "execution-environment",
                    "detail": "no authority-owned delivery lifecycle adapter is configured",
                },
                retry_eligible=False,
            )
        try:
            if dispatch["new"]:
                outcome = self.adapter.execute_stage(context)
            else:
                reconcile = getattr(self.adapter, "reconcile_stage", None)
                if not callable(reconcile):
                    return DeliveryStageOutcome(
                        receipt_id=f"{context.dispatch_id}:unreconciled",
                        status="indeterminate",
                        receipt={"dispatch_id": context.dispatch_id},
                        failure={
                            "kind": "unknown-effects",
                            "detail": "a prior external stage dispatch has no authoritative reconciliation adapter",
                        },
                        retry_eligible=False,
                    )
                outcome = reconcile(context)
            if not isinstance(outcome, DeliveryStageOutcome):
                raise TypeError("delivery stage adapter returned an invalid outcome")
            return outcome
        except Exception as error:
            # An adapter exception happens after dispatch intent was made
            # durable.  Never classify it as a harmless retry or replay it.
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:indeterminate",
                status="indeterminate",
                receipt={"dispatch_id": context.dispatch_id, "adapter_error": f"{type(error).__name__}: {error}"},
                failure={
                    "kind": "unknown-effects",
                    "detail": "stage adapter raised after durable dispatch intent; reconcile authoritatively",
                },
                retry_eligible=False,
            )

    def _apply_preauthorized_rollback(
        self,
        context: DeliveryStageContext,
        dispatch: Mapping[str, Any],
        outcome: DeliveryStageOutcome,
    ) -> DeliveryStageOutcome:
        if outcome.status != "failed" or not rollback_policy_is_preauthorized(context.objective):
            return outcome
        begin = self.store.begin_delivery_stage_rollback(
            context.dispatch_id,
            coordinator_epoch=self.coordinator_epoch,
            lease_epoch=int(context.objective["lease"]["lease_epoch"]),
        )
        rollback_receipt: Mapping[str, Any] | None = None
        if begin["new"]:
            rollback = getattr(self.adapter, "rollback_deployment", None) if self.adapter is not None else None
            if not callable(rollback):
                rollback_receipt = {
                    "verified": False,
                    "error": "preauthorized rollback adapter is unavailable",
                }
            else:
                try:
                    rollback_receipt = rollback(context, outcome)
                except Exception as error:
                    rollback_receipt = {
                        "verified": False,
                        "error": f"{type(error).__name__}: {error}",
                    }
            if not isinstance(rollback_receipt, Mapping) or rollback_receipt.get("verified") is not True:
                return replace(
                    outcome,
                    status="indeterminate",
                    receipt={
                        **dict(outcome.receipt),
                        "rollback": dict(rollback_receipt or {}),
                    },
                    failure={
                        "kind": "unknown-effects",
                        "detail": "deployment rollback could not be authoritatively verified",
                    },
                    retry_eligible=False,
                )
            self.store.settle_delivery_stage_rollback(
                context.dispatch_id,
                coordinator_epoch=self.coordinator_epoch,
                lease_epoch=int(context.objective["lease"]["lease_epoch"]),
                receipt=dict(rollback_receipt),
            )
        elif begin["state"] == "settled":
            rollback_receipt = begin["receipt"]
        else:
            return replace(
                outcome,
                status="indeterminate",
                receipt={**dict(outcome.receipt), "rollback": {"state": begin["state"]}},
                failure={
                    "kind": "unknown-effects",
                    "detail": "a prior rollback intent is unresolved; reconcile authoritatively",
                },
                retry_eligible=False,
            )
        return replace(outcome, receipt={**dict(outcome.receipt), "rollback": dict(rollback_receipt or {})})

    def _record_outcome(
        self,
        objective: Mapping[str, Any],
        outcome: DeliveryStageOutcome,
        *,
        dispatch_id: str | None,
    ) -> dict[str, Any]:
        if outcome.status == "deferred":
            if dispatch_id is None or outcome.failure is None:
                raise ValueError("a deferred observation requires its dispatch and wait reason")
            deferred = self.store.defer_delivery_observation(
                str(objective["objective_id"]),
                expected_revision=int(objective["state_revision"]),
                coordinator_epoch=self.coordinator_epoch,
                lease_epoch=int(objective["lease"]["lease_epoch"]),
                reason=dict(outcome.failure),
                next_wakeup_at=outcome.next_wakeup_at,
                dispatch_id=dispatch_id,
            )
            return {"objective_id": objective["objective_id"], "status": "deferred", "objective": deferred}
        result = self.store.record_delivery_stage_receipt(
            str(objective["objective_id"]),
            outcome.receipt_id,
            stage=str(objective["stage"]),
            attempt=int(objective["stage_attempt"]),
            expected_revision=int(objective["state_revision"]),
            coordinator_epoch=self.coordinator_epoch,
            lease_epoch=int(objective["lease"]["lease_epoch"]),
            status=outcome.status,
            receipt=dict(outcome.receipt),
            evidence_fingerprint=outcome.evidence_fingerprint,
            identities=dict(outcome.identities),
            failure=(dict(outcome.failure) if outcome.failure is not None else None),
            retry_eligible=outcome.retry_eligible,
            next_wakeup_at=outcome.next_wakeup_at,
            cost_delta=outcome.cost_delta,
            dispatch_id=dispatch_id,
        )
        return {
            "objective_id": objective["objective_id"],
            "status": "recorded",
            "receipt": result["receipt"],
            "objective": result["objective"],
            "idempotent": result["idempotent"],
        }
