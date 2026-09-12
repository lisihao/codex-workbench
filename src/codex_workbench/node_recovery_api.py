"""Task-scoped activation and compact recovery status on the existing MCP API."""
from __future__ import annotations

from typing import Any

from .node_recovery_policy import RecoveryPolicy
from .node_recovery_store import NodeRecoveryStore
from .store import WorkbenchStore

INSTALLED_RECOVERY_ACTIONS = (
    "observe_readiness", "materialize_dependencies", "resume_node",
    "narrow_validation", "source_only_recovery", "request_repair", "repair_source",
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
]


def recovery_tool(store: WorkbenchStore, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run only the exact task policy or status operation after MCP authentication."""
    fields = {"task_id"}
    if name == "workbench_configure_node_recovery":
        fields |= {"expected_revision", "policy"}
    elif name != "workbench_get_node_recovery":
        raise ValueError("unsupported recovery operation")
    if set(arguments) != fields:
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
