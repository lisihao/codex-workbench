"""Task-scoped activation and compact recovery status on the existing MCP API."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any

from .config import WorkbenchConfig
from .model import canonical_hash
from .node_recovery_policy import RecoveryPolicy
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError, WorkbenchStore

INSTALLED_RECOVERY_ACTIONS = (
    "observe_readiness", "materialize_dependencies", "resume_node",
    "narrow_validation", "source_only_recovery", "request_repair", "repair_source",
    "resume_owner_repairs",
)

RECOVERY_TOOLS = [
    {
        "name": "workbench_configure_node_recovery",
        "description": "Explicitly enable or disable bounded automatic recovery for one task. Does not grant publication, deployment, authentication or arbitrary shell authority.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["task_id", "expected_revision", "policy"],
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 1},
                "policy": {"type": "object"},
            },
        },
    },
    {
        "name": "workbench_get_node_recovery",
        "description": "Read one task's recovery policy, structured blockers, original-node progress, deduplicated actions and repair deployment waits.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "workbench_resume_owner_repairs",
        "description": "Resume exhausted accepted-owner preparation after an explicitly identified Workbench deployment is installed and the current Authority is active. The task policy must already authorize resume_owner_repairs.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": [
                "task_id", "node_id", "expected_revision", "expected_attempt",
                "expected_version", "expected_commit", "expected_tag",
                "confirm_deployment",
            ],
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "node_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 1},
                "expected_attempt": {"type": "integer", "minimum": 1},
                "expected_version": {"type": "string", "minLength": 1},
                "expected_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "expected_tag": {"type": "string", "minLength": 1},
                "confirm_deployment": {"const": True},
            },
        },
    },
]


def recovery_tool(store: WorkbenchStore, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run only the exact task policy or status operation after MCP authentication."""
    fields = {"task_id"}
    if name == "workbench_configure_node_recovery":
        fields |= {"expected_revision", "policy"}
    elif name != "workbench_get_node_recovery":
        raise ValueError("unsupported recovery operation")
    accepted_fields = fields | ({"request_id"} if name == "workbench_configure_node_recovery" else set())
    if frozenset(arguments) not in {frozenset(fields), frozenset(accepted_fields)}:
        raise ValueError("recovery operation requires its exact declared fields")
    task_id = arguments["task_id"]
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    recovery = NodeRecoveryStore(store)
    if name == "workbench_configure_node_recovery":
        policy = RecoveryPolicy.from_dict(arguments["policy"])
        unsupported = set(policy.allowed_actions) - set(INSTALLED_RECOVERY_ACTIONS)
        if policy.enabled and unsupported:
            raise ValueError(f"this build has no installed recovery adapter for: {', '.join(sorted(unsupported))}")
        recovery.configure_policy(
            task_id, policy,
            expected_task_revision=arguments["expected_revision"],
            actor="authenticated-task-policy",
        )
    return {
        "ok": True, **recovery.get_policy(task_id),
        "available_actions": list(INSTALLED_RECOVERY_ACTIONS),
        "episodes": recovery.list_summary(task_id=task_id),
        "metrics": recovery.metrics(task_id=task_id),
    }


def resume_owner_repairs_tool(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Resume exhausted owners from exact installed deployment and live-Authority evidence."""

    fields = {
        "task_id", "node_id", "expected_revision", "expected_attempt",
        "expected_version", "expected_commit", "expected_tag",
        "confirm_deployment", "request_id",
    }
    if set(arguments) != fields:
        raise ValueError("owner repair resume requires its exact declared fields")
    task_id = _text(arguments["task_id"], "task_id")
    node_id = _text(arguments["node_id"], "node_id")
    request_id = _text(arguments["request_id"], "request_id")
    version = _text(arguments["expected_version"], "expected_version")
    commit = _text(arguments["expected_commit"], "expected_commit")
    tag = _text(arguments["expected_tag"], "expected_tag")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("expected_commit must be a lowercase Git SHA-1")
    if arguments["confirm_deployment"] is not True:
        raise ValueError("confirm_deployment must be true")
    expected_revision = _positive_int(arguments["expected_revision"], "expected_revision")
    expected_attempt = _positive_int(arguments["expected_attempt"], "expected_attempt")
    policy = NodeRecoveryStore(store).get_policy(task_id).get("policy")
    if (
        not isinstance(policy, dict)
        or policy.get("enabled") is not True
        or "resume_owner_repairs" not in policy.get("allowed_actions", [])
    ):
        raise StateConflictError("owner repair resume is not authorized by task policy")
    candidate = store.exhausted_blocked_owner_repair_candidate(
        task_id, node_id, expected_attempt,
    )
    if candidate["task_revision"] != expected_revision:
        raise StateConflictError("owner repair resume task revision changed")
    try:
        manifest_bytes = config.install_manifest.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateConflictError("installed Workbench manifest is unavailable") from error
    if not isinstance(manifest, dict) or {
        "version": manifest.get("version"),
        "commit": manifest.get("commit"),
        "tag": manifest.get("tag"),
    } != {"version": version, "commit": commit, "tag": tag}:
        raise StateConflictError("installed Workbench identity does not match the request")
    installed_at = _timestamp(manifest.get("installed_at"), "manifest installed_at")
    authority = store.authority_status()
    if not isinstance(authority, dict) or authority.get("active") is not True:
        raise StateConflictError("current Workbench Authority is not active")
    authority_started_at = _timestamp(
        authority.get("started_at"), "Authority started_at"
    )
    if authority_started_at < installed_at:
        raise StateConflictError("current Authority predates the installed deployment")
    with store.connection() as connection:
        failures = [
            connection.execute(
                "SELECT event_type, task_id, created_at FROM events WHERE cursor = ?",
                (cursor,),
            ).fetchone()
            for cursor in candidate["failure_event_cursors"].values()
        ]
    if not failures or any(
        failure is None
        or failure["event_type"] != "node.accepted_source_repair_rolled_back"
        or failure["task_id"] != task_id
        or _timestamp(failure["created_at"], "owner failure created_at") >= installed_at
        for failure in failures
    ):
        raise StateConflictError("installed deployment does not follow the exhausted owner failures")
    manifest_sha256 = sha256(manifest_bytes).hexdigest()
    repair_fingerprint = canonical_hash({
        "kind": "verified-external-workbench-deployment/v1",
        "task_id": task_id,
        "node_id": node_id,
        "failure_event_cursors": candidate["failure_event_cursors"],
        "version": version,
        "commit": commit,
        "tag": tag,
        "installed_at": manifest["installed_at"],
        "install_manifest_sha256": manifest_sha256,
    })
    readiness_fingerprint = canonical_hash({
        "kind": "active-authority-readiness/v1",
        "install_manifest_sha256": manifest_sha256,
        "authority_instance_id": authority.get("instance_id"),
        "authority_epoch": authority.get("authority_epoch"),
        "authority_started_at": authority.get("started_at"),
    })
    action_fingerprint = canonical_hash({
        "kind": "authenticated-owner-repair-resume/v1",
        "request_id": request_id,
        "task_id": task_id,
        "node_id": node_id,
        "expected_attempt": expected_attempt,
        "expected_revision": expected_revision,
        "expected_event_cursor": candidate["event_cursor"],
        "expected_progress_fingerprint": candidate["progress_fingerprint"],
        "repair_fingerprint": repair_fingerprint,
        "readiness_fingerprint": readiness_fingerprint,
    })
    receipt = store.resume_exhausted_blocked_owner_repairs(
        request_id=request_id,
        action_fingerprint=action_fingerprint,
        task_id=task_id,
        node_id=node_id,
        expected_attempt=expected_attempt,
        expected_revision=expected_revision,
        expected_event_cursor=candidate["event_cursor"],
        expected_progress_fingerprint=candidate["progress_fingerprint"],
        repair_fingerprint=repair_fingerprint,
        readiness_fingerprint=readiness_fingerprint,
    )
    return {
        "ok": True,
        "deployment": {
            "version": version,
            "commit": commit,
            "tag": tag,
            "install_manifest_sha256": manifest_sha256,
            "authority_epoch": authority.get("authority_epoch"),
        },
        **receipt,
    }


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _timestamp(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateConflictError(f"{name} is invalid") from error
    if parsed.tzinfo is None:
        raise StateConflictError(f"{name} must include a timezone")
    return parsed
