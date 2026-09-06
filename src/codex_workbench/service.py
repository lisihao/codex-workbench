from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable

from .artifacts import ArtifactStore
from .dependency_inputs import (
    DependencyInput,
    DependencyInputError,
    apply_accepted_ancestor_patches,
    apply_recorded_dependency_input,
    base_dependency_input,
    changed_paths_since_input_tree,
    effective_spec_with_dependency_input,
    load_recorded_dependency_input,
    validate_dependency_input_lineage,
)
from .dirty_worktree_recovery import DirtyWorktreeRecovery, DirtyWorktreeRecoveryError
from .executors import (
    ClaudeExecutor,
    CodexExecutor,
    DeterministicExecutor,
    ExecutionRequest,
    FixtureExecutor,
    validate_archify_verifier_packets,
    validate_worker_scope,
)
from .evidence import reusable_evidence_key
from .governance import governance_receipt_fields
from .model import (
    LEGACY_ROUTING_STRATEGY_VERSION,
    ClaudeDispatchDecision,
    NodeResult,
    QuotaSnapshot,
    TaskContract,
    canonical_json,
    codex_model_profile,
    codex_model_reasoning_effort,
)
from .quota import JsonFileQuotaAdapter, QuotaRefresher
from .recovery import RecoveryPolicy, WorktreeRecoveryManager
from .routing import codex_fallback_model, route_task, strategy_for_node
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeError, WorktreeManager, scope_allows


_ARCHIFY_COMMANDS = frozenset({"deliver", "compare", "visual-check", "validate", "migrate"})
_DIRTY_WORKTREE_RECOVERY_PROVIDER = "workbench-dirty-worktree-recovery"
_FAILED_ATTEMPT_RECOVERY_PROVIDER = "workbench-failed-attempt-recovery"


@dataclass(frozen=True)
class _ClaimRoute:
    """Capacity decision captured atomically with a durable node claim."""

    quota: QuotaSnapshot | None
    active_claude_models: tuple[str, ...]
    decision: ClaudeDispatchDecision | None


class Coordinator:
    def __init__(
        self,
        store: WorkbenchStore,
        state_root: Path,
        *,
        coordinator_epoch: int,
        max_workers: int = 4,
        spark_workers: int | None = None,
        poll_seconds: float = 1.0,
        quota_ttl_seconds: int = 900,
        quota_refresh_seconds: float = 60,
        quota_snapshot_file: Path | None = None,
        fatal_exit: Callable[[int], None] | None = None,
    ):
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        resolved_spark_workers = min(4, max_workers) if spark_workers is None else spark_workers
        if not 0 <= resolved_spark_workers <= max_workers:
            raise ValueError("spark_workers must be between 0 and max_workers")
        self.store = store
        self.state_root = state_root
        self.max_workers = max_workers
        self.spark_workers = resolved_spark_workers
        self._lane_capacities = {
            "spark": self.spark_workers,
            "general": self.max_workers,
            "control": self.max_workers,
        }
        self.poll_seconds = poll_seconds
        self.coordinator_epoch = coordinator_epoch
        self.quota_ttl_seconds = quota_ttl_seconds
        self.artifacts = ArtifactStore(state_root / "artifacts")
        self.worktrees = WorktreeManager(state_root / "worktrees")
        self.blocked_worktree_recovery = DirtyWorktreeRecovery(self.artifacts, self.worktrees)
        # Failed retries use the same sealed capture/lineage mechanism, but
        # continue into their original executor after restoration.
        self.failed_attempt_recovery = self.blocked_worktree_recovery
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workbench-worker")
        self._futures: dict[Future[None], tuple[str, str | None]] = {}
        self._routed_to_codex: set[str] = set()
        self._routing_lock = threading.Lock()
        self._stop = threading.Event()
        self.recovery = WorktreeRecoveryManager(store, RecoveryPolicy.load(state_root))
        self._recovery_thread = threading.Thread(
            target=self.recovery.run_forever,
            args=(self._stop,),
            name="workbench-worktree-recovery",
            daemon=True,
        )
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
        recovered, _ = self.store.recover_interrupted_with_orphans()
        for orphan in self.store.pending_failed_attempt_recovery_orphans():
            target = self.worktrees.worktree_path(
                str(orphan["task_id"]),
                str(orphan["node_id"]),
                int(orphan["attempt"]),
            )
            if not target.exists():
                self.store.record_system_event(
                    "failed_attempt_recovery.orphan_resolved",
                    {
                        **orphan,
                        "target": str(target),
                        "status": "target_absent",
                    },
                )
                continue
            try:
                archive = self.worktrees.archive_failed_recovery(
                    str(orphan["repository"]),
                    target,
                    str(orphan["branch"]),
                    task_id=str(orphan["task_id"]),
                    node_id=str(orphan["node_id"]),
                    attempt=int(orphan["attempt"]),
                )
            except (OSError, ValueError, WorktreeError) as error:
                self.store.record_system_event(
                    "failed_attempt_recovery.orphan_cleanup_failed",
                    {
                        **orphan,
                        "target": str(target),
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            else:
                self.store.record_system_event(
                    "failed_attempt_recovery.orphan_resolved",
                    {
                        **orphan,
                        "target": str(target),
                        "archive": str(archive),
                        "status": "archived",
                    },
                )
        return recovered

    def run_forever(self) -> None:
        worker_counter = 0
        self._recovery_thread.start()
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
                # Capacity saturation is not a reason to leave a global
                # Workbench worker slot idle.  Claim the ready node and carry
                # the exact decision into its thread, where it persistently
                # routes to the governed Codex fallback in the same attempt.
                claimed = self._claim_next_ready_node(worker_id)
                if claimed is None:
                    break
                decision = (
                    None
                    if claimed.get("blocked_worktree_recovery") is not None
                    else self._claim_time_decision(
                        claimed["spec"], claimed["contract"], quota, active_claude_models
                    )
                )
                claim_route = _ClaimRoute(quota, active_claude_models, decision)
                active_claude_model = (
                    claimed["spec"]["model"]
                    if decision is not None and decision.action == "claude"
                    else None
                )
                future = self._pool.submit(self._execute_claimed, claimed, claim_route)
                self._futures[future] = (
                    f"{claimed['task_id']}/{claimed['node_id']}",
                    active_claude_model,
                )
            self._stop.wait(self.poll_seconds)
        self._pool.shutdown(wait=True, cancel_futures=False)
        self._recovery_thread.join(timeout=30)

    def _claim_next_ready_node(self, worker_id: str) -> dict | None:
        """Prefer ready, admissible Spark work without reserving idle threads.

        The store re-applies task priority, dependency, parallelizability,
        scope-conflict, authority, and lane-capacity checks for both attempts.
        General/control work borrows every slot Spark cannot currently use.
        """

        if self.spark_workers:
            spark = self.store.claim_ready_node(
                worker_id,
                self.coordinator_epoch,
                execution_lanes=("spark",),
                lane_capacities=self._lane_capacities,
            )
            if spark is not None:
                return spark
        return self.store.claim_ready_node(
            worker_id,
            self.coordinator_epoch,
            execution_lanes=("general", "control"),
            lane_capacities=self._lane_capacities,
        )

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
        contract_raw: dict,
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
        contract = TaskContract.from_dict(contract_raw)
        node_strategy = strategy_for_node(contract, spec)
        shared_capacity = node_strategy.version != LEGACY_ROUTING_STRATEGY_VERSION
        governed = route_task(
            contract,
            claude_models_available=(str(spec["model"]),),
            quota_snapshot=quota,
            active_models=(),
            max_age_seconds=quota_ttl_seconds,
            strategy=node_strategy,
        )
        baseline = quota.dispatch_decision(
            spec["model"],
            max_age_seconds=quota_ttl_seconds,
            shared_capacity=shared_capacity,
        )
        if governed.executor != "claude":
            return ClaudeDispatchDecision(
                "codex",
                baseline.zone,
                f"Task routing contract does not admit Claude: {governed.reason}",
                0,
            )
        return quota.dispatch_decision(
            spec["model"],
            active_models,
            max_age_seconds=quota_ttl_seconds,
            shared_capacity=shared_capacity,
        )

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _is_claude_model(model: object) -> bool:
        lower = str(model).lower()
        return any(family in lower for family in ("sonnet", "opus", "fable"))

    def _latest_completed_claude_at(self) -> datetime | None:
        latest: datetime | None = None
        for task in self.store.list_tasks(limit=10_000):
            for node in task.get("nodes", []):
                if node.get("effective_executor") != "claude":
                    continue
                model = node.get("effective_model") or node.get("model")
                if not self._is_claude_model(model):
                    continue
                settled_at = self._timestamp(node.get("settled_at"))
                if settled_at is not None and (latest is None or settled_at > latest):
                    latest = settled_at
        return latest

    def _claim_time_decision(
        self,
        spec: dict,
        contract: dict,
        quota: QuotaSnapshot | None,
        active_models: tuple[str, ...],
    ) -> ClaudeDispatchDecision | None:
        decision = self._claude_decision(
            spec,
            contract,
            quota,
            active_models,
            quota_ttl_seconds=self.quota_ttl_seconds,
        )
        if decision is None or decision.action != "claude" or quota is None:
            return decision
        last_settled_at = self._latest_completed_claude_at()
        if last_settled_at is None:
            return decision
        observed_at = self._timestamp(quota.observed_at)
        if observed_at is not None and observed_at > last_settled_at:
            return decision
        observed_text = quota.observed_at if observed_at is not None else "invalid"
        return ClaudeDispatchDecision(
            "codex",
            "unknown",
            (
                "Claude quota snapshot must be newer than the most recent "
                f"Claude completion ({last_settled_at.isoformat()}); observed_at={observed_text}"
            ),
            0,
        )

    def _runtime_quota_fallback(
        self,
        spec: dict,
        contract: dict,
        claim_route: _ClaimRoute,
    ) -> ClaudeDispatchDecision | None:
        """Only a newer runtime quota/auth state may revoke a reserved turn.

        A claim already reserved shared capacity.  Do not turn that reservation
        into a false overflow merely because the claiming node itself appears
        in the active-model set; concurrently claimed Claude nodes may finish.
        """

        if claim_route.decision is None or claim_route.decision.action != "claude":
            return None
        latest = self.store.latest_quota()
        if latest == claim_route.quota:
            return None
        decision = self._claude_decision(
            spec,
            contract,
            latest,
            claim_route.active_claude_models,
            quota_ttl_seconds=self.quota_ttl_seconds,
        )
        if decision is not None and decision.action == "codex":
            return decision
        return None

    def _execute_claimed(
        self,
        claimed: dict,
        claim_route: _ClaimRoute | None = None,
    ) -> None:
        if claimed.get("blocked_worktree_recovery") is not None:
            self._execute_blocked_worktree_recovery(claimed)
            return
        request: ExecutionRequest
        failed_attempt_recovery = claimed.get("failed_attempt_recovery")
        failed_attempt_assigned = False
        try:
            spec = claimed["spec"]
            contract = claimed["contract"]
            worktree: Path | None = None
            dependency_input: DependencyInput | None = None
            input_receipt_ref: str | None = None
            recovery_artifacts: dict[str, str] = {}
            if failed_attempt_recovery is not None:
                (
                    worktree,
                    dependency_input,
                    input_receipt_ref,
                    recovery_artifacts,
                ) = self._prepare_failed_attempt_recovery(claimed)
                failed_attempt_assigned = True
            elif spec["executor"] != "fixture":
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
                    dependency_input = self._compose_worker_patches(claimed["task_id"], worktree)
                elif spec.get("depends_on"):
                    dependency_input = self._prepare_dependency_input(
                        claimed["task_id"], claimed["node_id"], worktree
                    )
            if input_receipt_ref is None and dependency_input is not None:
                input_receipt_ref = self.artifacts.put_text(
                    canonical_json(dependency_input.receipt), "dependency-input.json"
                )
            request = ExecutionRequest(
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                attempt=claimed["attempt"],
                contract=contract,
                spec=spec,
                worktree=worktree,
                steering=claimed["steering"],
                input_tree_sha=(
                    dependency_input.input_tree_sha if dependency_input is not None else None
                ),
                input_receipt=(dependency_input.receipt if dependency_input is not None else None),
                input_receipt_ref=input_receipt_ref,
            )
            if spec.get("verifier") and spec.get("executor") != "fixture":
                request = replace(
                    request,
                    archify_receipts=self._archify_receipt_packets(claimed["task_id"]),
                )
            cache_spec = effective_spec_with_dependency_input(spec, dependency_input)
            cache_key = reusable_evidence_key(contract, cache_spec, worktree, request.steering)
            cached = self.store.cached_evidence(cache_key) if cache_key else None
            if cached is not None:
                packet_refs: tuple[str, ...] = ()
                if spec.get("verifier") and request.archify_receipts:
                    cache_packet_error, packet_refs = validate_archify_verifier_packets(
                        request,
                        self.artifacts,
                    )
                    if cache_packet_error is not None:
                        cached = None
                if cached is not None:
                    result = NodeResult.from_dict(cached["result"])
                    if recovery_artifacts:
                        result = replace(
                            result,
                            artifacts={**recovery_artifacts, **result.artifacts},
                        )
                    if input_receipt_ref is not None:
                        result = self._with_dependency_input_receipt(result, input_receipt_ref)
                    if packet_refs:
                        result = replace(
                            result,
                            evidence=tuple(dict.fromkeys((*result.evidence, *packet_refs))),
                        )
                    self.store.record_evidence_reuse(
                        cache_key,
                        claimed["task_id"],
                        claimed["node_id"],
                    )
                    self.store.settle_node(
                        claimed["task_id"],
                        claimed["node_id"],
                        result,
                        attempt=claimed["attempt"],
                        coordinator_epoch=claimed["coordinator_epoch"],
                        lease_epoch=claimed["lease_epoch"],
                    )
                    return
            if claim_route is None:
                quota = self.store.latest_quota()
                claim_route = _ClaimRoute(
                    quota,
                    (),
                    self._claim_time_decision(spec, contract, quota, ()),
                )
            decision = claim_route.decision
            if decision is not None and decision.action != "claude":
                fallback_kind = (
                    "claude-capacity-overflow"
                    if decision.action == "defer"
                    else "quota-refresh-required"
                    if "must be newer than the most recent Claude completion" in decision.reason
                    else "quota-or-auth-policy"
                )
                request, result = self._execute_codex_fallback(
                    claimed,
                    request,
                    decision.reason,
                    decision.zone,
                    fallback_kind=fallback_kind,
                )
            else:
                runtime_decision = self._runtime_quota_fallback(spec, contract, claim_route)
                if runtime_decision is not None:
                    request, result = self._execute_codex_fallback(
                        claimed,
                        request,
                        runtime_decision.reason,
                        runtime_decision.zone,
                        fallback_kind="runtime-quota-change",
                    )
                else:
                    result = self._executor(spec["executor"]).execute(request)
                    if spec["executor"] == "claude" and result.status in {"failed", "blocked"}:
                        request, result = self._execute_codex_fallback(
                            claimed,
                            request,
                            result.summary,
                            decision.zone if decision is not None else "unknown",
                            fallback_kind=f"claude-executor-{result.status}",
                        )
            if worktree is not None and not spec.get("verifier") and result.status in {"failed", "blocked"}:
                result = self._with_observed_failure_paths(request, result)
            result = validate_worker_scope(self.worktrees, request, result)
            if worktree is not None and result.status == "succeeded" and not spec.get("verifier"):
                patch = self.worktrees.diff_patch(
                    worktree, request.input_tree_sha or contract["base_sha"]
                )
                if patch:
                    result = replace(
                        result,
                        artifacts={
                            **result.artifacts,
                            "patch": self.artifacts.put_bytes(patch, "patch"),
                        },
                    )
            if recovery_artifacts:
                result = replace(
                    result,
                    artifacts={**recovery_artifacts, **result.artifacts},
                )
            if input_receipt_ref is not None:
                result = self._with_dependency_input_receipt(result, input_receipt_ref)
            if cache_key and result.status == "succeeded":
                self.store.save_evidence(
                    cache_key,
                    result,
                    claimed["task_id"],
                    claimed["node_id"],
                )
        except DependencyInputError as error:
            if failed_attempt_recovery is not None and not failed_attempt_assigned:
                result = self._failed_attempt_recovery_failure(
                    claimed, f"failed-attempt recovery dependency lineage is unavailable: {error}"
                )
            else:
                result = NodeResult(
                    status="blocked",
                    summary=f"dependency inputs unavailable: {error}",
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    verdict="blocked" if claimed["spec"].get("verifier") else None,
                    **governance_receipt_fields(claimed["contract"]),
                )
        except WorktreeError as error:
            if failed_attempt_recovery is not None and not failed_attempt_assigned:
                result = self._failed_attempt_recovery_failure(
                    claimed, f"failed-attempt recovery rejected: {error}"
                )
            else:
                result = NodeResult(
                    status="blocked",
                    summary=f"worktree unavailable: {error}",
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    verdict="blocked" if claimed["spec"].get("verifier") else None,
                    **governance_receipt_fields(claimed["contract"]),
                )
        except Exception as error:
            if failed_attempt_recovery is not None and not failed_attempt_assigned:
                result = self._failed_attempt_recovery_failure(
                    claimed,
                    f"failed-attempt recovery crashed: {type(error).__name__}: {error}",
                )
            else:
                result = NodeResult(
                    status="indeterminate",
                    summary=f"worker crashed: {type(error).__name__}: {error}",
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    **governance_receipt_fields(claimed["contract"]),
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

    def _prepare_failed_attempt_recovery(
        self,
        claimed: dict,
    ) -> tuple[Path, DependencyInput, str, dict[str, str]]:
        """Capture and restore one failed worktree before its executor starts.

        This entire method runs after the durable claim and before model
        dispatch.  It intentionally performs Git and artifact IO outside a
        store transaction; ``assign_failed_attempt_recovery_worktree`` later
        commits only the fenced, already-validated outcome.
        """

        binding = claimed.get("failed_attempt_recovery")
        if not isinstance(binding, dict):
            raise DirtyWorktreeRecoveryError("failed-attempt recovery binding is missing")
        spec = claimed["spec"]
        contract = claimed["contract"]
        if spec.get("verifier"):
            raise DirtyWorktreeRecoveryError(
                "failed-attempt worktree recovery is only supported for worker nodes"
            )
        source = binding.get("source")
        if not isinstance(source, dict):
            raise DirtyWorktreeRecoveryError("failed-attempt recovery source is missing")
        source_worktree = source.get("worktree")
        source_branch = source.get("branch")
        source_base = source.get("base_sha")
        expected_paths = source.get("changed_paths")
        if not (
            isinstance(source_worktree, str)
            and isinstance(source_branch, str)
            and isinstance(source_base, str)
            and isinstance(expected_paths, list)
            and all(isinstance(path, str) and path for path in expected_paths)
        ):
            raise DirtyWorktreeRecoveryError("failed-attempt recovery source is invalid")
        if source_base != contract["base_sha"]:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt recovery source base does not match the task contract"
            )
        source_path = self.failed_attempt_recovery.validate_retry_source(
            repository=contract["repository"],
            base_sha=source_base,
            worktree=source_worktree,
            branch=source_branch,
        )
        ignored_paths = self.failed_attempt_recovery.ignored_paths(source_path)
        if ignored_paths:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt source contains ignored paths that cannot be recovered safely: "
                + ", ".join(ignored_paths)
            )
        try:
            source_result = json.loads(str(binding["source_result_json"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt recovery source result is invalid"
            ) from error
        source_artifacts = source_result.get("artifacts") if isinstance(source_result, dict) else None
        dependency_ref = (
            source_artifacts.get("dependency-input")
            if isinstance(source_artifacts, dict)
            else None
        )
        if dependency_ref is not None:
            if not isinstance(dependency_ref, str) or not dependency_ref:
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt recovery dependency input reference is invalid"
                )
            dependency_input = load_recorded_dependency_input(
                self.artifacts,
                dependency_ref,
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                base_sha=source_base,
            )
            validate_dependency_input_lineage(
                self.store.get_task(claimed["task_id"]),
                claimed["node_id"],
                dependency_input,
            )
        else:
            if spec.get("depends_on"):
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt recovery cannot reproduce an unrecorded dependency input"
                )
            dependency_input = base_dependency_input(
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                base_sha=source_base,
                worktree=source_path,
            )
            dependency_ref = self.artifacts.put_text(
                canonical_json(dependency_input.receipt),
                "failed-attempt-base-input.json",
            )

        actual_paths = tuple(
            sorted(changed_paths_since_input_tree(source_path, dependency_input.input_tree_sha))
        )
        expected = tuple(expected_paths)
        if actual_paths != expected:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt source changes drifted from its result receipt"
            )
        self._validate_failed_attempt_recovery_scope(contract, spec, actual_paths)

        target_attempt = int(claimed["attempt"])
        target = self.worktrees.prepare_clean(
            contract["repository"],
            source_base,
            claimed["task_id"],
            claimed["node_id"],
            target_attempt,
        )
        target_branch = self.worktrees.branch_name(
            claimed["task_id"], claimed["node_id"], target_attempt
        )
        if not actual_paths:
            try:
                # Even a clean failed attempt reproduces its immutable input rather
                # than recomputing accepted ancestors from a potentially changed
                # task snapshot.
                restored = apply_recorded_dependency_input(
                    self.artifacts,
                    self.worktrees,
                    ref=dependency_ref,
                    task_id=claimed["task_id"],
                    node_id=claimed["node_id"],
                    base_sha=source_base,
                    worktree=target,
                )
                if restored.input_tree_sha != dependency_input.input_tree_sha:
                    raise DirtyWorktreeRecoveryError(
                        "failed-attempt recovery input lineage changed before retry"
                    )
                if tuple(
                    sorted(changed_paths_since_input_tree(source_path, dependency_input.input_tree_sha))
                ) != expected:
                    raise DirtyWorktreeRecoveryError(
                        "failed-attempt source changed while its clean retry was being prepared"
                    )
                self.store.assign_failed_attempt_recovery_worktree(
                    claimed["task_id"],
                    claimed["node_id"],
                    str(target),
                    attempt=target_attempt,
                    coordinator_epoch=int(claimed["coordinator_epoch"]),
                    lease_epoch=int(claimed["lease_epoch"]),
                    binding=binding,
                    recovery=None,
                    recovery_ref=None,
                    prepared_recovery=None,
                )
                return target, dependency_input, dependency_ref, {}
            except Exception:
                self._archive_failed_recovery_target(
                    contract=contract,
                    claimed=claimed,
                    target=target,
                    branch=target_branch,
                )
                raise

        try:
            untracked_paths = DirtyWorktreeRecovery.untracked_paths(source_path)
            recovery = self.failed_attempt_recovery.capture(
                repository=contract["repository"],
                base_sha=source_base,
                worktree=str(source_path),
                branch=source_branch,
                attempt=int(source["attempt"]),
                expected_changed_paths=expected,
                task_id=claimed["task_id"],
                node_id=claimed["node_id"],
                input_tree_sha=dependency_input.input_tree_sha,
                dependency_input_ref=dependency_ref,
                preserve_untracked_paths=untracked_paths,
            )
            recovered_paths = tuple(recovery.get("changed_paths", ()))
            if recovered_paths != actual_paths:
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt recovery capture did not preserve the observed changed paths"
                )
            self._validate_failed_attempt_recovery_scope(contract, spec, recovered_paths)
            recovery_ref = self.artifacts.put_text(
                canonical_json(recovery), "failed-attempt-recovery.json"
            )
            outcome = self.failed_attempt_recovery.prepare_for_retry(
                repository=contract["repository"],
                source_worktree=str(source_path),
                target_worktree=str(target),
                target_branch=target_branch,
                target_attempt=target_attempt,
                recovery=recovery,
            )
            if outcome.status != "succeeded":
                raise DirtyWorktreeRecoveryError(outcome.summary)
            prepared_recovery = outcome.prepared_recovery
            expected_prepared = {
                "target_attempt": target_attempt,
                "target_worktree": str(target),
                "target_branch": target_branch,
                "target_patch_sha256": recovery["patch_sha256"],
            }
            if not isinstance(prepared_recovery, dict) or any(
                prepared_recovery.get(key) != value
                for key, value in expected_prepared.items()
            ):
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt recovery prepared target does not match the captured patch"
                )
            self.store.assign_failed_attempt_recovery_worktree(
                claimed["task_id"],
                claimed["node_id"],
                str(target),
                attempt=target_attempt,
                coordinator_epoch=int(claimed["coordinator_epoch"]),
                lease_epoch=int(claimed["lease_epoch"]),
                binding=binding,
                recovery=recovery,
                recovery_ref=recovery_ref,
                prepared_recovery=prepared_recovery,
            )
            return (
                target,
                dependency_input,
                dependency_ref,
                {
                    "failed-attempt-recovery": recovery_ref,
                    "failed-attempt-recovery-snapshot": str(recovery["patch_ref"]),
                },
            )
        except Exception:
            self._archive_failed_recovery_target(
                contract=contract,
                claimed=claimed,
                target=target,
                branch=target_branch,
            )
            raise

    @staticmethod
    def _validate_failed_attempt_recovery_scope(
        contract: dict,
        spec: dict,
        paths: tuple[str, ...],
    ) -> None:
        for relative_path in paths:
            try:
                task_allowed = scope_allows(
                    relative_path,
                    list(contract["allowed_scope"]),
                    list(contract["forbidden_scope"]),
                )
                node_allowed = scope_allows(relative_path, list(spec.get("write_scopes", ())), [])
            except (KeyError, TypeError, ValueError) as error:
                raise DirtyWorktreeRecoveryError(
                    f"failed-attempt recovery path is invalid: {relative_path!r}"
                ) from error
            if not task_allowed:
                raise DirtyWorktreeRecoveryError(
                    f"failed-attempt recovery path is outside task scope: {relative_path}"
                )
            if not node_allowed:
                raise DirtyWorktreeRecoveryError(
                    f"failed-attempt recovery path is outside node write scope: {relative_path}"
                )

    def _with_observed_failure_paths(self, request: ExecutionRequest, result: NodeResult) -> NodeResult:
        if request.worktree is None:
            return result
        paths = tuple(
            sorted(
                (
                    changed_paths_since_input_tree(request.worktree, request.input_tree_sha)
                    if request.input_tree_sha is not None
                    else self.worktrees.changed_paths(
                        request.worktree, request.contract["base_sha"]
                    )
                )
                | set(DirtyWorktreeRecovery.ignored_paths(request.worktree))
            )
        )
        return replace(result, changed_paths=paths)

    def _failed_attempt_recovery_failure(self, claimed: dict, summary: str) -> NodeResult:
        binding = claimed.get("failed_attempt_recovery")
        source = binding.get("source") if isinstance(binding, dict) else None
        changed_paths = (
            tuple(path for path in source.get("changed_paths", ()) if isinstance(path, str))
            if isinstance(source, dict)
            else ()
        )
        return NodeResult(
            status="failed",
            summary=summary,
            result_kind="worker",
            changed_paths=changed_paths,
            checks=(f"REJECTED: {summary}",),
            provider=_FAILED_ATTEMPT_RECOVERY_PROVIDER,
            actual_model=None,
            **governance_receipt_fields(claimed["contract"]),
        )

    def _execute_blocked_worktree_recovery(self, claimed: dict) -> None:
        """Run a blocked-worktree receipt without invoking any model executor."""

        try:
            result = self._run_blocked_worktree_recovery(claimed)
        except (DirtyWorktreeRecoveryError, WorktreeError, ValueError, OSError) as error:
            result = self._blocked_worktree_recovery_failure(
                claimed,
                f"blocked-worktree recovery blocked: {error}",
            )
        except Exception as error:
            # Before a2 is assigned, failures must restore the a1 receipt
            # rather than leaving the recovery node running or indeterminate.
            result = self._blocked_worktree_recovery_failure(
                claimed,
                f"blocked-worktree recovery crashed: {type(error).__name__}: {error}",
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
            return

    def _run_blocked_worktree_recovery(self, claimed: dict) -> NodeResult:
        binding = claimed.get("blocked_worktree_recovery")
        if not isinstance(binding, dict):
            raise WorktreeError("recovery binding is missing")
        spec = claimed["spec"]
        contract = claimed["contract"]
        if spec.get("verifier"):
            raise WorktreeError("blocked-worktree recovery is only supported for worker nodes")
        recovery = binding.get("recovery")
        if not isinstance(recovery, dict):
            raise WorktreeError("blocked-worktree recovery receipt is missing")
        source_worktree = str(recovery["source_worktree"])
        target_attempt = int(claimed["attempt"])
        target = self.worktrees.prepare_clean(
            contract["repository"],
            contract["base_sha"],
            claimed["task_id"],
            claimed["node_id"],
            target_attempt,
        )
        target_branch = self.worktrees.branch_name(
            claimed["task_id"], claimed["node_id"], target_attempt
        )
        outcome = self.blocked_worktree_recovery.prepare(
            repository=contract["repository"],
            source_worktree=source_worktree,
            target_worktree=str(target),
            target_branch=target_branch,
            target_attempt=target_attempt,
            recovery=recovery,
            acceptance_commands=tuple(contract.get("acceptance_commands", ())),
            timeout_seconds=int(contract["timeout_seconds"]),
        )
        artifacts = {
            **outcome.artifacts,
            "recovery-snapshot": str(recovery["patch_ref"]),
        }
        checks = (
            "PASS: source a1 remained read-only recovery evidence",
            f"PASS: source allocation {binding['source_allocation_id']} is bound to a1",
            f"PASS: a2 was prepared as attempt {target_attempt}",
            *outcome.checks,
        )
        if outcome.status != "succeeded":
            artifacts["recovery-failure-archive"] = self._archive_failed_recovery_target(
                contract=contract,
                claimed=claimed,
                target=target,
                branch=target_branch,
            )
            return NodeResult(
                status=outcome.status,
                summary=f"deterministic clean-target recovery: {outcome.summary}",
                artifacts=artifacts,
                exit_code=outcome.exit_code,
                result_kind="worker",
                changed_paths=outcome.changed_paths,
                checks=checks,
                provider=_DIRTY_WORKTREE_RECOVERY_PROVIDER,
                actual_model=None,
                **governance_receipt_fields(contract),
            )

        artifacts["patch"] = str(recovery["patch_ref"])
        if isinstance(recovery.get("dependency_input_ref"), str):
            artifacts["dependency-input"] = recovery["dependency_input_ref"]
        binding_ref = self.artifacts.put_text(
            canonical_json(
                {
                    "schema_version": 2,
                    "kind": "blocked-worktree-recovery-binding",
                    "source": {
                        "allocation_id": binding["source_allocation_id"],
                        "attempt": recovery["source_attempt"],
                        "worktree": source_worktree,
                        "branch": recovery["source_branch"],
                        "base_sha": recovery["base_sha"],
                        "input_tree_sha": recovery.get("input_tree_sha"),
                        "dependency_input_ref": recovery.get("dependency_input_ref"),
                        "patch_ref": recovery["patch_ref"],
                        "patch_sha256": recovery["patch_sha256"],
                    },
                    "target": {
                        "attempt": target_attempt,
                        "worktree": str(target),
                        "branch": target_branch,
                    },
                }
            ),
            "recovery-binding.json",
        )
        artifacts["recovery-binding"] = binding_ref
        result = NodeResult(
            status="succeeded",
            summary=f"deterministic clean-target recovery: {outcome.summary}",
            artifacts=artifacts,
            result_kind="worker",
            changed_paths=outcome.changed_paths,
            checks=checks,
            provider=_DIRTY_WORKTREE_RECOVERY_PROVIDER,
            actual_model=None,
            **governance_receipt_fields(contract),
        )
        request = ExecutionRequest(
            task_id=claimed["task_id"],
            node_id=claimed["node_id"],
            attempt=target_attempt,
            contract=contract,
            spec=spec,
            worktree=target,
            input_tree_sha=(
                recovery["input_tree_sha"]
                if isinstance(recovery.get("input_tree_sha"), str)
                else None
            ),
            input_receipt_ref=(
                recovery["dependency_input_ref"]
                if isinstance(recovery.get("dependency_input_ref"), str)
                else None
            ),
        )
        result = validate_worker_scope(self.worktrees, request, result)
        if result.status != "succeeded":
            result = replace(
                result,
                artifacts={
                    **result.artifacts,
                    "recovery-failure-archive": self._archive_failed_recovery_target(
                        contract=contract,
                        claimed=claimed,
                        target=target,
                        branch=target_branch,
                    ),
                },
            )
            return result
        # a1 is consumed only after a clean a2 holds the exact patch, passed
        # its declared offline acceptance, and passed scope validation.
        recovery_preflight = self.store.prevalidate_dirty_worktree_recovery_target(
            claimed["task_id"],
            claimed["node_id"],
            str(target),
            attempt=target_attempt,
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        self.store.assign_worktree(
            claimed["task_id"],
            claimed["node_id"],
            str(target),
            attempt=target_attempt,
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
            recovery_preflight=recovery_preflight,
        )
        return result

    def _archive_failed_recovery_target(
        self,
        *,
        contract: dict,
        claimed: dict,
        target: Path,
        branch: str,
    ) -> str:
        """Archive a2 diagnostics while releasing its deterministic retry slot."""

        archive = self.worktrees.archive_failed_recovery(
            contract["repository"],
            target,
            branch,
            task_id=claimed["task_id"],
            node_id=claimed["node_id"],
            attempt=int(claimed["attempt"]),
        )
        return self.artifacts.put_text(
            canonical_json(
                {
                    "schema_version": 1,
                    "kind": "blocked-worktree-recovery-failure-archive",
                    "task_id": claimed["task_id"],
                    "node_id": claimed["node_id"],
                    "attempt": int(claimed["attempt"]),
                    "archive": str(archive),
                }
            ),
            "recovery-failure-archive.json",
        )

    def _blocked_worktree_recovery_failure(self, claimed: dict, summary: str) -> NodeResult:
        """Produce a receipt that lets Store restore the authoritative a1."""

        binding = claimed.get("blocked_worktree_recovery")
        recovery = binding.get("recovery") if isinstance(binding, dict) else None
        patch_ref = recovery.get("patch_ref") if isinstance(recovery, dict) else None
        artifacts = (
            {"recovery-snapshot": patch_ref}
            if isinstance(patch_ref, str) and patch_ref
            else {"recovery-error": self.artifacts.put_text(summary, "blocked-worktree-recovery-error.txt")}
        )
        changed_paths = tuple(
            path
            for path in (recovery.get("changed_paths", ()) if isinstance(recovery, dict) else ())
            if isinstance(path, str)
        )
        return NodeResult(
            status="blocked",
            summary=summary,
            artifacts=artifacts,
            result_kind="worker",
            changed_paths=changed_paths,
            checks=(f"BLOCKED: {summary}",),
            provider=_DIRTY_WORKTREE_RECOVERY_PROVIDER,
            actual_model=None,
            **governance_receipt_fields(claimed["contract"]),
        )

    def _execute_codex_fallback(
        self,
        claimed: dict,
        request: ExecutionRequest,
        reason: str,
        zone: str,
        *,
        fallback_kind: str,
    ) -> tuple[ExecutionRequest, NodeResult]:
        contract = TaskContract.from_dict(request.contract)
        node_strategy = strategy_for_node(contract, request.spec)
        fallback_model = codex_fallback_model(
            contract,
            strategy=node_strategy,
            attempt=int(claimed["attempt"]),
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
                "model_profile": codex_model_profile(fallback_model),
                "model_reasoning_effort": codex_model_reasoning_effort(fallback_model),
                "zone": zone,
                "reason": reason,
                "fallback_kind": fallback_kind,
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
            spec={
                **request.spec,
                "executor": "codex",
                "model": fallback_model,
                "model_profile": codex_model_profile(fallback_model),
                "model_reasoning_effort": codex_model_reasoning_effort(fallback_model),
            },
            worktree=request.worktree,
            steering=request.steering,
            archify_receipts=request.archify_receipts,
            input_tree_sha=request.input_tree_sha,
            input_receipt=request.input_receipt,
            input_receipt_ref=request.input_receipt_ref,
        )
        return routed_request, self._executor("codex").execute(routed_request)

    def _prepare_dependency_input(
        self, task_id: str, node_id: str, worktree: Path
    ) -> DependencyInput | None:
        task = self.store.get_task(task_id)
        return apply_accepted_ancestor_patches(
            task,
            node_id,
            worktree,
            self.artifacts,
            self.worktrees,
        )

    def _compose_worker_patches(
        self, task_id: str, worktree: Path, *, node_id: str | None = None
    ) -> DependencyInput | None:
        """Compose the verifier's complete accepted worker closure exactly once."""

        task = self.store.get_task(task_id)
        target_node_id = node_id
        if target_node_id is None:
            verifier_nodes = [node for node in task.get("nodes", ()) if node.get("verifier")]
            if len(verifier_nodes) != 1 or not isinstance(verifier_nodes[0].get("node_id"), str):
                raise DependencyInputError("task lacks one verifier for dependency composition")
            target_node_id = verifier_nodes[0]["node_id"]
        return apply_accepted_ancestor_patches(
            task,
            target_node_id,
            worktree,
            self.artifacts,
            self.worktrees,
        )

    @staticmethod
    def _with_dependency_input_receipt(result: NodeResult, receipt_ref: str) -> NodeResult:
        return replace(
            result,
            artifacts={**result.artifacts, "dependency-input": receipt_ref},
        )

    def _archify_receipt_packets(self, task_id: str) -> tuple[dict, ...]:
        """Load every required Archify receipt for the final Sol verifier.

        Worker receipt artifacts are immutable ArtifactStore objects.  The
        packet preserves each role and its scope metadata so the verifier
        cannot accidentally inspect only the first normalized Archify role.
        Renderer-owning commands carry a pinned artifact-check envelope;
        ``validate`` carries a pinned command-replay envelope.  The final host
        gate sees both, so command-only evidence cannot bypass Sol review.
        ``migrate`` remains unavailable to current role contracts and is
        rejected before settlement.
        """

        task = self.store.get_task(task_id)
        packets: list[dict] = []
        for node in task["nodes"]:
            if node.get("verifier"):
                continue
            directive = node.get("archify")
            if not (
                isinstance(directive, dict)
                and directive.get("schema_version") == 1
                and directive.get("artifact_required") is True
                and isinstance(directive.get("role"), str)
            ):
                continue
            result = node.get("result")
            artifacts = result.get("artifacts") if isinstance(result, dict) else None
            receipt_ref = artifacts.get("archify-receipt") if isinstance(artifacts, dict) else None
            execution_ref = artifacts.get("archify-execution") if isinstance(artifacts, dict) else None
            if node.get("state") != "accepted" or not isinstance(receipt_ref, str):
                raise ValueError(f"accepted Archify worker {node['node_id']} lacks validated receipt evidence")
            if not isinstance(node.get("worktree"), str) or not node["worktree"]:
                raise ValueError(f"accepted Archify worker {node['node_id']} lacks a worktree")
            try:
                receipt = json.loads(self.artifacts.verify(receipt_ref).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"accepted Archify worker {node['node_id']} has unreadable receipt evidence: {error}"
                ) from error
            if not isinstance(receipt, dict):
                raise ValueError(f"accepted Archify worker {node['node_id']} receipt artifact is not an object")
            command = receipt.get("command")
            if command not in _ARCHIFY_COMMANDS:
                raise ValueError(
                    f"accepted Archify worker {node['node_id']} has unsupported receipt command {command!r}"
                )
            if not isinstance(execution_ref, str):
                raise ValueError(f"accepted Archify worker {node['node_id']} lacks validated receipt evidence")
            try:
                self.artifacts.verify(execution_ref)
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"accepted Archify worker {node['node_id']} has unreadable execution evidence: {error}"
                ) from error
            packets.append(
                {
                    "node_id": node["node_id"],
                    "role": directive["role"],
                    "receipt_ref": receipt_ref,
                    "execution_ref": execution_ref,
                    "receipt": receipt,
                    "worktree": node["worktree"],
                    "read_scopes": list(node.get("read_scopes", ())),
                    "write_scopes": list(node.get("write_scopes", ())),
                }
            )
        return tuple(packets)

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
                os.environ.get("CODEX_WORKBENCH_CLAUDE") or "claude",
                self.quota_ttl_seconds,
            )
        raise ValueError(f"unsupported executor {kind!r}")
