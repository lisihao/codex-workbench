from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid

from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .executors import ClaudeExecutor
from .model import TaskContract
from .planner import CodexPlanner
from .store import WorkbenchStore


def submit_natural_language_request(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    *,
    objective: str,
    repository: str,
    allowed_scope: list[str] | tuple[str, ...],
    forbidden_scope: list[str] | tuple[str, ...] = (),
    acceptance_commands: list[str] | tuple[str, ...] = (),
    task_id: str | None = None,
    command_id: str | None = None,
    planner_model: str = "gpt-5.6-sol",
    executor_model: str = "gpt-5.6-luna",
    verifier_model: str = "gpt-5.6-sol",
    timeout_seconds: int = 3600,
    retry_limit: int = 3,
    external_write_permission: bool = False,
    queue: bool = True,
    base_sha: str | None = None,
) -> dict:
    resolved_repository = Path(repository).expanduser().resolve(strict=True)
    resolved_base_sha = subprocess.check_output(
        [
            "git",
            "-C",
            str(resolved_repository),
            "rev-parse",
            f"{base_sha or 'HEAD'}^{{commit}}",
        ],
        text=True,
    ).strip()
    resolved_task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
    contract = TaskContract(
        task_id=resolved_task_id,
        repository=str(resolved_repository),
        base_sha=resolved_base_sha,
        objective=objective,
        allowed_scope=tuple(allowed_scope),
        forbidden_scope=tuple(forbidden_scope),
        acceptance_commands=tuple(acceptance_commands),
        planner_model=planner_model,
        executor_model=executor_model,
        verifier_model=verifier_model,
        timeout_seconds=timeout_seconds,
        retry_limit=retry_limit,
        external_write_permission=external_write_permission,
        destructive_action_permission=False,
    )
    contract.validate()
    artifacts = ArtifactStore(config.state_root / "artifacts")
    claude_ok, _ = ClaudeExecutor(
        artifacts,
        store.latest_quota(),
        os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude"),
    ).qualification("sonnet")
    nodes = CodexPlanner(
        os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
        model=planner_model,
    ).compile(
        contract,
        claude_available=claude_ok,
        default_executor_model=executor_model,
        verifier_model=verifier_model,
    )
    resolved_command_id = command_id or f"request-{uuid.uuid4()}"
    store.create_task(contract, nodes, resolved_command_id)
    if queue:
        store.queue_task(resolved_task_id)
    return {
        "ok": True,
        "task_id": resolved_task_id,
        "command_id": resolved_command_id,
        "base_sha": resolved_base_sha,
        "claude_dispatch_available": claude_ok,
        "nodes": [node.to_dict() for node in nodes],
    }
