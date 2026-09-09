from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, TextIO

from . import __version__
from .acceptance import build_acceptance_report
from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .delivery import DeliveryError, GitHubDelivery, GitHubDeliveryRequest
from .governance import code_as_harness_health
from .planner import PlannerError
from .recovery import RecoveryPolicy, WorktreeRecoveryError, WorktreeRecoveryManager
from .worktrees import WorktreeError
from .store import CommandConflictError, StateConflictError, WorkbenchStore
from .submission import enqueue_natural_language_request, planning_request_receipt
from .sync import RepositorySynchronizer, RepositorySyncError


LIST_TASKS_DEFAULT_LIMIT = 10
LIST_TASKS_MAX_LIMIT = 25
LIST_TASKS_MAX_RESPONSE_BYTES = 64 * 1024
_LIST_TASKS_MAX_TASK_ID_CHARS = 64
_LIST_TASKS_MAX_STATE_CHARS = 24
_LIST_TASKS_MAX_CONTRACT_HASH_CHARS = 64
_LIST_TASKS_MAX_TIMESTAMP_CHARS = 40
_LIST_TASKS_MAX_CURSOR = 9_223_372_036_854_775_807
_LIST_TASKS_NODE_STATES = (
    "pending",
    "queued",
    "running",
    "verifying",
    "accepted",
    "failed",
    "blocked",
    "indeterminate",
    "cancelled",
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "workbench_request",
        "description": "Quickly enqueue a bounded natural-language planning request on the Mac mini authority; planning and model calls run asynchronously.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["objective"],
            "properties": {
                "objective": {"type": "string"},
                "repository": {"type": "string"},
                "allowed_scopes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "source_thread_id": {
                    "type": "string",
                    "description": "Use the latest WB context receipt for this Codex thread.",
                },
                "forbidden_scopes": {"type": "array", "items": {"type": "string"}},
                "acceptance_commands": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string"},
                "command_id": {"type": "string"},
                "base_sha": {"type": "string"},
                "planner_model": {"type": "string", "default": "gpt-5.6-sol"},
                "executor_model": {"type": "string", "default": "gpt-5.6-luna"},
                "verifier_model": {"type": "string", "default": "gpt-5.6-sol"},
                "strategy": {"type": "object"},
                "task_type": {
                    "enum": [
                        "implementation",
                        "debugging",
                        "architecture",
                        "review",
                        "tests",
                        "docs",
                        "creative",
                        "exploration",
                    ],
                    "default": "implementation",
                },
                "complexity": {"enum": ["low", "standard", "high"], "default": "standard"},
                "parallelizable": {"type": "boolean", "default": True},
                "claude_allowed": {"type": "boolean", "default": True},
                "task_points": {"type": "number", "exclusiveMinimum": 0, "default": 1.0},
                "verification_tier": {"enum": ["L0", "L1", "L2", "L3"], "default": "L2"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "retry_limit": {"type": "integer", "minimum": 0, "maximum": 3},
                "external_write_permission": {"type": "boolean", "default": False},
                "queue": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "workbench_get_request",
        "description": "Read the durable status and frozen input of one asynchronous planning request.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command_id"],
            "properties": {"command_id": {"type": "string"}},
        },
    },
    {
        "name": "workbench_get_session",
        "description": "Read the durable WB binding and active task for one Codex thread.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_thread_id"],
            "properties": {"source_thread_id": {"type": "string"}},
        },
    },
    {
        "name": "workbench_continue_session",
        "description": "Append a user message to the active task bound to this Codex thread. It never pauses, cancels, or replaces that objective; use explicit task control for those actions.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_thread_id", "instruction"],
            "properties": {
                "source_thread_id": {"type": "string"},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 500},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
        },
    },
    {
        "name": "workbench_harness_health",
        "description": "Report Codex/Claude binaries, canonical Skill and policy artifacts, plus static Workbench Code-as-Harness wiring. It never authenticates or invokes a model.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "name": "workbench_sync_github",
        "description": "Fast-forward a clean Mac mini checkout from its GitHub remote before planning.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["repository", "branch"],
            "properties": {
                "repository": {"type": "string"},
                "remote": {"type": "string", "default": "origin"},
                "branch": {"type": "string"},
            },
        },
    },
    {
        "name": "workbench_list_tasks",
        "description": "List a deterministic, bounded page of durable Workbench task summaries. Use next_cursor as cursor for the next page; prompts, results, worktrees, and artifacts are available only from workbench_inspect_task.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cursor": {
                    "type": "string",
                    "pattern": "^(0|[1-9][0-9]{0,18})$",
                    "maxLength": 19,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": LIST_TASKS_MAX_LIMIT,
                    "default": LIST_TASKS_DEFAULT_LIMIT,
                },
            },
        },
    },
    {
        "name": "workbench_inspect_task",
        "description": "Inspect one task, including nodes, revisions, results, and Evidence refs. task_ref is the bounded reference returned by workbench_list_tasks when its display task_id is shortened.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "anyOf": [{"required": ["task_id"]}, {"required": ["task_ref"]}],
            "properties": {
                "task_id": {"type": "string"},
                "task_ref": {
                    "type": "string",
                    "pattern": "^[1-9][0-9]{0,18}$",
                    "maxLength": 19,
                },
            },
        },
    },
    {
        "name": "workbench_control_task",
        "description": "Queue, pause, resume, cancel, steer, or explicitly resolve an indeterminate node. Queue/resume may include an instruction, which is validated and persisted atomically before launch. Resuming a blocked task requires an exact node attempt plus an explicit recovery or no-side-effects assertion.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "action", "expected_revision"],
            "properties": {
                "task_id": {"type": "string"},
                "action": {
                    "enum": [
                        "queue",
                        "resume",
                        "pause",
                        "cancel",
                        "set_priority",
                        "steer",
                        "resolve_indeterminate",
                    ]
                },
                "expected_revision": {"type": "integer"},
                "expected_attempt": {"type": "integer", "minimum": 1},
                "priority": {"type": "integer", "minimum": -10, "maximum": 10},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 500},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                "node_id": {"type": "string"},
                "confirm_recovery": {"type": "boolean"},
                "preserve_untracked": {"type": "boolean"},
                "expected_checkpoint_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "confirm_no_side_effects": {"type": "boolean"},
                "resolution": {"enum": ["retry", "fail", "cancel"]},
            },
        },
    },
    {
        "name": "workbench_deliver_github",
        "description": "Deliver an accepted task through an authorized integration branch, PR, CI, merge, and optional release.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "command_id", "base_branch"],
            "properties": {
                "task_id": {"type": "string"},
                "command_id": {"type": "string"},
                "base_branch": {"type": "string"},
                "remote": {"type": "string", "default": "origin"},
                "merge": {"type": "boolean", "default": False},
                "release_tag": {"type": "string"},
            },
        },
    },
    {
        "name": "workbench_read_events",
        "description": "Read the durable event/evidence timeline from a cursor.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string"},
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "name": "workbench_read_artifact",
        "description": "Read a content-addressed text Evidence artifact with a bounded response.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_ref"],
            "properties": {
                "artifact_ref": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
            },
        },
    },
    {
        "name": "workbench_acceptance_report",
        "description": "Evaluate Workbench A1-A12 from the Mac mini durable Evidence ledger.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "name": "workbench_worktree_status",
        "description": "Read Workbench-owned worktree recycling, NAS archive, and short-lived home-presence state.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
    {
        "name": "workbench_reclaim_worktrees",
        "description": "Run one bounded recovery sweep. Eligible worktrees are quarantined first; deletion requires a fully restored and verified NAS receipt.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_items": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1}
            },
        },
    },
    {
        "name": "workbench_restore_worktree",
        "description": "Restore one verified NAS worktree archive into a new recovery directory on the authority.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["archive_id"],
            "properties": {
                "archive_id": {"type": "string"},
                "destination": {"type": "string"},
            },
        },
    },
    {
        "name": "workbench_list_approvals",
        "description": "List durable pending approval receipts from the Mac mini authority.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pending_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "name": "workbench_decide_approval",
        "description": "Apply an explicit retry, fail, or cancel decision to a pending receipt.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["approval_id", "decision", "expected_revision"],
            "properties": {
                "approval_id": {"type": "string"},
                "decision": {"enum": ["retry", "fail", "cancel"]},
                "expected_revision": {"type": "integer", "minimum": 1},
            },
        },
    },
]


class WorkbenchMCPServer:
    def __init__(self, config: WorkbenchConfig, store: WorkbenchStore):
        self.config = config
        self.store = store
        self.artifacts = ArtifactStore(config.state_root / "artifacts")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "codex-workbench", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self._tool_result(params.get("name"), params.get("arguments") or {})
            else:
                return self._error(request_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (
            CommandConflictError,
            DeliveryError,
            KeyError,
            OSError,
            PlannerError,
            RepositorySyncError,
            StateConflictError,
            WorktreeRecoveryError,
            WorktreeError,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            }

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _text(value: Any) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}
            ]
        }

    @staticmethod
    def _required_expected_revision(arguments: dict[str, Any]) -> int:
        value = arguments.get("expected_revision")
        if type(value) is not int:
            raise ValueError("expected_revision must be an integer")
        return value

    @staticmethod
    def _control_instruction(arguments: dict[str, Any]) -> str | None:
        if "instruction" not in arguments:
            return None
        instruction = arguments["instruction"]
        if type(instruction) is not str:
            raise ValueError("instruction must be a string")
        normalized = instruction.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("instruction must contain 1 to 500 characters")
        return normalized

    @staticmethod
    def _required_blocked_resume_text(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if type(value) is not str:
            raise ValueError(f"{name} must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError(f"{name} must contain 1 to 500 characters")
        return normalized

    @staticmethod
    def _required_blocked_resume_attempt(arguments: dict[str, Any]) -> int:
        value = arguments.get("expected_attempt")
        if type(value) is not int or value < 1:
            raise ValueError("expected_attempt must be a positive integer")
        return value

    @staticmethod
    def _optional_strict_boolean(arguments: dict[str, Any], name: str) -> bool:
        if name not in arguments:
            return False
        value = arguments[name]
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
        return value

    def _resume_blocked_task(
        self,
        task_id: str,
        arguments: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        if "instruction" in arguments:
            raise ValueError(
                "blocked resume uses a durable recovery reason; steer the blocked task first "
                "if the next attempt also needs a new instruction"
            )
        node_id = arguments.get("node_id")
        if type(node_id) is not str or not node_id.strip():
            raise ValueError("node_id is required to resume a blocked task")
        node_id = node_id.strip()
        expected_attempt = self._required_blocked_resume_attempt(arguments)
        reason = self._required_blocked_resume_text(arguments, "reason")
        confirm_recovery = self._optional_strict_boolean(arguments, "confirm_recovery")
        preserve_untracked = self._optional_strict_boolean(arguments, "preserve_untracked")
        confirm_no_side_effects = self._optional_strict_boolean(
            arguments,
            "confirm_no_side_effects",
        )

        task = self.store.get_task(task_id)
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            raise StateConflictError("blocked task nodes are invalid")
        node = next(
            (
                item
                for item in nodes
                if isinstance(item, dict) and item.get("node_id") == node_id
            ),
            None,
        )
        if not isinstance(node, dict) or node.get("state") != "blocked":
            raise StateConflictError(f"node {node_id} is not blocked")
        result = node.get("result")
        changed_paths = result.get("changed_paths") if isinstance(result, dict) else None
        if not isinstance(changed_paths, list):
            raise StateConflictError(
                "blocked node result must contain changed_paths as an explicit list"
            )

        if changed_paths:
            if not confirm_recovery:
                raise ValueError(
                    "dirty blocked resume requires confirm_recovery=true"
                )
            if confirm_no_side_effects:
                raise ValueError(
                    "confirm_no_side_effects cannot authorize a dirty blocked resume"
                )
            resumed = self.store.capture_and_resume_blocked_worktree(
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
                reason=reason,
                preserve_untracked=preserve_untracked,
                expected_checkpoint_sha=arguments.get("expected_checkpoint_sha"),
            )
            return {
                "ok": True,
                "action": "resume-blocked-worktree",
                "operator_confirmed": True,
                **resumed,
            }

        if confirm_recovery or preserve_untracked or "expected_checkpoint_sha" in arguments:
            raise ValueError(
                "clean blocked resume does not accept recovery or untracked-preservation assertions"
            )
        resumed = self.store.retry_blocked_node(
            task_id,
            node_id,
            expected_revision=expected_revision,
            expected_attempt=expected_attempt,
            reason=reason,
            confirm_no_side_effects=confirm_no_side_effects,
        )
        return {"ok": True, "action": "retry-blocked", **resumed}

    @staticmethod
    def _bounded_summary_text(value: Any, maximum: int) -> tuple[str, bool]:
        text = str(value)
        if len(text) <= maximum:
            return text, False
        return f"{text[: maximum - 1]}…", True

    @staticmethod
    def _list_tasks_arguments(arguments: dict[str, Any]) -> tuple[int, int]:
        limit = arguments.get("limit", LIST_TASKS_DEFAULT_LIMIT)
        if type(limit) is not int:
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= LIST_TASKS_MAX_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {LIST_TASKS_MAX_LIMIT}"
            )

        cursor = arguments.get("cursor", "0")
        return limit, WorkbenchMCPServer._sqlite_row_reference(
            cursor, "cursor", allow_zero=True
        )

    @staticmethod
    def _sqlite_row_reference(
        value: Any, label: str, *, allow_zero: bool = False
    ) -> int:
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError(f"{label} must be a decimal string")
        if len(value) > 1 and value.startswith("0"):
            raise ValueError(f"{label} must not contain leading zeroes")
        parsed = int(value)
        minimum = 0 if allow_zero else 1
        if not minimum <= parsed <= _LIST_TASKS_MAX_CURSOR:
            raise ValueError(f"{label} is outside the SQLite row reference range")
        return parsed

    def _list_task_summaries(self, limit: int, cursor: int) -> dict[str, Any]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                WITH page AS (
                    SELECT
                        rowid AS task_ref,
                        task_id,
                        state,
                        state_revision,
                        priority,
                        contract_hash,
                        created_at,
                        updated_at
                    FROM tasks
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                    LIMIT ?
                )
                SELECT
                    page.task_ref,
                    page.task_id,
                    page.state,
                    page.state_revision,
                    page.priority,
                    page.contract_hash,
                    page.created_at,
                    page.updated_at,
                    COUNT(nodes.node_id) AS node_total,
                    COALESCE(SUM(CASE WHEN nodes.state = 'pending' THEN 1 ELSE 0 END), 0) AS node_pending,
                    COALESCE(SUM(CASE WHEN nodes.state = 'queued' THEN 1 ELSE 0 END), 0) AS node_queued,
                    COALESCE(SUM(CASE WHEN nodes.state = 'running' THEN 1 ELSE 0 END), 0) AS node_running,
                    COALESCE(SUM(CASE WHEN nodes.state = 'verifying' THEN 1 ELSE 0 END), 0) AS node_verifying,
                    COALESCE(SUM(CASE WHEN nodes.state = 'accepted' THEN 1 ELSE 0 END), 0) AS node_accepted,
                    COALESCE(SUM(CASE WHEN nodes.state = 'failed' THEN 1 ELSE 0 END), 0) AS node_failed,
                    COALESCE(SUM(CASE WHEN nodes.state = 'blocked' THEN 1 ELSE 0 END), 0) AS node_blocked,
                    COALESCE(SUM(CASE WHEN nodes.state = 'indeterminate' THEN 1 ELSE 0 END), 0) AS node_indeterminate,
                    COALESCE(SUM(CASE WHEN nodes.state = 'cancelled' THEN 1 ELSE 0 END), 0) AS node_cancelled
                FROM page
                LEFT JOIN nodes ON nodes.task_id = page.task_id
                GROUP BY
                    page.task_ref,
                    page.task_id,
                    page.state,
                    page.state_revision,
                    page.priority,
                    page.contract_hash,
                    page.created_at,
                    page.updated_at
                ORDER BY page.task_ref ASC
                """,
                (cursor, limit + 1),
            ).fetchall()

        summaries: list[tuple[int, dict[str, Any]]] = []
        for row in rows[:limit]:
            task_id, task_id_truncated = self._bounded_summary_text(
                row["task_id"], _LIST_TASKS_MAX_TASK_ID_CHARS
            )
            state, _ = self._bounded_summary_text(
                row["state"], _LIST_TASKS_MAX_STATE_CHARS
            )
            contract_hash, _ = self._bounded_summary_text(
                row["contract_hash"], _LIST_TASKS_MAX_CONTRACT_HASH_CHARS
            )
            created_at, _ = self._bounded_summary_text(
                row["created_at"], _LIST_TASKS_MAX_TIMESTAMP_CHARS
            )
            updated_at, _ = self._bounded_summary_text(
                row["updated_at"], _LIST_TASKS_MAX_TIMESTAMP_CHARS
            )
            summaries.append(
                (
                    int(row["task_ref"]),
                    {
                        "task_id": task_id,
                        "task_id_truncated": task_id_truncated,
                        "task_ref": str(row["task_ref"]),
                        "state": state,
                        "state_revision": int(row["state_revision"]),
                        "priority": int(row["priority"]),
                        "contract_hash": contract_hash,
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "node_counts": {
                            "total": int(row["node_total"]),
                            **{
                                state_name: int(row[f"node_{state_name}"])
                                for state_name in _LIST_TASKS_NODE_STATES
                            },
                        },
                    },
                )
            )

        while summaries:
            has_more = len(rows) > len(summaries)
            payload = {
                "tasks": [summary for _, summary in summaries],
                "next_cursor": str(summaries[-1][0]) if has_more else None,
            }
            serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            if len(serialized) <= LIST_TASKS_MAX_RESPONSE_BYTES:
                return payload
            summaries.pop()

        if rows:
            raise ValueError("task summary cannot fit within the response limit")
        return {"tasks": [], "next_cursor": None}

    def _inspect_task_id(self, arguments: dict[str, Any]) -> str:
        has_task_id = "task_id" in arguments
        has_task_ref = "task_ref" in arguments
        if has_task_id and has_task_ref:
            raise ValueError("pass either task_id or task_ref, not both")
        if has_task_id:
            task_id = arguments["task_id"]
            if type(task_id) is not str:
                raise ValueError("task_id must be a string")
            return task_id
        if not has_task_ref:
            raise ValueError("task_id or task_ref is required")
        task_ref = self._sqlite_row_reference(arguments["task_ref"], "task_ref")
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT task_id FROM tasks WHERE rowid = ?", (task_ref,)
            ).fetchone()
        if row is None:
            raise KeyError(f"task_ref not found: {task_ref}")
        return str(row["task_id"])

    def _tool_result(self, name: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "workbench_request":
            source_thread_id = arguments.get("source_thread_id")
            binding = (
                self.store.get_session_binding(source_thread_id)
                if source_thread_id
                else None
            )
            repository = arguments.get("repository") or (
                binding["repository"] if binding else None
            )
            allowed_scopes = arguments.get("allowed_scopes") or (
                binding["allowed_scopes"] if binding else None
            )
            if not repository or not allowed_scopes:
                raise ValueError(
                    "repository and allowed_scopes are required unless source_thread_id has an active WB binding"
                )
            return self._text(
                enqueue_natural_language_request(
                    self.config,
                    self.store,
                    objective=arguments["objective"],
                    repository=repository,
                    allowed_scope=allowed_scopes,
                    forbidden_scope=arguments.get("forbidden_scopes", ()),
                    acceptance_commands=arguments.get("acceptance_commands", ()),
                    task_id=arguments.get("task_id"),
                    command_id=arguments.get("command_id"),
                    planner_model=arguments.get("planner_model", "gpt-5.6-sol"),
                    executor_model=arguments.get("executor_model", "gpt-5.6-luna"),
                    verifier_model=arguments.get("verifier_model", "gpt-5.6-sol"),
                    task_type=arguments.get("task_type", "implementation"),
                    complexity=arguments.get("complexity", "standard"),
                    parallelizable=bool(arguments.get("parallelizable", True)),
                    claude_allowed=bool(arguments.get("claude_allowed", True)),
                    task_points=float(arguments.get("task_points", 1.0)),
                    verification_tier=arguments.get("verification_tier", "L2"),
                    timeout_seconds=int(arguments.get("timeout_seconds", 3600)),
                    retry_limit=int(arguments.get("retry_limit", 3)),
                    external_write_permission=bool(arguments.get("external_write_permission", False)),
                    queue=bool(arguments.get("queue", True)),
                    base_sha=arguments.get("base_sha") or (binding["base_sha"] if binding else None),
                    strategy=arguments.get("strategy"),
                    source_thread_id=source_thread_id,
                    context_bundle_ref=binding["context_ref"] if binding else None,
                    context_excerpt=binding["context_excerpt"] if binding else None,
                )
            )
        if name == "workbench_get_request":
            return self._text(
                planning_request_receipt(
                    self.store.get_planning_request(arguments["command_id"])
                )
            )
        if name == "workbench_get_session":
            binding = self.store.get_session_binding(arguments["source_thread_id"])
            return self._text(
                {key: value for key, value in binding.items() if key != "context_excerpt"}
            )
        if name == "workbench_continue_session":
            receipt = self.store.append_active_session_steering(
                arguments["source_thread_id"],
                arguments["instruction"],
                expected_revision=(
                    int(arguments["expected_revision"])
                    if "expected_revision" in arguments
                    else None
                ),
            )
            return self._text(
                {
                    "ok": True,
                    "continuation": "active-objective-preserved",
                    **receipt,
                }
            )
        if name == "workbench_harness_health":
            return self._text(code_as_harness_health(self.config))
        if name == "workbench_sync_github":
            return self._text(
                RepositorySynchronizer().sync_github(
                    arguments["repository"],
                    arguments.get("remote", "origin"),
                    arguments["branch"],
                )
            )
        if name == "workbench_list_tasks":
            limit, cursor = self._list_tasks_arguments(arguments)
            return self._text(self._list_task_summaries(limit, cursor))
        if name == "workbench_inspect_task":
            return self._text(self.store.get_task(self._inspect_task_id(arguments)))
        if name == "workbench_read_events":
            return self._text(
                self.store.read_events(
                    after=int(arguments.get("after", 0)),
                    limit=int(arguments.get("limit", 500)),
                    task_id=arguments.get("task_id"),
                )
            )
        if name == "workbench_deliver_github":
            return self._text(
                GitHubDelivery(self.store, self.artifacts).deliver(
                    GitHubDeliveryRequest(
                        task_id=arguments["task_id"],
                        command_id=arguments["command_id"],
                        base_branch=arguments["base_branch"],
                        remote=arguments.get("remote", "origin"),
                        merge=bool(arguments.get("merge", False)),
                        release_tag=arguments.get("release_tag"),
                    )
                )
            )
        if name == "workbench_read_artifact":
            ref = arguments["artifact_ref"]
            path = self.artifacts.path_for(ref)
            data = path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            limit = int(arguments.get("max_chars", 100000))
            return self._text(
                {
                    "artifact_ref": ref,
                    "size_bytes": len(data),
                    "truncated": len(text) > limit,
                    "text": text[:limit],
                }
            )
        if name == "workbench_acceptance_report":
            return self._text(build_acceptance_report(self.store))
        if name == "workbench_worktree_status":
            policy = RecoveryPolicy.load(self.config.state_root)
            return self._text(
                {
                    "enabled": policy.enabled,
                    "home_presence": self.store.active_home_presence(),
                    "allocations": self.store.list_worktree_allocations(),
                    "archives": self.store.list_worktree_archives(),
                }
            )
        if name == "workbench_reclaim_worktrees":
            max_items = int(arguments.get("max_items", 1))
            if max_items < 1 or max_items > 100:
                raise ValueError("max_items must be between 1 and 100")
            return self._text(
                WorktreeRecoveryManager(
                    self.store,
                    RecoveryPolicy.load(self.config.state_root),
                ).sweep(max_items=max_items)
            )
        if name == "workbench_restore_worktree":
            destination = arguments.get("destination")
            return self._text(
                WorktreeRecoveryManager(
                    self.store,
                    RecoveryPolicy.load(self.config.state_root),
                ).restore(
                    arguments["archive_id"],
                    Path(destination) if destination is not None else None,
                )
            )
        if name == "workbench_list_approvals":
            return self._text(
                self.store.list_approvals(
                    pending_only=bool(arguments.get("pending_only", True)),
                    limit=int(arguments.get("limit", 100)),
                )
            )
        if name == "workbench_decide_approval":
            revision = self.store.decide_approval(
                arguments["approval_id"],
                arguments["decision"],
                expected_revision=int(arguments["expected_revision"]),
            )
            return self._text(
                {
                    "ok": True,
                    "approval_id": arguments["approval_id"],
                    "revision": revision,
                }
            )
        if name == "workbench_control_task":
            task_id = arguments["task_id"]
            action = arguments["action"]
            expected_revision = self._required_expected_revision(arguments)
            if action in {"queue", "resume"}:
                if action == "resume" and self.store.get_task(task_id)["state"] == "blocked":
                    return self._text(
                        self._resume_blocked_task(
                            task_id,
                            arguments,
                            expected_revision=expected_revision,
                        )
                    )
                instruction = self._control_instruction(arguments)
                if instruction is not None:
                    receipt = self.store.queue_task_with_instruction(
                        task_id,
                        instruction,
                        expected_revision=expected_revision,
                    )
                    return self._text({"ok": True, "task_id": task_id, **receipt})
                revision = self.store.queue_task(
                    task_id,
                    expected_revision=expected_revision,
                )
            elif action == "pause":
                revision = self.store.transition_task(
                    task_id,
                    "paused",
                    expected_revision=expected_revision,
                )
            elif action == "cancel":
                revision = self.store.transition_task(
                    task_id,
                    "cancelled",
                    expected_revision=expected_revision,
                )
            elif action == "set_priority":
                revision = self.store.set_task_priority(
                    task_id,
                    int(arguments["priority"]),
                    expected_revision=expected_revision,
                )
            elif action == "steer":
                instruction = self._control_instruction(arguments)
                if instruction is None:
                    raise ValueError("instruction is required")
                receipt = self.store.append_task_steering_receipt(
                    task_id,
                    instruction,
                    expected_revision=expected_revision,
                )
                return self._text({"ok": True, "task_id": task_id, **receipt})
            elif action == "resolve_indeterminate":
                revision = self.store.resolve_indeterminate(
                    task_id,
                    arguments["node_id"],
                    arguments["resolution"],
                    expected_revision=expected_revision,
                )
            else:
                raise ValueError(f"unsupported control action: {action}")
            return self._text({"ok": True, "task_id": task_id, "revision": revision})
        raise ValueError(f"unknown Workbench tool: {name}")


def serve_stdio(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    server = WorkbenchMCPServer(config, store)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = server.handle(message)
        except json.JSONDecodeError as error:
            response = WorkbenchMCPServer._error(None, -32700, f"parse error: {error}")
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
