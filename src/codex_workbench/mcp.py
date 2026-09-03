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
from .store import CommandConflictError, StateConflictError, WorkbenchStore
from .submission import submit_natural_language_request
from .sync import RepositorySynchronizer, RepositorySyncError


TOOLS: list[dict[str, Any]] = [
    {
        "name": "workbench_request",
        "description": "Compile and optionally queue a bounded development DAG on the Mac mini authority.",
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
                "executor_model": {"type": "string", "default": "gpt-5.6-luna"},
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
        "description": "List durable Workbench tasks and their current DAG state.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
        },
    },
    {
        "name": "workbench_inspect_task",
        "description": "Inspect one task, including nodes, revisions, results, and Evidence refs.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string"}},
        },
    },
    {
        "name": "workbench_control_task",
        "description": "Queue, pause, resume, cancel, or explicitly resolve an indeterminate node.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "action"],
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
                "priority": {"type": "integer", "minimum": -10, "maximum": 10},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 500},
                "node_id": {"type": "string"},
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
                submit_natural_language_request(
                    self.config,
                    self.store,
                    objective=arguments["objective"],
                    repository=repository,
                    allowed_scope=allowed_scopes,
                    forbidden_scope=arguments.get("forbidden_scopes", ()),
                    acceptance_commands=arguments.get("acceptance_commands", ()),
                    task_id=arguments.get("task_id"),
                    command_id=arguments.get("command_id"),
                    executor_model=arguments.get("executor_model", "gpt-5.6-luna"),
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
                    source_thread_id=source_thread_id,
                    context_bundle_ref=binding["context_ref"] if binding else None,
                    context_excerpt=binding["context_excerpt"] if binding else None,
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
            return self._text(self.store.list_tasks(limit=int(arguments.get("limit", 100))))
        if name == "workbench_inspect_task":
            return self._text(self.store.get_task(arguments["task_id"]))
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
            task = self.store.get_task(task_id)
            if action in {"queue", "resume"}:
                revision = self.store.queue_task(task_id)
            elif action == "pause":
                revision = self.store.transition_task(
                    task_id,
                    "paused",
                    expected_revision=int(arguments.get("expected_revision", task["state_revision"])),
                )
            elif action == "cancel":
                revision = self.store.transition_task(
                    task_id,
                    "cancelled",
                    expected_revision=int(arguments.get("expected_revision", task["state_revision"])),
                )
            elif action == "set_priority":
                revision = self.store.set_task_priority(
                    task_id,
                    int(arguments["priority"]),
                    expected_revision=int(arguments["expected_revision"]),
                )
            elif action == "steer":
                revision = self.store.append_task_steering(
                    task_id,
                    arguments["instruction"],
                    expected_revision=int(arguments["expected_revision"]),
                )
            elif action == "resolve_indeterminate":
                revision = self.store.resolve_indeterminate(
                    task_id,
                    arguments["node_id"],
                    arguments["resolution"],
                    expected_revision=int(arguments["expected_revision"]),
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
