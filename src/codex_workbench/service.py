from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
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
from .model import ClaudeDispatchDecision, NodeResult, QuotaSnapshot, retry_model
from .quota import JsonFileQuotaAdapter, QuotaRefresher
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeError, WorktreeManager


class Coordinator:
    def __init__(
        self,
        store: WorkbenchStore,
        state_root: Path,
        *,
        coordinator_epoch: int,
        max_workers: int = 4,
        poll_seconds: float = 1.0,
        quota_ttl_seconds: int = 900,
        quota_refresh_seconds: float = 300,
        quota_snapshot_file: Path | None = None,
        fatal_exit: Callable[[int], None] | None = None,
    ):
        self.store = store
        self.state_root = state_root
        self.max_workers = max_workers
        self.poll_seconds = poll_seconds
        self.coordinator_epoch = coordinator_epoch
        self.quota_ttl_seconds = quota_ttl_seconds
        self.artifacts = ArtifactStore(state_root / "artifacts")
        self.worktrees = WorktreeManager(state_root / "worktrees")
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workbench-worker")
        self._futures: dict[Future[None], tuple[str, str | None]] = {}
        self._routed_to_codex: set[str] = set()
        self._routing_lock = threading.Lock()
        self._stop = threading.Event()
        self._fatal_exit = fatal_exit if fatal_exit is not None else os._exit
        quota_path = os.environ.get("CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE")
        quota_source = Path(quota_path).expanduser() if quota_path else quota_snapshot_file
        self._quota_refresher = (
            QuotaRefresher(
                store,
                JsonFileQuotaAdapter(quota_source),
                interval_seconds=quota_refresh_seconds,
            )
            if quota_source is not None
            else None
        )
        self._next_quota_refresh = 0.0
        self._quota_unavailable_reported = False

    def recover(self) -> int:
        return self.store.recover_interrupted()

    def run_forever(self) -> None:
        worker_counter = 0
        while not self._stop.is_set():
            if self._quota_refresher is not None and time.monotonic() >= self._next_quota_refresh:
                try:
                    refreshed = self._quota_refresher.refresh_once()
                    source = self._quota_refresher.adapter
                    if isinstance(source, JsonFileQuotaAdapter) and not source.path.is_file():
                        if not self._quota_unavailable_reported:
                            self.store.record_system_event(
                                "quota.refresh_unavailable",
                                {"path": str(source.path), "policy": "fail-closed"},
                            )
                            self._quota_unavailable_reported = True
                    elif refreshed:
                        self._quota_unavailable_reported = False
                except (OSError, ValueError) as error:
                    self.store.record_system_event(
                        "quota.refresh_failed",
                        {"error": f"{type(error).__name__}: {error}"},
                    )
                self._next_quota_refresh = time.monotonic() + self._quota_refresher.interval_seconds
            self._collect()
            while len(self._futures) < self.max_workers:
                worker_counter += 1
                worker_id = f"{socket.gethostname()}-{os.getpid()}-{worker_counter}"
                quota = self.store.latest_quota()
                active_claude_models = self._active_claude_models()

                def admissible(spec: dict) -> bool:
                    decision = self._claude_decision(
                        spec,
                        quota,
                        active_claude_models,
                        quota_ttl_seconds=self.quota_ttl_seconds,
                    )
                    return decision is None or decision.action != "defer"

                claimed = self.store.claim_ready_node(
                    worker_id,
                    self.coordinator_epoch,
                    admissible=admissible,
                )
                if claimed is None:
                    break
                decision = self._claude_decision(
                    claimed["spec"],
                    quota,
                    active_claude_models,
                    quota_ttl_seconds=self.quota_ttl_seconds,
                )
                active_claude_model = (
                    claimed["spec"]["model"]
                    if decision is not None and decision.action == "claude"
                    else None
                )
                future = self._pool.submit(self._execute_claimed, claimed)
                self._futures[future] = (
                    f"{claimed['task_id']}/{claimed['node_id']}",
                    active_claude_model,
                )
            self._stop.wait(self.poll_seconds)
        self._pool.shutdown(wait=True, cancel_futures=False)

    def stop(self) -> None:
        self._stop.set()

    def _collect(self) -> None:
        for future in list(self._futures):
            if future.done():
                label, _ = self._futures.pop(future)
                with self._routing_lock:
                    self._routed_to_codex.discard(label)
                try:
                    future.result()
                except Exception as error:
                    try:
                        self.store.record_system_event(
                            "coordinator.failed",
                            {"worker": label, "error": f"{type(error).__name__}: {error}"},
                        )
                    finally:
                        self._stop.set()
                        self._fatal_exit(70)

    def _active_claude_models(self) -> tuple[str, ...]:
        with self._routing_lock:
            routed = set(self._routed_to_codex)
        return tuple(
            model
            for label, model in self._futures.values()
            if model is not None and label not in routed
        )

    @staticmethod
    def _claude_decision(
        spec: dict,
        quota: QuotaSnapshot | None,
        active_models: tuple[str, ...] = (),
        *,
        quota_ttl_seconds: int = 900,
    ) -> ClaudeDispatchDecision | None:
        if spec["executor"] != "claude":
            return None
        if quota is None:
            return ClaudeDispatchDecision(
                "codex",
                "unknown",
                "Claude quota is unknown",
                0,
            )
        return quota.dispatch_decision(
            spec["model"],
            active_models,
            max_age_seconds=quota_ttl_seconds,
        )

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
                self.store.assign_worktree(
                    claimed["task_id"],
                    claimed["node_id"],
                    str(worktree),
                    attempt=claimed["attempt"],
                    coordinator_epoch=claimed["coordinator_epoch"],
                    lease_epoch=claimed["lease_epoch"],
                )
                if spec.get("verifier"):
                    self._compose_worker_patches(claimed["task_id"], worktree)
            request = ExecutionRequest(
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                attempt=claimed["attempt"],
                contract=contract,
                spec=spec,
                worktree=worktree,
                steering=claimed["steering"],
            )
            cache_key = reusable_evidence_key(contract, spec, worktree, request.steering)
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
                    attempt=claimed["attempt"],
                    coordinator_epoch=claimed["coordinator_epoch"],
                    lease_epoch=claimed["lease_epoch"],
                )
                return
            decision = self._claude_decision(
                spec,
                self.store.latest_quota(),
                quota_ttl_seconds=self.quota_ttl_seconds,
            )
            if decision is not None and decision.action == "codex":
                request, result = self._execute_codex_fallback(
                    claimed,
                    request,
                    decision.reason,
                    decision.zone,
                )
            else:
                result = self._executor(spec["executor"]).execute(request)
                if spec["executor"] == "claude" and result.status == "blocked":
                    request, result = self._execute_codex_fallback(
                        claimed,
                        request,
                        result.summary,
                        decision.zone if decision is not None else "unknown",
                    )
            result = validate_worker_scope(self.worktrees, request, result)
            if worktree is not None and result.status == "succeeded" and not spec.get("verifier"):
                patch = self.worktrees.diff_patch(worktree, contract["base_sha"])
                if patch:
                    result = replace(
                        result,
                        artifacts={
                            **result.artifacts,
                            "patch": self.artifacts.put_bytes(patch, "patch"),
                        },
                    )
            if cache_key and result.status == "succeeded":
                self.store.save_evidence(
                    cache_key,
                    result,
                    claimed["task_id"],
                    claimed["node_id"],
                )
        except WorktreeError as error:
            result = NodeResult(
                status="blocked",
                summary=f"worktree unavailable: {error}",
                result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                verdict="blocked" if claimed["spec"].get("verifier") else None,
            )
        except Exception as error:
            result = NodeResult(
                status="indeterminate",
                summary=f"worker crashed: {type(error).__name__}: {error}",
                result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
            )
        try:
            self.store.settle_node(
                claimed["task_id"],
                claimed["node_id"],
                result,
                attempt=claimed["attempt"],
                coordinator_epoch=claimed["coordinator_epoch"],
                lease_epoch=claimed["lease_epoch"],
            )
        except StateConflictError:
            # A newer coordinator/node lease owns the durable state; this late result is fenced.
            return

    def _execute_codex_fallback(
        self,
        claimed: dict,
        request: ExecutionRequest,
        reason: str,
        zone: str,
    ) -> tuple[ExecutionRequest, NodeResult]:
        fallback_model = retry_model(
            request.contract["executor_model"],
            int(claimed["attempt"]),
        )
        with self._routing_lock:
            self._routed_to_codex.add(f"{claimed['task_id']}/{claimed['node_id']}")
        self.store.record_node_route(
            claimed["task_id"],
            claimed["node_id"],
            executor="codex",
            model=fallback_model,
            payload={
                "attempt": claimed["attempt"],
                "from": "claude",
                "to": "codex",
                "model": fallback_model,
                "zone": zone,
                "reason": reason,
            },
            attempt=claimed["attempt"],
            coordinator_epoch=claimed["coordinator_epoch"],
            lease_epoch=claimed["lease_epoch"],
        )
        routed_request = ExecutionRequest(
            task_id=request.task_id,
            node_id=request.node_id,
            attempt=request.attempt,
            contract=request.contract,
            spec={**request.spec, "executor": "codex", "model": fallback_model},
            worktree=request.worktree,
            steering=request.steering,
        )
        return routed_request, self._executor("codex").execute(routed_request)

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
                self.quota_ttl_seconds,
            )
        raise ValueError(f"unsupported executor {kind!r}")
