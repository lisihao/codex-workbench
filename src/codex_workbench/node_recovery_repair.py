"""One fixed, journaled repair-planning action for bounded recovery defects."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Mapping, NotRequired, TypedDict

from .authority_service import AuthorityService
from .config import WorkbenchConfig
from .model import canonical_hash, canonical_json, now_iso
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError


_ACTION = "request_repair"
_TOOL = "workbench_request"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_MAX_REFS = 32
_MAX_TEXT = 500
_REPEATED_REPAIR_CATEGORIES = frozenset({
    "dependency", "environment", "network", "validation_failure", "tooling_bug",
})
_REPAIR_TRIGGER_FIELDS = frozenset({"episode_id", "stage_key"})


class RepairActionPlan(TypedDict):
    """Immutable planning-only repair request prepared from one parent episode."""

    action: str
    request_id: str
    parent_task_id: str
    parent_node_id: str
    parent_node_attempt: int
    parent_task_revision: int
    failure_fingerprint: str
    category: str
    stage_key: str
    repair_task_id: str
    repair_request_id: str
    repair_fingerprint: str
    repository: str
    base_sha: str
    allowed_scopes: list[str]
    evidence_refs: dict[str, str]
    policy_revision: int
    policy: dict[str, object]
    claude_allowed: bool
    arguments: dict[str, object]
    action_fingerprint: str
    repair_trigger: NotRequired[dict[str, str]]


class RepairActionReceipt(TypedDict):
    """Known Authority/planning outcome for the fixed repair action."""

    action: str
    request_id: str
    journal_status: str
    known_effects: bool
    receipt: dict[str, object] | None


class RepairNodeActions:
    """Create or read one bounded repair planning request per eligible failure."""

    def __init__(
        self,
        config: WorkbenchConfig,
        store: NodeRecoveryStore,
        authority_service: AuthorityService,
    ) -> None:
        self.config = config
        self.store = store
        self.authority_service = authority_service

    def prepare(
        self,
        observation: Mapping[str, Any],
        action: str,
        request_id: str,
        **_unused: object,
    ) -> RepairActionPlan:
        """Freeze one exact repair-planning request without enqueueing it."""

        if action != _ACTION:
            raise ValueError("RepairNodeActions supports only request_repair")
        request_id = _request_id(request_id)
        identity = _observation_identity(observation)
        parent = self._parent_binding(identity)
        policy_row = self.store.get_policy(identity["task_id"])
        policy = _authorized_policy(policy_row)
        trigger = _repeated_repair_trigger(observation)
        if trigger is None:
            if identity["category"] != "tooling_bug" or not _explicit_tooling_evidence(observation):
                raise StateConflictError("repair requires explicit tooling_bug evidence")
        else:
            self._validate_repeated_repair_trigger(trigger, identity, policy_row)
        repository, scopes, base_sha = _repair_repository(policy)
        refs = _evidence_refs(observation.get("evidence_refs"))
        stable = {
            "parent_task_id": identity["task_id"],
            "parent_node_id": identity["node_id"],
            "parent_node_attempt": identity["node_attempt"],
            "failure_fingerprint": identity["failure_fingerprint"],
        }
        repair_task_id = "node-repair-" + canonical_hash(stable)[:24]
        repair_request_id = "node-repair-request-" + canonical_hash(stable)[:24]
        repair_fingerprint = canonical_hash(
            {
                **stable,
                "repository": repository,
                "base_sha": base_sha,
                "allowed_scopes": scopes,
            }
        )
        plan: RepairActionPlan = {
            "action": _ACTION,
            "request_id": request_id,
            "parent_task_id": identity["task_id"],
            "parent_node_id": identity["node_id"],
            "parent_node_attempt": identity["node_attempt"],
            "parent_task_revision": identity["task_revision"],
            "failure_fingerprint": identity["failure_fingerprint"],
            "category": identity["category"],
            "stage_key": _ACTION,
            "repair_task_id": repair_task_id,
            "repair_request_id": repair_request_id,
            "repair_fingerprint": repair_fingerprint,
            "repository": repository,
            "base_sha": base_sha,
            "allowed_scopes": scopes,
            "evidence_refs": refs,
            "policy_revision": int(policy_row["policy_revision"]),
            "policy": _policy_binding(policy, repository=repository, scopes=scopes),
            "claude_allowed": parent["claude_allowed"],
            "arguments": {},
            "action_fingerprint": "",
            **({"repair_trigger": trigger} if trigger is not None else {}),
        }
        plan["arguments"] = _arguments(plan)
        plan["action_fingerprint"] = canonical_hash({**plan, "action_fingerprint": ""})
        return plan

    def execute(self, plan: Mapping[str, Any]) -> RepairActionReceipt:
        """Enqueue the exact repair request once, or return its existing receipt."""

        normalized = self._validated_plan(plan)
        existing = self._authority_receipt(normalized["request_id"])
        if existing is not None:
            return self._receipt(normalized, existing)
        planning = self._planning_or_task_receipt(normalized)
        if planning is not None:
            return planning
        try:
            self._assert_plan_current(normalized)
        except (StateConflictError, ValueError):
            return {
                "action": _ACTION,
                "request_id": normalized["request_id"],
                "journal_status": "completed",
                "known_effects": True,
                "receipt": {
                    "ok": False,
                    "known_effects": True,
                    "stage_succeeded": False,
                    "reason_kind": "repair_admission_rejected",
                    "observation_patch": {
                        "repair_enqueue_rejected": True,
                        "repair_requested": False,
                        "repair_linked": False,
                    },
                },
            }
        receipt = self.authority_service.dispatch(
            {
                "request_id": normalized["request_id"],
                "tool": _TOOL,
                "task_id": normalized["repair_task_id"],
                "arguments": normalized["arguments"],
            }
        )
        return self._receipt(normalized, receipt)

    def reconcile(self, plan: Mapping[str, Any]) -> RepairActionReceipt | None:
        """Read only the original Authority or planning key; never enqueue anew."""

        normalized = self._validated_plan(plan)
        existing = self._authority_receipt(normalized["request_id"])
        if existing is not None:
            return self._receipt(normalized, existing)
        return self._planning_or_task_receipt(normalized)

    def _parent_binding(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        task = self.store.base_store.get_task(identity["task_id"])
        if task.get("state") in {"paused", "cancelled"}:
            raise StateConflictError("repair parent task is paused or cancelled")
        if task.get("state") != "blocked":
            raise StateConflictError("repair requires a blocked parent task")
        with self.store.base_store.connection() as connection:
            pending_approval = connection.execute(
                "SELECT 1 FROM approvals WHERE task_id = ? AND decision IS NULL LIMIT 1",
                (identity["task_id"],),
            ).fetchone()
        if pending_approval is not None:
            raise StateConflictError("repair parent task has a pending approval")
        if task.get("state_revision") != identity["task_revision"]:
            raise StateConflictError("repair parent task revision changed after observation")
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            raise StateConflictError("repair parent task nodes are invalid")
        node = next(
            (item for item in nodes if isinstance(item, dict) and item.get("node_id") == identity["node_id"]),
            None,
        )
        if not isinstance(node, dict) or node.get("state") != "blocked":
            raise StateConflictError("repair requires a blocked parent node")
        if node.get("attempt") != identity["node_attempt"]:
            raise StateConflictError("repair parent node attempt changed after observation")
        contract = task.get("contract")
        if not isinstance(contract, dict) or not isinstance(contract.get("claude_allowed"), bool):
            raise StateConflictError("repair parent contract is invalid")
        return {"claude_allowed": contract["claude_allowed"]}

    def _validate_repeated_repair_trigger(
        self,
        trigger: Mapping[str, str],
        identity: Mapping[str, Any],
        policy_row: Mapping[str, Any],
        *,
        expected_policy_revision: int | None = None,
    ) -> None:
        """Verify a repeat trigger against the durable episode and action ledger."""

        if identity["category"] not in _REPEATED_REPAIR_CATEGORIES:
            raise StateConflictError("repeated repair category is not authorized")
        policy = _authorized_policy(policy_row)
        policy_revision = _policy_revision(policy_row)
        if expected_policy_revision is not None and policy_revision != expected_policy_revision:
            raise StateConflictError("repair policy revision changed after preparation")
        try:
            episode = self.store.get_episode(trigger["episode_id"])
        except KeyError as error:
            raise StateConflictError("repeated repair trigger episode is unavailable") from error
        if (
            episode.get("task_id") != identity["task_id"]
            or episode.get("node_id") != identity["node_id"]
            or episode.get("node_attempt") != identity["node_attempt"]
            or episode.get("failure_fingerprint") != identity["failure_fingerprint"]
            or episode.get("task_revision") != identity["task_revision"]
        ):
            raise StateConflictError("repeated repair trigger does not match the parent episode")
        if (
            episode.get("policy_revision") != policy_revision
            or episode.get("policy") != policy_row.get("policy")
        ):
            raise StateConflictError("repeated repair trigger policy does not match the parent episode")
        if episode.get("category") != identity["category"]:
            raise StateConflictError("repeated repair trigger category does not match the parent episode")
        if episode.get("state") in {"resolved", "suspended"}:
            raise StateConflictError("repeated repair trigger episode is no longer actionable")
        observed = episode.get("observation")
        decision = episode.get("decision")
        if not isinstance(observed, Mapping) or not isinstance(decision, Mapping):
            raise StateConflictError("repeated repair trigger episode is invalid")
        if (
            observed.get("approval_denied") is True
            or decision.get("requires_authorization") is True
            or decision.get("reason_kind") in {"approval_denied", "authorization_required", "unknown_effects", "user_pause"}
        ):
            raise StateConflictError("repeated repair trigger requires user intervention")
        deadline = _deadline(episode.get("time_budget_deadline_at"))
        if deadline <= datetime.fromisoformat(now_iso()):
            raise StateConflictError("repeated repair trigger time budget is exhausted")
        stage_attempts = episode.get("stage_attempts")
        if not isinstance(stage_attempts, Mapping):
            raise StateConflictError("repeated repair trigger stage attempts are invalid")
        attempts = stage_attempts.get(trigger["stage_key"])
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < _max_action_attempts(policy)
        ):
            raise StateConflictError("repeated repair trigger stage is not exhausted")
        actions = episode.get("actions")
        if not isinstance(actions, list):
            raise StateConflictError("repeated repair trigger action ledger is invalid")
        if any(
            not isinstance(action, Mapping)
            or (
                action.get("state") != "completed"
                and str(action.get("stage_key", "")).split(":", 1)[0] != _ACTION
            )
            for action in actions
        ):
            raise StateConflictError("repeated repair trigger has unresolved action effects")
        relevant = [
            action for action in actions
            if isinstance(action, Mapping) and action.get("stage_key") == trigger["stage_key"]
        ]
        if len(relevant) < attempts:
            raise StateConflictError("repeated repair trigger has incomplete stage action receipts")
        for action in relevant:
            receipt = action.get("receipt")
            if (
                action.get("state") != "completed"
                or not isinstance(receipt, Mapping)
                or receipt.get("known_effects") is not True
                or receipt.get("stage_succeeded") is not False
            ):
                raise StateConflictError("repeated repair trigger stage effects are not known failed receipts")

    def _assert_plan_current(self, plan: RepairActionPlan) -> None:
        identity = {
            "task_id": plan["parent_task_id"],
            "node_id": plan["parent_node_id"],
            "node_attempt": plan["parent_node_attempt"],
            "task_revision": plan["parent_task_revision"],
            "failure_fingerprint": plan["failure_fingerprint"],
            "category": plan["category"],
        }
        self._parent_binding(identity)
        policy_row = self.store.get_policy(plan["parent_task_id"])
        policy = _authorized_policy(policy_row)
        trigger = _plan_repair_trigger(plan)
        if trigger is not None:
            self._validate_repeated_repair_trigger(
                trigger,
                identity,
                policy_row,
                expected_policy_revision=plan["policy_revision"],
            )
        repository, scopes, base_sha = _repair_repository(policy)
        if (
            _policy_revision(policy_row) != plan["policy_revision"]
            or _policy_binding(policy, repository=repository, scopes=scopes) != plan["policy"]
            or (repository, scopes, base_sha) != (plan["repository"], plan["allowed_scopes"], plan["base_sha"])
        ):
            raise StateConflictError("repair policy or repository binding changed after preparation")

    def _authority_receipt(self, request_id: str) -> dict[str, Any] | None:
        try:
            return self.authority_service.get_request(request_id)
        except KeyError:
            return None

    def _planning_or_task_receipt(self, plan: RepairActionPlan) -> RepairActionReceipt | None:
        try:
            request = self.store.base_store.get_planning_request(plan["repair_request_id"])
        except KeyError:
            request = None
        if request is not None:
            if request.get("task_id") != plan["repair_task_id"]:
                raise StateConflictError("repair planning request belongs to a different task")
            document = request.get("request")
            if not isinstance(document, Mapping) or (
                document.get("repository") != plan["repository"]
                or document.get("base_sha") != plan["base_sha"]
                or document.get("allowed_scope") != plan["allowed_scopes"]
            ):
                raise StateConflictError("repair planning request identity conflicts with the prepared request")
            return _known_receipt(plan, request, source="planning_request")
        try:
            task = self.store.base_store.get_task(plan["repair_task_id"])
        except KeyError:
            return None
        contract = task.get("contract")
        if not isinstance(contract, dict) or (
            contract.get("repository") != plan["repository"]
            or contract.get("base_sha") != plan["base_sha"]
            or contract.get("allowed_scope") != plan["allowed_scopes"]
        ):
            raise StateConflictError("repair task identity conflicts with the prepared request")
        return _known_receipt(plan, {"status": "materialized", "task_id": plan["repair_task_id"]}, source="task")

    def _receipt(self, plan: RepairActionPlan, authority_receipt: Mapping[str, Any]) -> RepairActionReceipt:
        state = authority_receipt.get("state")
        if state not in {"completed", "executing", "unknown"}:
            raise StateConflictError("Authority journal returned an invalid repair state")
        if state != "completed":
            return {
                "action": _ACTION,
                "request_id": plan["request_id"],
                "journal_status": state,
                "known_effects": False,
                "receipt": None,
            }
        result = _business_receipt(authority_receipt.get("result"))
        if result.get("ok") is not True:
            return {
                "action": _ACTION,
                "request_id": plan["request_id"],
                "journal_status": "completed",
                "known_effects": True,
                "receipt": {"ok": False, "known_effects": True, "stage_succeeded": False,
                            "error": _bounded_error(result)},
            }
        return _known_receipt(plan, result, source="authority_request")

    def _validated_plan(self, raw: Mapping[str, Any]) -> RepairActionPlan:
        if not isinstance(raw, Mapping):
            raise ValueError("repair action plan must be an object")
        required = {
            "action", "request_id", "parent_task_id", "parent_node_id", "parent_node_attempt",
            "parent_task_revision", "failure_fingerprint", "category", "stage_key", "repair_task_id",
            "repair_request_id", "repair_fingerprint", "repository", "base_sha",
            "allowed_scopes", "evidence_refs", "policy_revision", "policy", "arguments",
            "claude_allowed", "action_fingerprint",
        }
        trigger = _plan_repair_trigger(raw)
        if trigger is not None:
            required.add("repair_trigger")
        if set(raw) != required:
            raise ValueError("repair action plan has unsupported or missing fields")
        if raw["action"] != _ACTION:
            raise ValueError("repair action plan is not request_repair")
        request_id = _request_id(raw["request_id"])
        identity = _identity_from_plan(raw)
        category = _identifier(raw["category"], "repair action category")
        repair_task_id = _identifier(raw["repair_task_id"], "repair_task_id")
        repair_request_id = _identifier(raw["repair_request_id"], "repair_request_id")
        repair_fingerprint = _fingerprint(raw["repair_fingerprint"], "repair_fingerprint")
        repository = _plan_repository(raw["repository"])
        base_sha = _sha(raw["base_sha"], "base_sha")
        scopes = _plan_scopes(raw["allowed_scopes"])
        refs = _evidence_refs(raw["evidence_refs"])
        stable = {
            "parent_task_id": identity["parent_task_id"],
            "parent_node_id": identity["parent_node_id"],
            "parent_node_attempt": identity["parent_node_attempt"],
            "failure_fingerprint": identity["failure_fingerprint"],
        }
        expected_task_id = "node-repair-" + canonical_hash(stable)[:24]
        expected_request_id = "node-repair-request-" + canonical_hash(stable)[:24]
        expected_deployment = canonical_hash(
            {**stable, "repository": repository, "base_sha": base_sha, "allowed_scopes": scopes}
        )
        if (
            repair_task_id != expected_task_id
            or repair_request_id != expected_request_id
            or repair_fingerprint != expected_deployment
        ):
            raise ValueError("repair action plan stable identity is invalid")
        policy_revision = _policy_revision(raw)
        if not isinstance(raw["policy"], dict) or not isinstance(raw["arguments"], dict):
            raise ValueError("repair action plan policy and arguments must be objects")
        if not isinstance(raw["claude_allowed"], bool):
            raise ValueError("repair action plan claude_allowed must be a boolean")
        fingerprint = _fingerprint(raw["action_fingerprint"], "action_fingerprint")
        plan: RepairActionPlan = {
            "action": _ACTION,
            "request_id": request_id,
            **identity,
            "category": category,
            "stage_key": _identifier(raw["stage_key"], "stage_key"),
            "repair_task_id": repair_task_id,
            "repair_request_id": repair_request_id,
            "repair_fingerprint": repair_fingerprint,
            "repository": repository,
            "base_sha": base_sha,
            "allowed_scopes": scopes,
            "evidence_refs": refs,
            "policy_revision": policy_revision,
            "policy": dict(raw["policy"]),
            "claude_allowed": raw["claude_allowed"],
            "arguments": dict(raw["arguments"]),
            "action_fingerprint": fingerprint,
            **({"repair_trigger": trigger} if trigger is not None else {}),
        }
        if trigger is None:
            if category != "tooling_bug":
                raise ValueError("repair action plan is not tooling repair")
        elif category not in _REPEATED_REPAIR_CATEGORIES:
            raise ValueError("repair action plan repeated category is not authorized")
        if plan["stage_key"] != _ACTION:
            raise ValueError("repair action plan stage_key must be request_repair")
        expected_policy = {
            "enabled": True,
            "allowed_actions": [_ACTION],
            "repair_repository": repository,
            "repair_allowed_scopes": scopes,
        }
        if plan["policy"] != expected_policy:
            raise ValueError("repair action plan policy binding is invalid")
        expected = _arguments(plan)
        if canonical_json(plan["arguments"]) != canonical_json(expected):
            raise ValueError("repair action arguments are not bound to the fixed plan")
        if canonical_hash({**plan, "action_fingerprint": ""}) != fingerprint:
            raise ValueError("repair action plan fingerprint is invalid")
        return plan


def _identity_from_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parent_task_id": _identifier(raw["parent_task_id"], "parent_task_id"),
        "parent_node_id": _identifier(raw["parent_node_id"], "parent_node_id"),
        "parent_node_attempt": _nonnegative(raw["parent_node_attempt"], "parent_node_attempt"),
        "parent_task_revision": _positive(raw["parent_task_revision"], "parent_task_revision"),
        "failure_fingerprint": _fingerprint(raw["failure_fingerprint"], "failure_fingerprint"),
    }


def _observation_identity(observation: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise ValueError("repair observation must be an object")
    return {
        "task_id": _identifier(observation.get("task_id"), "observation.task_id"),
        "node_id": _identifier(observation.get("node_id"), "observation.node_id"),
        "node_attempt": _nonnegative(
            observation.get("node_attempt", observation.get("attempt")), "observation.node_attempt"
        ),
        "task_revision": _positive(observation.get("task_revision"), "observation.task_revision"),
        "failure_fingerprint": _fingerprint(
            observation.get("failure_fingerprint"), "observation.failure_fingerprint"
        ),
        "category": _identifier(observation.get("category"), "observation.category"),
    }


def _repeated_repair_trigger(observation: Mapping[str, Any]) -> dict[str, str] | None:
    if "repeated_recovery_failure" not in observation:
        return None
    return _repair_trigger(observation["repeated_recovery_failure"])


def _plan_repair_trigger(plan: Mapping[str, Any]) -> dict[str, str] | None:
    if "repair_trigger" not in plan:
        return None
    return _repair_trigger(plan["repair_trigger"])


def _repair_trigger(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != _REPAIR_TRIGGER_FIELDS:
        raise StateConflictError("repeated repair trigger is invalid")
    trigger = {
        "episode_id": _identifier(raw["episode_id"], "repeated repair episode_id"),
        "stage_key": _identifier(raw["stage_key"], "repeated repair stage_key"),
    }
    if trigger["stage_key"].split(":", 1)[0] == _ACTION:
        raise StateConflictError("repeated repair trigger must name an automatic recovery stage")
    return trigger


def _policy_revision(policy_row: Mapping[str, Any]) -> int:
    value = policy_row.get("policy_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StateConflictError("repair policy revision is invalid")
    return value


def _max_action_attempts(policy: Mapping[str, object]) -> int:
    value = policy.get("max_action_attempts")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise StateConflictError("repair policy action limit is invalid")
    return value


def _deadline(value: object) -> datetime:
    if not isinstance(value, str):
        raise StateConflictError("repeated repair trigger deadline is invalid")
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError as error:
        raise StateConflictError("repeated repair trigger deadline is invalid") from error
    if deadline.tzinfo is None:
        raise StateConflictError("repeated repair trigger deadline is invalid")
    return deadline


def _explicit_tooling_evidence(observation: Mapping[str, Any]) -> bool:
    origin = observation.get("origin")
    code = observation.get("code", observation.get("failure_code"))
    explicit = {"tooling_bug", "tooling-bug", "harness-bug", "runner-bug"}
    return origin in explicit or code in explicit


def _authorized_policy(policy_row: Mapping[str, Any]) -> dict[str, object]:
    policy = policy_row.get("policy")
    if not isinstance(policy, dict):
        raise StateConflictError("repair policy is invalid")
    if policy.get("enabled") is not True:
        raise StateConflictError("repair policy is disabled")
    actions = policy.get("allowed_actions")
    if not isinstance(actions, list) or _ACTION not in actions:
        raise StateConflictError("repair is not authorized by policy")
    repository = policy.get("repair_repository")
    scopes = policy.get("repair_allowed_scopes")
    if not isinstance(repository, str) or not isinstance(scopes, list) or not scopes:
        raise StateConflictError("repair policy has no repository and owned scope")
    return dict(policy)


def _policy_binding(
    policy: Mapping[str, object],
    *,
    repository: str | None = None,
    scopes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "enabled": True,
        "allowed_actions": [_ACTION],
        "repair_repository": repository if repository is not None else policy["repair_repository"],
        "repair_allowed_scopes": scopes if scopes is not None else list(policy["repair_allowed_scopes"]),
    }


def _repair_repository(policy: Mapping[str, object]) -> tuple[str, list[str], str]:
    repository = _absolute_repository(policy.get("repair_repository"))
    scopes = _scopes(repository, policy.get("repair_allowed_scopes"))
    try:
        base_sha = subprocess.run(
            ["/usr/bin/git", "-C", repository, "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StateConflictError("repair repository HEAD could not be read") from error
    if base_sha.returncode != 0:
        raise StateConflictError("repair repository must be a Git worktree with HEAD")
    return repository, scopes, _sha(base_sha.stdout.strip(), "repair repository HEAD")


def _absolute_repository(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("repair_repository must be an absolute path")
    raw = Path(value)
    if not raw.is_absolute():
        raise ValueError("repair_repository must be an absolute path")
    try:
        repository = raw.resolve(strict=True)
    except OSError as error:
        raise StateConflictError("repair_repository is unavailable") from error
    if repository == Path("/") or not repository.is_dir() or repository.is_symlink():
        raise StateConflictError("repair_repository is not a bounded directory")
    return str(repository)


def _plan_repository(value: object) -> str:
    """Validate an immutable plan path without consulting its current filesystem state."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("repair plan repository is invalid")
    repository = Path(value)
    if not repository.is_absolute() or repository == Path("/"):
        raise ValueError("repair plan repository is invalid")
    return value


def _scopes(repository: str, value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("repair_allowed_scopes must be a non-empty array")
    root = Path(repository)
    scopes: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
            raise ValueError("repair allowed scope is invalid")
        parsed = PurePosixPath(raw)
        if parsed.is_absolute() or raw in {".", ""} or ".." in parsed.parts or ".git" in parsed.parts:
            raise ValueError("repair allowed scope escapes or covers the Git worktree")
        candidate = root.joinpath(*parsed.parts)
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise ValueError("repair allowed scope escapes the repository") from error
        cursor = root
        for part in parsed.parts:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise ValueError("repair allowed scope traverses a symlink")
        normalized = str(parsed)
        if normalized not in scopes:
            scopes.append(normalized)
    return scopes


def _plan_scopes(value: object) -> list[str]:
    """Validate immutable owned scopes without checking current symlinks."""

    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("repair plan allowed scopes are invalid")
    scopes: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
            raise ValueError("repair plan allowed scope is invalid")
        parsed = PurePosixPath(raw)
        if parsed.is_absolute() or raw in {".", ""} or ".." in parsed.parts or ".git" in parsed.parts:
            raise ValueError("repair plan allowed scope is invalid")
        normalized = str(parsed)
        if normalized in scopes:
            raise ValueError("repair plan allowed scopes are invalid")
        scopes.append(normalized)
    return scopes


def _evidence_refs(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise StateConflictError("repair requires bounded evidence references")
    result: dict[str, str] = {}
    for key, ref in value.items():
        if len(result) >= _MAX_REFS:
            break
        if not isinstance(key, str) or not key or not isinstance(ref, str):
            continue
        if re.fullmatch(r"sha256:[0-9a-f]{64}:[A-Za-z0-9][A-Za-z0-9._-]{0,31}", ref) is None:
            continue
        result[key[:80]] = ref
    if not result:
        raise StateConflictError("repair requires a current content-addressed evidence reference")
    return result


def _objective(identity: Mapping[str, Any], refs: Mapping[str, str], scopes: list[str]) -> str:
    evidence = ", ".join(f"{key}={value}" for key, value in sorted(refs.items()))
    trigger = identity.get("repair_trigger")
    if isinstance(trigger, Mapping):
        return (
            "Diagnose and repair one persistent Workbench recovery defect only. "
            f"Parent task={identity['task_id']}; node={identity['node_id']}; "
            f"attempt={identity['node_attempt']}; category={identity['category']}; "
            f"repeated stage={trigger['stage_key']}. "
            f"Owned paths={', '.join(scopes)}. Evidence refs={evidence}. "
            "Fix only the evidenced recovery defect, run focused tests, preserve the parent objective, "
            "and do not deploy, publish, or broaden scopes."
        )
    return (
        "Repair one persistent Workbench tooling defect only. "
        f"Parent task={identity['task_id']}; node={identity['node_id']}; "
        f"attempt={identity['node_attempt']}; category=tooling_bug. "
        f"Owned paths={', '.join(scopes)}. Evidence refs={evidence}. "
        "Fix only the evidenced tooling defect, run focused tests, preserve the parent objective, "
        "and do not deploy, publish, or broaden scopes."
    )


def _arguments(plan: RepairActionPlan) -> dict[str, object]:
    return {
        "objective": _objective(
            {
                "task_id": plan["parent_task_id"],
                "node_id": plan["parent_node_id"],
                "node_attempt": plan["parent_node_attempt"],
                "category": plan["category"],
                **({"repair_trigger": plan["repair_trigger"]} if "repair_trigger" in plan else {}),
            },
            plan["evidence_refs"],
            plan["allowed_scopes"],
        ),
        "repository": plan["repository"],
        "allowed_scopes": plan["allowed_scopes"],
        "task_id": plan["repair_task_id"],
        "command_id": plan["repair_request_id"],
        "base_sha": plan["base_sha"],
        "task_type": "debugging",
        "complexity": "low",
        "claude_allowed": plan["claude_allowed"],
        "timeout_seconds": 900,
        "retry_limit": 1,
        "external_write_permission": False,
        "queue": True,
    }


def _known_receipt(
    plan: RepairActionPlan,
    source_receipt: Mapping[str, Any],
    *,
    source: str,
) -> RepairActionReceipt:
    receipt = {
        "ok": True,
        "known_effects": True,
        "stage_succeeded": True,
        "repair_task_id": plan["repair_task_id"],
        "repair_request_id": plan["repair_request_id"],
        "repair_fingerprint": plan["repair_fingerprint"],
        "planning_status": source_receipt.get("status"),
        "receipt_source": source,
        "observation_patch": {
            "repair_requested": True,
            "repair_linked": True,
            "repair_task_id": plan["repair_task_id"],
            "repair_request_id": plan["repair_request_id"],
        },
    }
    return {
        "action": _ACTION,
        "request_id": plan["request_id"],
        "journal_status": "completed",
        "known_effects": True,
        "receipt": receipt,
    }


def _business_receipt(raw: object) -> dict[str, object]:
    if isinstance(raw, Mapping) and isinstance(raw.get("content"), list):
        texts = [
            item.get("text")
            for item in raw["content"]
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        if len(texts) == 1:
            try:
                value = json.loads(texts[0])
            except json.JSONDecodeError:
                if raw.get("isError") is True:
                    return {"ok": False, "error": texts[0]}
                raise StateConflictError("repair Authority result is not a JSON receipt")
            if isinstance(value, dict):
                return dict(value)
    if isinstance(raw, Mapping):
        return dict(raw)
    raise StateConflictError("repair Authority result is not an object")


def _bounded_error(result: Mapping[str, object]) -> str:
    value = result.get("error", result.get("message", "repair planning request failed"))
    return str(value)[:_MAX_TEXT]


def _request_id(value: object) -> str:
    return _identifier(value, "request_id", maximum=200)


def _identifier(value: object, label: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise StateConflictError(f"{label} is invalid")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value
