"""Bounded MCP access to the non-executing responsibility ledger.

The adapter validates the JSON request and maps the originating session to the
ledger's owner fields.  It does not create a route, claim a task node, or
invoke any execution capability; those concerns remain owned by the existing
Authority and scheduler surfaces.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .responsibility import ResponsibilityLedger
from .store import WorkbenchStore


TOOL_NAME = "workbench_responsibility"
_OPERATIONS = ("list", "inspect", "open", "propose_handoff", "claim_handoff", "defer")
_OPERATION_SET = frozenset(_OPERATIONS)
_WAIT_KINDS = (
    "resource",
    "dependency",
    "environment",
    "indeterminate",
    "approval",
    "user_pause",
)
_WAIT_REASON_FIELDS = frozenset(
    {
        "wait_kind",
        "detail",
        "release_condition",
        "responsible_owner",
        "next_recheck_at",
        "requires_user_action",
    }
)
_COMMON_FIELDS = frozenset({"op", "task_id", "goal_id"})
_MUTATION_FIELDS = frozenset(
    {
        "op",
        "task_id",
        "goal_id",
        "node_id",
        "attempt",
        "task_revision",
        "source_thread_id",
        "next_action",
        "deadline",
        "request_id",
    }
)


RESPONSIBILITY_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Inspect or record one task-scoped responsibility snapshot. This is an "
        "accountability ledger only: it never creates a route, claims a task "
        "node, changes task state, or starts execution."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["op", "task_id"],
        "properties": {
            "op": {"enum": list(_OPERATIONS)},
            "task_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "goal_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "integer", "minimum": 0},
            "node_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "attempt": {"type": "integer", "minimum": 0},
            "task_revision": {"type": "integer", "minimum": 1},
            "source_thread_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "next_action": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "object", "minProperties": 1},
                ],
            },
            "deadline": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_responsibility_revision": {"type": "integer", "minimum": 1},
            "proposed_owner": {"type": "string", "minLength": 1, "maxLength": 200},
            "wait_reason": {
                "type": "object",
                "additionalProperties": False,
                "required": ["wait_kind", "detail", "release_condition"],
                "properties": {
                    "wait_kind": {"enum": list(_WAIT_KINDS)},
                    "detail": {"type": "string", "minLength": 1},
                    "release_condition": {"type": "string", "minLength": 1},
                    "responsible_owner": {"type": "string", "minLength": 1, "maxLength": 200},
                    "next_recheck_at": {
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "null"},
                        ],
                    },
                    "requires_user_action": {"type": "boolean"},
                },
            },
            "next_recheck_at": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ],
            },
        },
        "oneOf": [
            {"properties": {"op": {"const": "list"}}},
            {"properties": {"op": {"const": "inspect"}}, "required": ["goal_id"]},
            {
                "properties": {"op": {"const": "open"}},
                "required": sorted(_MUTATION_FIELDS),
            },
            {
                "properties": {"op": {"const": "propose_handoff"}},
                "required": sorted(
                    _MUTATION_FIELDS | {"expected_responsibility_revision", "proposed_owner"}
                ),
            },
            {
                "properties": {"op": {"const": "claim_handoff"}},
                "required": sorted(_MUTATION_FIELDS | {"expected_responsibility_revision"}),
            },
            {
                "properties": {"op": {"const": "defer"}},
                "required": sorted(
                    _MUTATION_FIELDS
                    | {"expected_responsibility_revision", "wait_reason", "next_recheck_at"}
                ),
            },
        ],
    },
}

# Match the convention used by one-tool modules and by callers that expose a
# list of MCP definitions.
TOOL = RESPONSIBILITY_TOOL
RESPONSIBILITY_TOOLS = [RESPONSIBILITY_TOOL]


def _json_object(raw: object) -> dict[str, Any]:
    """Copy one request through a strict JSON boundary."""

    if not isinstance(raw, Mapping):
        raise ValueError("responsibility arguments must be an object")
    try:
        encoded = json.dumps(dict(raw), ensure_ascii=False, allow_nan=False, sort_keys=True)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("responsibility arguments must be JSON-safe") from error
    if not isinstance(normalized, dict):
        raise ValueError("responsibility arguments must be an object")
    return normalized


def _text(value: object, label: str, *, maximum: int = 200) -> str:
    """Validate one non-empty, whitespace-delimited identifier field."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{label} must be at most {maximum} characters")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise ValueError(f"{label} must not contain control characters")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    """Validate a JSON integer while rejecting JSON booleans."""

    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _next_action(value: object) -> str | dict[str, Any]:
    """Keep the ledger's string-or-object next-action input JSON-shaped."""

    if isinstance(value, str):
        return _text(value, "next_action")
    if isinstance(value, dict) and value:
        return value
    raise ValueError("next_action must be a non-empty string or object")


def _wait_reason(value: object, *, source_thread_id: str) -> dict[str, Any]:
    """Validate the explicit typed wait while deriving ownership from the source session."""

    if not isinstance(value, dict) or not value:
        raise ValueError("wait_reason must be a non-empty object")
    unexpected = set(value) - _WAIT_REASON_FIELDS
    if unexpected:
        raise ValueError(f"wait_reason has unsupported fields: {sorted(unexpected)}")
    required = {"wait_kind", "detail", "release_condition"}
    missing = required - set(value)
    if missing:
        raise ValueError(f"wait_reason requires: {', '.join(sorted(missing))}")
    if value["wait_kind"] not in _WAIT_KINDS:
        raise ValueError("wait_reason.wait_kind is unsupported")
    for field in ("detail", "release_condition"):
        value[field] = _text(value[field], f"wait_reason.{field}")
    if "responsible_owner" in value:
        owner = value["responsible_owner"]
        if not isinstance(owner, str) or owner != source_thread_id:
            raise ValueError("wait_reason.responsible_owner must match source_thread_id")
    if "next_recheck_at" in value and value["next_recheck_at"] is not None:
        value["next_recheck_at"] = _text(value["next_recheck_at"], "wait_reason.next_recheck_at")
    if "requires_user_action" in value and type(value["requires_user_action"]) is not bool:
        raise ValueError("wait_reason.requires_user_action must be a boolean")
    return value


def _arguments(raw: object) -> dict[str, Any]:
    """Validate operation-specific fields before touching durable state."""

    values = _json_object(raw)
    op = values.get("op")
    if op not in _OPERATION_SET:
        raise ValueError("responsibility op is unsupported")
    if op == "list":
        if set(values) - {"op", "task_id", "limit", "cursor"}:
            raise ValueError("responsibility list has unsupported fields")
        values["task_id"] = _text(values.get("task_id"), "task_id")
        values["limit"] = _integer(values.get("limit", 20), "limit", minimum=1)
        values["cursor"] = _integer(values.get("cursor", 0), "cursor", minimum=0)
        return values
    for field in ("task_id", "goal_id"):
        values[field] = _text(values.get(field), field)

    expected_fields = {
        "inspect": _COMMON_FIELDS,
        "open": _MUTATION_FIELDS,
        "propose_handoff": _MUTATION_FIELDS
        | {"expected_responsibility_revision", "proposed_owner"},
        "claim_handoff": _MUTATION_FIELDS | {"expected_responsibility_revision"},
        "defer": _MUTATION_FIELDS
        | {"expected_responsibility_revision", "wait_reason", "next_recheck_at"},
    }[op]
    if set(values) != expected_fields:
        unexpected = sorted(set(values) - expected_fields)
        missing = sorted(expected_fields - set(values))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unsupported " + ", ".join(unexpected))
        raise ValueError("responsibility operation requires exact fields: " + "; ".join(details))

    if op == "inspect":
        return values

    for field in ("node_id", "source_thread_id", "request_id"):
        values[field] = _text(values[field], field)
    values["attempt"] = _integer(values["attempt"], "attempt", minimum=0)
    values["task_revision"] = _integer(values["task_revision"], "task_revision", minimum=1)
    values["next_action"] = _next_action(values["next_action"])
    values["deadline"] = _text(values["deadline"], "deadline")

    if op in {"propose_handoff", "claim_handoff", "defer"}:
        values["expected_responsibility_revision"] = _integer(
            values["expected_responsibility_revision"],
            "expected_responsibility_revision",
            minimum=1,
        )
    if op == "propose_handoff":
        values["proposed_owner"] = _text(values["proposed_owner"], "proposed_owner")
    if op == "defer":
        values["wait_reason"] = _wait_reason(
            values["wait_reason"], source_thread_id=values["source_thread_id"]
        )
        if values["next_recheck_at"] is not None:
            values["next_recheck_at"] = _text(values["next_recheck_at"], "next_recheck_at")
    return values


def responsibility_tool(store: WorkbenchStore, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch one validated responsibility operation to ``ResponsibilityLedger``.

    ``request_id`` is intentionally passed as the ledger's business
    ``command_id``.  The Authority caller may use the same value for its
    outer journal receipt; no second id is invented here.
    """

    values = _arguments(arguments)
    ledger = ResponsibilityLedger(store)
    op = values["op"]
    if op == "list":
        return ledger.list_for_task(values["task_id"], limit=values["limit"], cursor=values["cursor"])
    if op == "inspect":
        return ledger.inspect(task_id=values["task_id"], goal_id=values["goal_id"])

    source_thread_id = values["source_thread_id"]
    common = {
        "command_id": values["request_id"],
        "task_id": values["task_id"],
        "goal_id": values["goal_id"],
        "node_id": values["node_id"],
        "attempt": values["attempt"],
        "task_revision": values["task_revision"],
        "next_action": values["next_action"],
        "deadline": values["deadline"],
    }
    if op == "open":
        return ledger.open(owner=source_thread_id, **common)
    if op == "propose_handoff":
        return ledger.propose_handoff(
            current_owner=source_thread_id,
            proposed_owner=values["proposed_owner"],
            expected_responsibility_revision=values["expected_responsibility_revision"],
            **common,
        )
    if op == "claim_handoff":
        return ledger.claim_handoff(
            claimant=source_thread_id,
            expected_responsibility_revision=values["expected_responsibility_revision"],
            **common,
        )
    if op == "defer":
        return ledger.defer(
            current_owner=source_thread_id,
            wait_reason=values["wait_reason"],
            next_recheck_at=values["next_recheck_at"],
            expected_responsibility_revision=values["expected_responsibility_revision"],
            **common,
        )
    raise AssertionError(f"unhandled responsibility operation: {op}")
