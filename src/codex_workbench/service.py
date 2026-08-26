from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable

from .artifacts import ArtifactStore
from .executors import (
    ClaudeExecutor,
    CodexExecutor,
    DeterministicExecutor,
    ExecutionRequest,
    FixtureExecutor,
    validate_worker_scope,
)
from .evidence import reusable_evidence_key
from .model import NodeResult
from .store import WorkbenchStore
from .worktrees import WorktreeError, WorktreeManager


class Coordinator:
    def __init__(
        self,
        store: WorkbenchStore,
        state_root: Path,
        *,
        max_workers: int = 4,
        poll_seconds: float = 1.0,
    ):
        self.store = store
        self.state_root = state_root
        self.max_workers = max_workers
        self.poll_seconds = poll_seconds
        self.artifacts = ArtifactStore(state_root / "artifacts")
        self.worktrees = WorktreeManager(state_root / "worktrees")
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workbench-worker")
        self._futures: dict[Future[None], str] = {}
        self._stop = threading.Event()

    def recover(self) -> int:
        return self.store.recover_interrupted()

    def run_forever(self) -> None:
        worker_counter = 0
        while not self._stop.is_set():
            self._collect()
            while len(self._futures) < self.max_workers:
                worker_counter += 1
                worker_id = f"{socket.gethostname()}-{os.getpid()}-{worker_counter}"
                claimed = self.store.claim_ready_node(worker_id)
                if claimed is None:
                    break
                future = self._pool.submit(self._execute_claimed, claimed)
                self._futures[future] = f"{claimed['task_id']}/{claimed['node_id']}"
            self._stop.wait(self.poll_seconds)
        self._pool.shutdown(wait=True, cancel_futures=False)

    def stop(self) -> None:
        self._stop.set()

    def _collect(self) -> None:
        for future in list(self._futures):
            if future.done():
                self._futures.pop(future)
                future.result()

    def _execute_claimed(self, claimed: dict) -> None:
        request: ExecutionRequest
        try:
            spec = claimed["spec"]
            contract = claimed["contract"]
            worktree: Path | None = None
            if spec["executor"] != "fixture":
                worktree = self.worktrees.prepare(
                    contract["repository"],
                    contract["base_sha"],
                    claimed["task_id"],
                    claimed["node_id"],
                    claimed["attempt"],
                )
                self.store.assign_worktree(claimed["task_id"], claimed["node_id"], str(worktree))
                if spec.get("verifier"):
                    self._compose_worker_patches(claimed["task_id"], worktree)
            request = ExecutionRequest(
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                attempt=claimed["attempt"],
                contract=contract,
                spec=spec,
                worktree=worktree,
            )
            cache_key = reusable_evidence_key(contract, spec, worktree)
            cached = self.store.cached_evidence(cache_key) if cache_key else None
            if cached is not None:
                self.store.record_evidence_reuse(
                    cache_key,
                    claimed["task_id"],
                    claimed["node_id"],
                )
                self.store.settle_node(
                    claimed["task_id"],
                    claimed["node_id"],
                    NodeResult.from_dict(cached["result"]),
                )
                return
            executor = self._executor(spec["executor"])
            result = executor.execute(request)
            if spec["executor"] == "claude" and result.status == "blocked":
                fallback_model = contract["executor_model"]
                self.store.record_node_event(
                    "node.routed",
                    claimed["task_id"],
                    claimed["node_id"],
                    {
                        "attempt": claimed["attempt"],
                        "from": "claude",
                        "to": "codex",
                        "model": fallback_model,
                        "reason": result.summary,
                    },
                )
                request = ExecutionRequest(
                    task_id=request.task_id,
                    node_id=request.node_id,
                    attempt=request.attempt,
                    contract=request.contract,
                    spec={**request.spec, "executor": "codex", "model": fallback_model},
                    worktree=request.worktree,
                )
                result = self._executor("codex").execute(request)
            result = validate_worker_scope(self.worktrees, request, result)
            if worktree is not None and result.status == "succeeded" and not spec.get("verifier"):
                patch = self.worktrees.diff_patch(worktree, contract["base_sha"])
                if patch:
                    result = NodeResult(
                        status=result.status,
                        summary=result.summary,
                        artifacts={**result.artifacts, "patch": self.artifacts.put_bytes(patch, "patch")},
                        actual_model=result.actual_model,
                        exit_code=result.exit_code,
                        retryable=result.retryable,
                    )
            if cache_key and result.status == "succeeded":
                self.store.save_evidence(
                    cache_key,
                    result,
                    claimed["task_id"],
                    claimed["node_id"],
                )
        except WorktreeError as error:
            result = NodeResult(status="blocked", summary=f"worktree unavailable: {error}")
        except Exception as error:
            result = NodeResult(status="indeterminate", summary=f"worker crashed: {type(error).__name__}: {error}")
        self.store.settle_node(claimed["task_id"], claimed["node_id"], result)

    def _compose_worker_patches(self, task_id: str, worktree: Path) -> None:
        task = self.store.get_task(task_id)
        for node in task["nodes"]:
            if node.get("verifier") or node["state"] != "accepted" or not node.get("result"):
                continue
            patch_ref = node["result"].get("artifacts", {}).get("patch")
            if patch_ref:
                self.worktrees.apply_patch(worktree, self.artifacts.path_for(patch_ref))

    def _executor(self, kind: str):
        if kind == "fixture":
            return FixtureExecutor(self.artifacts)
        if kind == "deterministic":
            return DeterministicExecutor(self.artifacts)
        if kind == "codex":
            return CodexExecutor(self.artifacts, os.environ.get("CODEX_WORKBENCH_CODEX", "codex"))
        if kind == "claude":
            return ClaudeExecutor(
                self.artifacts,
                self.store.latest_quota(),
                os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude"),
            )
        raise ValueError(f"unsupported executor {kind!r}")
