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
from .planner import PlannerError
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
            "required": ["objective", "repository", "allowed_scopes"],
            "properties": {
                "objective": {"type": "string"},
                "repository": {"type": "string"},
                "allowed_scopes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "forbidden_scopes": {"type": "array", "items": {"type": "string"}},
                "acceptance_commands": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string"},
                "command_id": {"type": "string"},
                "base_sha": {"type": "string"},
                "executor_model": {"type": "string", "default": "gpt-5.6-luna"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "retry_limit": {"type": "integer", "minimum": 0, "maximum": 3},
                "external_write_permission": {"type": "boolean", "default": False},
                "queue": {"type": "boolean", "default": True},
            },
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
                    "enum": ["queue", "resume", "pause", "cancel", "resolve_indeterminate"]
                },
                "expected_revision": {"type": "integer"},
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
            return self._text(
                submit_natural_language_request(
                    self.config,
                    self.store,
                    objective=arguments["objective"],
                    repository=arguments["repository"],
                    allowed_scope=arguments["allowed_scopes"],
                    forbidden_scope=arguments.get("forbidden_scopes", ()),
                    acceptance_commands=arguments.get("acceptance_commands", ()),
                    task_id=arguments.get("task_id"),
                    command_id=arguments.get("command_id"),
                    executor_model=arguments.get("executor_model", "gpt-5.6-luna"),
                    timeout_seconds=int(arguments.get("timeout_seconds", 3600)),
                    retry_limit=int(arguments.get("retry_limit", 3)),
                    external_write_permission=bool(arguments.get("external_write_permission", False)),
                    queue=bool(arguments.get("queue", True)),
                    base_sha=arguments.get("base_sha"),
                )
            )
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
