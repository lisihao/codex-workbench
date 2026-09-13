"""Pure durable evidence rules for opt-in blocked-node continuation.

The policy configuration is task-scoped, while each recovery action must still
prove its current task/node boundary and its fresh native preview.  This file
does not read SQLite, invoke adapters, or trust worker-provided risk claims.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping, Sequence


_CONFIG_KEYS = frozenset({
    "same_task_only",
    "allow_across_attempts",
    "allow_across_revisions",
})
_CAPTURE_KEY = "capture"
_CAPTURE_KEYS = frozenset({
    "version",
    "policy_revision",
    "goal",
    "captured_task_revision",
    "captured_nodes",
    "scope",
    "permissions",
    "policy_scope",
    "risk_boundary",
    "rollback_conditions",
})
_GOAL_KEYS = frozenset({"task_id", "objective_sha256"})
_SCOPE_KEYS = frozenset({"repository", "allowed_scope", "forbidden_scope", "node_scopes"})
_PERMISSION_KEYS = frozenset({"external_write_permission", "destructive_action_permission"})
_POLICY_SCOPE_KEYS = frozenset({
    "allowed_actions",
    "validation_profiles",
    "repair_repository",
    "repair_allowed_scopes",
})
_NODE_SCOPE_KEYS = frozenset({"read_scopes", "write_scopes"})
_SAFE_ACTIONS = frozenset({"narrow_validation", "source_only_recovery"})
_SHA256 = frozenset("0123456789abcdef")


class ContinuationAuthorizationError(ValueError):
    """A continuation policy, capture, or current action is not provable."""


def parse_continuation_authorization(raw: object) -> dict[str, bool] | None:
    """Parse the caller-configurable opt-in without accepting captured facts."""

    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != _CONFIG_KEYS:
        raise ContinuationAuthorizationError(
            "continuation_authorization must contain only same_task_only, "
            "allow_across_attempts, and allow_across_revisions"
        )
    result: dict[str, bool] = {}
    for key in sorted(_CONFIG_KEYS):
        value = raw[key]
        if type(value) is not bool:
            raise ContinuationAuthorizationError(f"continuation_authorization.{key} must be a boolean")
        result[key] = value
    if result["same_task_only"] is not True:
        raise ContinuationAuthorizationError("continuation_authorization.same_task_only must be true")
    return result


def split_stored_policy(document: Mapping[str, object]) -> tuple[dict[str, object], dict[str, bool] | None, dict[str, Any] | None]:
    """Separate a persisted Authority capture from public policy configuration."""

    if not isinstance(document, Mapping):
        raise ContinuationAuthorizationError("stored recovery policy must be an object")
    public = dict(document)
    raw = public.get("continuation_authorization")
    if raw is None:
        return public, None, None
    if not isinstance(raw, Mapping):
        raise ContinuationAuthorizationError("stored continuation_authorization must be an object")
    capture = raw.get(_CAPTURE_KEY)
    config_raw = {key: value for key, value in raw.items() if key != _CAPTURE_KEY}
    config = parse_continuation_authorization(config_raw)
    if capture is None:
        raise ContinuationAuthorizationError("continuation authorization capture is absent")
    if not isinstance(capture, Mapping):
        raise ContinuationAuthorizationError("continuation authorization capture must be an object")
    normalized_capture = validate_capture(capture)
    public["continuation_authorization"] = config
    return public, config, normalized_capture


def attach_capture(
    policy: Mapping[str, object],
    capture: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return the stored policy document with Authority-derived capture only."""

    result = dict(policy)
    config = parse_continuation_authorization(result.get("continuation_authorization"))
    if config is None:
        if capture is not None:
            raise ContinuationAuthorizationError("disabled continuation policy cannot retain a capture")
        return result
    if capture is None:
        raise ContinuationAuthorizationError("enabled continuation policy requires an Authority capture")
    result["continuation_authorization"] = {**config, _CAPTURE_KEY: validate_capture(capture)}
    return result


def capture_authorization(
    policy: Mapping[str, object],
    *,
    policy_revision: int,
    task: Mapping[str, object],
) -> dict[str, Any] | None:
    """Capture the actual task/node scope and permission boundary at configuration."""

    config = parse_continuation_authorization(policy.get("continuation_authorization"))
    if config is None:
        return None
    if type(policy_revision) is not int or policy_revision < 1:
        raise ContinuationAuthorizationError("continuation policy revision is invalid")
    facts = _task_facts(task)
    if facts["permissions"] != {
        "external_write_permission": False,
        "destructive_action_permission": False,
    }:
        raise ContinuationAuthorizationError(
            "continuation authorization requires no external or destructive permissions"
        )
    return {
        "version": 1,
        "policy_revision": policy_revision,
        "goal": {
            "task_id": facts["task_id"],
            "objective_sha256": facts["objective_sha256"],
        },
        "captured_task_revision": facts["task_revision"],
        "captured_nodes": facts["node_attempts"],
        "scope": facts["scope"],
        "permissions": facts["permissions"],
        "policy_scope": _policy_scope(policy),
        "risk_boundary": {
            "allowed_actions": sorted(_SAFE_ACTIONS),
            "risk_level": "low",
            "reversible": True,
            "severe_adverse_side_effects": "forbidden",
            "external_effects": "forbidden",
            "destructive_effects": "forbidden",
            "risk_source": "authority_task_node_and_native_preview",
        },
        "rollback_conditions": [
            "fresh_native_preview",
            "known_effects_or_reconcile_only",
            "unchanged_scope_and_permission_boundary",
            "no_external_or_destructive_permission",
        ],
    }


def validate_capture(raw: Mapping[str, object]) -> dict[str, Any]:
    """Strictly reparse one Authority-generated continuation capture."""

    if set(raw) != _CAPTURE_KEYS:
        raise ContinuationAuthorizationError("continuation authorization capture has unsupported fields")
    if raw.get("version") != 1:
        raise ContinuationAuthorizationError("continuation authorization capture version is invalid")
    policy_revision = raw.get("policy_revision")
    task_revision = raw.get("captured_task_revision")
    if (
        type(policy_revision) is not int
        or policy_revision < 1
        or type(task_revision) is not int
        or task_revision < 0
    ):
        raise ContinuationAuthorizationError("continuation authorization capture revision is invalid")
    goal = raw.get("goal")
    if not isinstance(goal, Mapping) or set(goal) != _GOAL_KEYS:
        raise ContinuationAuthorizationError("continuation authorization goal is invalid")
    task_id = _text(goal.get("task_id"), "continuation authorization goal task_id")
    objective = _digest(goal.get("objective_sha256"), "continuation authorization objective")
    captured_nodes = _attempts(raw.get("captured_nodes"), "continuation authorization captured_nodes")
    scope = _scope(raw.get("scope"), "continuation authorization scope")
    permissions = _permissions(raw.get("permissions"), "continuation authorization permissions")
    policy_scope = _policy_scope(raw.get("policy_scope"))
    risk_boundary = raw.get("risk_boundary")
    if not isinstance(risk_boundary, Mapping) or dict(risk_boundary) != {
        "allowed_actions": sorted(_SAFE_ACTIONS),
        "risk_level": "low",
        "reversible": True,
        "severe_adverse_side_effects": "forbidden",
        "external_effects": "forbidden",
        "destructive_effects": "forbidden",
        "risk_source": "authority_task_node_and_native_preview",
    }:
        raise ContinuationAuthorizationError("continuation authorization risk boundary is invalid")
    rollback = raw.get("rollback_conditions")
    if rollback != [
        "fresh_native_preview",
        "known_effects_or_reconcile_only",
        "unchanged_scope_and_permission_boundary",
        "no_external_or_destructive_permission",
    ]:
        raise ContinuationAuthorizationError("continuation authorization rollback conditions are invalid")
    if permissions != {"external_write_permission": False, "destructive_action_permission": False}:
        raise ContinuationAuthorizationError("continuation authorization permission boundary is unsafe")
    return {
        "version": 1,
        "policy_revision": policy_revision,
        "goal": {"task_id": task_id, "objective_sha256": objective},
        "captured_task_revision": task_revision,
        "captured_nodes": captured_nodes,
        "scope": scope,
        "permissions": permissions,
        "policy_scope": policy_scope,
        "risk_boundary": dict(risk_boundary),
        "rollback_conditions": list(rollback),
    }


def decision_authorization(
    policy: Mapping[str, object],
    capture: Mapping[str, object],
    *,
    policy_revision: int,
    task: Mapping[str, object],
    node_id: object,
    action: object,
    approval_pending: bool,
    approval_denied: bool,
    observation_available: bool,
) -> dict[str, Any]:
    """Derive a current action-eligibility record from Authority-owned facts."""

    config = parse_continuation_authorization(policy.get("continuation_authorization"))
    if config is None:
        raise ContinuationAuthorizationError("continuation authorization is not enabled")
    captured = validate_capture(capture)
    facts = _task_facts(task)
    normalized_node_id = _text(node_id, "continuation node_id")
    normalized_action = None if action is None else _text(action, "continuation action")
    result = _binding_base(
        config, captured, policy_revision, facts, normalized_node_id, normalized_action
    )
    reason = _current_reason(
        config,
        captured,
        facts,
        policy,
        policy_revision,
        normalized_node_id,
        normalized_action,
        approval_pending=approval_pending,
        approval_denied=approval_denied,
        observation_available=observation_available,
    )
    if reason is not None:
        return {**result, "authorized": False, "reason_kind": reason}
    return {
        **result,
        "authorized": True,
        "reason_kind": "eligible_pending_fresh_native_preview",
    }


def action_authorization(
    policy: Mapping[str, object],
    capture: Mapping[str, object],
    *,
    policy_revision: int,
    task: Mapping[str, object],
    node_id: object,
    action_plan: Mapping[str, object],
    approval_pending: bool,
    approval_denied: bool,
    observation_available: bool,
) -> dict[str, Any]:
    """Bind one new plan to its current safe scope, risk, and rollback facts."""

    action = _text(action_plan.get("action"), "continuation action plan action")
    context = decision_authorization(
        policy,
        capture,
        policy_revision=policy_revision,
        task=task,
        node_id=node_id,
        action=action,
        approval_pending=approval_pending,
        approval_denied=approval_denied,
        observation_available=observation_available,
    )
    if context["authorized"] is not True:
        return context
    facts = _task_facts(task)
    current_node = _text(node_id, "continuation node_id")
    if _has_release_tag(action_plan):
        return {**context, "authorized": False, "reason_kind": "external_release_scope_forbidden"}
    identity = {
        "task_id": facts["task_id"],
        "node_id": current_node,
        "node_attempt": facts["node_attempts"].get(current_node),
        "task_revision": facts["task_revision"],
    }
    if action_plan.get("task_id") != facts["task_id"]:
        return {**context, "authorized": False, "reason_kind": "goal_changed"}
    if any(action_plan.get(key) != value for key, value in identity.items()):
        return {**context, "authorized": False, "reason_kind": "action_plan_identity_changed"}
    if action == "narrow_validation":
        preview = action_plan.get("fresh_preview")
        profile = action_plan.get("validation_profile")
        arguments = action_plan.get("arguments")
        if (
            not isinstance(preview, Mapping)
            or not isinstance(arguments, Mapping)
            or not isinstance(profile, str)
            or profile not in context["policy_scope"]["validation_profiles"]
            or arguments.get("check_id") != profile
            or arguments.get("dry_run") is not False
            or not isinstance(preview.get("worktree"), str)
            or not preview["worktree"].startswith("/")
        ):
            return {**context, "authorized": False, "reason_kind": "native_preview_scope_invalid"}
        native_preview = {
            "kind": "controlled_validation",
            "validation_profile": profile,
            "fingerprint": _digest(preview.get("fingerprint"), "validation preview fingerprint"),
            "source_delta_sha256": _digest(
                preview.get("source_delta_sha256"), "validation preview source delta"
            ),
            "install_manifest_sha256": _digest(
                preview.get("install_manifest_sha256"), "validation preview install manifest"
            ),
            "runtime_fingerprint": _digest(
                preview.get("runtime_fingerprint"), "validation preview runtime"
            ),
        }
        action_scope = {"validation_profile": profile, "worktree": preview["worktree"]}
    elif action == "source_only_recovery":
        preview = action_plan.get("fresh_preview")
        arguments = action_plan.get("arguments")
        if (
            not isinstance(preview, Mapping)
            or not isinstance(arguments, Mapping)
            or arguments.get("source_only") is not True
            or arguments.get("dry_run") is not False
            or arguments.get("preserve_untracked") is not True
            or arguments.get("confirm_source_only_extraction") is not True
            or arguments.get("confirm_preserve_unknown_ignored") is not True
        ):
            return {**context, "authorized": False, "reason_kind": "native_preview_scope_invalid"}
        native_preview = {
            "kind": "source_only_recovery",
            "source_delta_sha256": _digest(
                preview.get("source_delta_sha256"), "source-only preview source delta"
            ),
            "install_manifest_sha256": _digest(
                preview.get("install_manifest_sha256"), "source-only preview install manifest"
            ),
            "runtime_fingerprint": _digest(
                preview.get("runtime_fingerprint"), "source-only preview runtime"
            ),
        }
        action_scope = {"source_only": True, "preserve_untracked": True}
    else:
        return {**context, "authorized": False, "reason_kind": "unsupported_internal_action"}
    return {
        **context,
        "authorized": True,
        "reason_kind": "fresh_native_preview_authorized",
        "action_scope": action_scope,
        "native_preview": native_preview,
        "risk_rationale": [
            "authority_current_task_and_node",
            "fixed_internal_action",
            "fresh_native_preview",
            "no_external_or_destructive_permission",
        ],
        "rollback_conditions": [
            *context["rollback_conditions"],
            "native_preview_fingerprint_matches_execution",
        ],
    }


def _binding_base(
    config: Mapping[str, bool],
    capture: Mapping[str, Any],
    policy_revision: int,
    facts: Mapping[str, Any],
    node_id: object,
    action: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "configuration": dict(config),
        "goal": dict(capture["goal"]),
        "policy_revision": policy_revision,
        "captured_policy_revision": capture["policy_revision"],
        "action": action,
        "current": {
            "task_revision": facts["task_revision"],
            "contract_hash": facts["contract_hash"],
            "node_id": node_id,
            "node_attempt": facts["node_attempts"].get(node_id),
        },
        "scope": facts["scope"],
        "policy_scope": dict(capture["policy_scope"]),
        "risk_rationale": ["authority_current_task_and_node"],
        "risk_level": "low",
        "reversible": True,
        "severe_adverse_side_effects": "forbidden",
        "rollback_conditions": list(capture["rollback_conditions"]),
        "external_effects": "forbidden",
    }


def _current_reason(
    config: Mapping[str, bool],
    capture: Mapping[str, Any],
    facts: Mapping[str, Any],
    policy: Mapping[str, object],
    policy_revision: int,
    node_id: object,
    action: str | None,
    *,
    approval_pending: bool,
    approval_denied: bool,
    observation_available: bool,
) -> str | None:
    if type(approval_pending) is not bool or type(approval_denied) is not bool:
        raise ContinuationAuthorizationError("continuation approval facts must be booleans")
    if type(observation_available) is not bool:
        raise ContinuationAuthorizationError("continuation observation availability must be a boolean")
    if policy_revision != capture["policy_revision"]:
        return "policy_revision_changed"
    if facts["task_id"] != capture["goal"]["task_id"]:
        return "goal_changed"
    if facts["objective_sha256"] != capture["goal"]["objective_sha256"]:
        return "goal_changed"
    if facts["scope"] != capture["scope"]:
        return "scope_changed"
    if facts["permissions"] != capture["permissions"]:
        return "permission_boundary_changed"
    if _policy_scope(policy) != capture["policy_scope"]:
        return "policy_scope_changed"
    if facts["task_revision"] != capture["captured_task_revision"] and not config["allow_across_revisions"]:
        return "task_revision_changed"
    current_node = _text(node_id, "continuation node_id")
    if current_node not in capture["captured_nodes"] or current_node not in facts["node_attempts"]:
        return "node_outside_authorized_scope"
    if (
        facts["node_attempts"][current_node] != capture["captured_nodes"][current_node]
        and not config["allow_across_attempts"]
    ):
        return "node_attempt_changed"
    if facts["task_state"] in {"paused", "pause", "cancelled", "canceled"}:
        return "user_pause"
    if facts["task_state"] != "blocked" or facts["node_states"].get(current_node) != "blocked":
        return "blocked_state_changed"
    if action is not None and action not in _SAFE_ACTIONS:
        return "unsupported_internal_action"
    if approval_denied:
        return "approval_denied"
    if approval_pending:
        return "approval_pending"
    if not observation_available:
        return "observation_unavailable"
    return None


def _task_facts(task: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        raise ContinuationAuthorizationError("continuation task snapshot is invalid")
    task_id = _text(task.get("task_id"), "continuation task_id")
    task_revision = _integer(task.get("state_revision"), "continuation task revision", minimum=0)
    contract_hash = _digest(task.get("contract_hash"), "continuation contract hash")
    task_state = _text(task.get("state"), "continuation task state").lower()
    contract = task.get("contract")
    if not isinstance(contract, Mapping):
        raise ContinuationAuthorizationError("continuation task contract is invalid")
    objective = _text(contract.get("objective"), "continuation task objective")
    scope = _scope_from_contract(contract)
    permissions = _permissions({
        "external_write_permission": contract.get("external_write_permission"),
        "destructive_action_permission": contract.get("destructive_action_permission"),
    }, "continuation task permissions")
    nodes = task.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise ContinuationAuthorizationError("continuation task nodes are invalid")
    node_attempts, node_scopes, node_states = _node_facts(nodes)
    return {
        "task_id": task_id,
        "task_revision": task_revision,
        "contract_hash": contract_hash,
        "task_state": task_state,
        "objective_sha256": sha256(objective.encode("utf-8")).hexdigest(),
        "scope": {**scope, "node_scopes": node_scopes},
        "permissions": permissions,
        "node_attempts": node_attempts,
        "node_states": node_states,
    }


def _scope_from_contract(contract: Mapping[str, object]) -> dict[str, Any]:
    repository = _text(contract.get("repository"), "continuation repository")
    if not repository.startswith("/"):
        raise ContinuationAuthorizationError("continuation repository must be absolute")
    return {
        "repository": repository,
        "allowed_scope": _strings(contract.get("allowed_scope"), "continuation allowed_scope"),
        "forbidden_scope": _strings(contract.get("forbidden_scope"), "continuation forbidden_scope"),
    }


def _node_facts(
    nodes: Sequence[object],
) -> tuple[dict[str, int], dict[str, dict[str, list[str]]], dict[str, str]]:
    attempts: dict[str, int] = {}
    scopes: dict[str, dict[str, list[str]]] = {}
    states: dict[str, str] = {}
    for raw in nodes:
        if not isinstance(raw, Mapping):
            raise ContinuationAuthorizationError("continuation node record is invalid")
        node_id = _text(raw.get("node_id"), "continuation node_id")
        if node_id in attempts:
            raise ContinuationAuthorizationError("continuation node ids must be unique")
        attempts[node_id] = _integer(raw.get("attempt"), "continuation node attempt", minimum=0)
        states[node_id] = _text(raw.get("state"), "continuation node state").lower()
        spec = raw.get("spec")
        source = spec if isinstance(spec, Mapping) else raw
        scopes[node_id] = {
            "read_scopes": _strings(source.get("read_scopes", []), "continuation node read_scopes"),
            "write_scopes": _strings(source.get("write_scopes", []), "continuation node write_scopes"),
        }
    if not attempts:
        raise ContinuationAuthorizationError("continuation task has no nodes")
    return attempts, scopes, states


def _scope(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _SCOPE_KEYS:
        raise ContinuationAuthorizationError(f"{label} is invalid")
    repository = _text(raw.get("repository"), f"{label}.repository")
    if not repository.startswith("/"):
        raise ContinuationAuthorizationError(f"{label}.repository must be absolute")
    node_scopes = raw.get("node_scopes")
    if not isinstance(node_scopes, Mapping) or not node_scopes:
        raise ContinuationAuthorizationError(f"{label}.node_scopes is invalid")
    normalized_nodes: dict[str, dict[str, list[str]]] = {}
    for node_id, node_scope in node_scopes.items():
        key = _text(node_id, f"{label}.node_scopes key")
        if not isinstance(node_scope, Mapping) or set(node_scope) != _NODE_SCOPE_KEYS:
            raise ContinuationAuthorizationError(f"{label}.node_scopes is invalid")
        normalized_nodes[key] = {
            "read_scopes": _strings(node_scope.get("read_scopes"), f"{label}.read_scopes"),
            "write_scopes": _strings(node_scope.get("write_scopes"), f"{label}.write_scopes"),
        }
    return {
        "repository": repository,
        "allowed_scope": _strings(raw.get("allowed_scope"), f"{label}.allowed_scope"),
        "forbidden_scope": _strings(raw.get("forbidden_scope"), f"{label}.forbidden_scope"),
        "node_scopes": normalized_nodes,
    }


def _permissions(raw: object, label: str) -> dict[str, bool]:
    if not isinstance(raw, Mapping) or set(raw) != _PERMISSION_KEYS:
        raise ContinuationAuthorizationError(f"{label} is invalid")
    result: dict[str, bool] = {}
    for key in sorted(_PERMISSION_KEYS):
        if type(raw.get(key)) is not bool:
            raise ContinuationAuthorizationError(f"{label}.{key} must be a boolean")
        result[key] = raw[key]
    return result


def _policy_scope(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContinuationAuthorizationError("continuation policy scope is invalid")
    if set(raw) == _POLICY_SCOPE_KEYS:
        source = raw
    else:
        source = {
            "allowed_actions": raw.get("allowed_actions"),
            "validation_profiles": raw.get("validation_profiles"),
            "repair_repository": raw.get("repair_repository"),
            "repair_allowed_scopes": raw.get("repair_allowed_scopes"),
        }
    if set(source) != _POLICY_SCOPE_KEYS:
        raise ContinuationAuthorizationError("continuation policy scope is invalid")
    repository = source.get("repair_repository")
    if repository is not None:
        repository = _text(repository, "continuation repair_repository")
        if not repository.startswith("/"):
            raise ContinuationAuthorizationError("continuation repair_repository must be absolute")
    return {
        "allowed_actions": _strings(source.get("allowed_actions"), "continuation allowed_actions"),
        "validation_profiles": _strings(
            source.get("validation_profiles"), "continuation validation_profiles"
        ),
        "repair_repository": repository,
        "repair_allowed_scopes": _strings(
            source.get("repair_allowed_scopes"), "continuation repair_allowed_scopes"
        ),
    }


def _attempts(raw: object, label: str) -> dict[str, int]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContinuationAuthorizationError(f"{label} is invalid")
    result: dict[str, int] = {}
    for node_id, attempt in raw.items():
        key = _text(node_id, label)
        result[key] = _integer(attempt, label, minimum=0)
    return result


def _strings(raw: object, label: str) -> list[str]:
    if not isinstance(raw, (list, tuple)) or isinstance(raw, (str, bytes)):
        raise ContinuationAuthorizationError(f"{label} must be an array")
    values = [_text(value, label) for value in raw]
    if len(set(values)) != len(values):
        raise ContinuationAuthorizationError(f"{label} must not contain duplicates")
    return values


def _integer(raw: object, label: str, *, minimum: int) -> int:
    if type(raw) is not int or raw < minimum:
        raise ContinuationAuthorizationError(f"{label} is invalid")
    return raw


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\x00" in raw:
        raise ContinuationAuthorizationError(f"{label} is invalid")
    return raw


def _digest(raw: object, label: str) -> str:
    value = _text(raw, label)
    if len(value) != 64 or any(character not in _SHA256 for character in value):
        raise ContinuationAuthorizationError(f"{label} must be a SHA-256 digest")
    return value


def _has_release_tag(value: object) -> bool:
    if isinstance(value, Mapping):
        return "release_tag" in value or any(_has_release_tag(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_has_release_tag(item) for item in value)
    return False
