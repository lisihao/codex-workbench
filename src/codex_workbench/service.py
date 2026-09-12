from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import traceback
from typing import Callable, Iterable

from .artifacts import ArtifactStore
from .config import WorkbenchConfig
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
from .delivery_lifecycle import DeliveryLifecycleReconciler
from .dirty_worktree_recovery import (
    DirtyWorktreeRecovery,
    DirtyWorktreeRecoveryError,
    PnpmOfflineMaterializer,
    inspect_recovery_source_delta,
    partition_recovery_paths,
    summarize_recovery_paths,
)
from .execution_attribution import (
    AttributionReference,
    CandidateDecision,
    ExecutionAttribution,
    ExecutionCondition,
    ExecutionConditions,
    ExecutionStateReference,
    ExecutionTimings,
    FailureAttribution,
    IdentityProvenance,
    ObservedModelIdentity,
    PhaseTiming,
    PhysicalCallIdentity,
    RequestedModelIdentity,
    TimingBoundary,
)
from .execution_readiness import (
    ExecutionReadinessRequest,
    ExecutionReadinessReport,
    SourceResolutionRequirement,
    assess_execution_readiness,
)
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
    canonical_hash,
    canonical_json,
    codex_model_profile,
    codex_model_reasoning_effort,
    now_iso,
)
from .planner import PlannerError
from .quota import JsonFileQuotaAdapter, QuotaRefresher
from .recovery import RecoveryPolicy, WorktreeRecoveryManager
from .recovery_processes import RecoveryProcessError, assert_recovery_source_idle
from .routing import (
    ROUTING_V3_POLICY_VERSION,
    codex_fallback_model,
    route_task,
    strategy_for_node,
)
from .store import CommandConflictError, StateConflictError, WorkbenchStore
from .submission import compile_natural_language_request
from .worktrees import WorktreeError, WorktreeManager, scope_allows


_ARCHIFY_COMMANDS = frozenset({"deliver", "compare", "visual-check", "validate", "migrate"})
_DIRTY_WORKTREE_RECOVERY_PROVIDER = "workbench-dirty-worktree-recovery"
_FAILED_ATTEMPT_RECOVERY_PROVIDER = "workbench-failed-attempt-recovery"
_PLANNING_FUTURE_PREFIX = "planning/"


@dataclass(frozen=True)
class _ClaimRoute:
    """Capacity decision captured atomically with a durable node claim."""

    quota: QuotaSnapshot | None
    active_claude_models: tuple[str, ...]
    decision: ClaudeDispatchDecision | None
    # Claude admission must point to the exact immutable ledger row selected
    # by the effective-observation projection.
    quota_snapshot_id: int | None = None


@dataclass(frozen=True)
class _AdmissionWait:
    """One ready Claude node held before claim by shared capacity."""

    task_id: str
    node_id: str
    model: str
    reason_kind: str
    resume_condition: str
    quota_snapshot_id: int
    decision: ClaudeDispatchDecision


@dataclass
class _ExecutionAttributionContext:
    """Observed coordinator boundaries for one durable node attempt.

    The context holds only bounded pointers and timestamps captured by this
    coordinator turn.  It deliberately does not retain prompt text, provider
    transcripts, or an inferred physical-call identifier.
    """

    claim_event_cursor: int | None = None
    queue_finished_at: str | None = None
    prepare_started_at: str | None = None
    prepare_finished_at: str | None = None
    prepare_duration_ms: int | None = None
    execute_started_at: str | None = None
    execute_finished_at: str | None = None
    execute_duration_ms: int | None = None
    readiness_report_ref: str | None = None
    readiness_report: ExecutionReadinessReport | None = None
    dependency_materialization_ref: str | None = None
    dependency_input_ref: str | None = None
    scope_checked: bool = False
    quota_snapshot_id: int | None = None
    route_event_cursors: list[int] = field(default_factory=list)
    candidate_decisions: list[CandidateDecision] = field(default_factory=list)
    # These are captured from the current executor return before coordinator
    # receipts are added.  A retry must not promote recovery or cached Evidence
    # into direct provider identity evidence.
    direct_artifact_refs: frozenset[str] = field(default_factory=frozenset)
    failure_origin: str | None = None
    failure_detail: str | None = None
    harness_failure_ref: str | None = None


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
        pnpm_materializer: PnpmOfflineMaterializer | None = None,
        delivery_lifecycle: DeliveryLifecycleReconciler | None = None,
        fatal_exit: Callable[[int], None] | None = None,
        config: WorkbenchConfig | None = None,
    ):
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        resolved_spark_workers = min(4, max_workers) if spark_workers is None else spark_workers
        if not 0 <= resolved_spark_workers <= max_workers:
            raise ValueError("spark_workers must be between 0 and max_workers")
        self.store = store
        self.state_root = state_root
        self.max_workers = max_workers
        # The authority passes its fully loaded configuration.  Unit callers
        # and legacy construction sites retain a local, state-root-bound
        # default until they can be migrated without changing their behavior.
        self.config = config or WorkbenchConfig(state_root, max_workers=max_workers)
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
        self.blocked_worktree_recovery = DirtyWorktreeRecovery(
            self.artifacts,
            self.worktrees,
            materializer=pnpm_materializer,
        )
        # Failed retries use the same sealed capture/lineage mechanism, but
        # continue into their original executor after restoration.
        self.failed_attempt_recovery = self.blocked_worktree_recovery
        # Lifecycle adapters are authority-owned and opt-in.  The coordinator
        # never creates a real GitHub/deployment adapter from a worker task.
        # It still owns a no-adapter reconciler by default so an explicitly
        # requested endpoint becomes a durable, visible decision boundary
        # rather than an unattended pending objective.
        self.delivery_lifecycle = delivery_lifecycle or DeliveryLifecycleReconciler(
            store,
            owner_id=f"coordinator-{coordinator_epoch}",
            coordinator_epoch=coordinator_epoch,
            adapter=None,
        )
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workbench-worker")
        self._delivery_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="workbench-delivery",
        )
        self._delivery_future: Future[list[dict[str, Any]]] | None = None
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
        self.node_recovery = None
        self._node_recovery_fault: str | None = None
        self._session_notification_fault: str | None = None

    def bind_authority_service(self, authority_service) -> None:
        """Share the running Authority journal with fixed recovery adapters."""
        from .node_recovery import NodeRecoveryReconciler
        from .node_recovery_actions import JournaledNodeActions
        from .node_recovery_readiness import ReadinessNodeActions
        from .node_recovery_local import LocalNodeActions
        from .node_recovery_repair import RepairNodeActions
        from .node_recovery_source_repair import SourceRepairNodeActions
        from .node_recovery_store import NodeRecoveryStore
        from .node_recovery_deployment import observe_repair_delivery

        readiness = ReadinessNodeActions(
            self.config, self.store, readiness_request_factory=self._readiness_request,
        )
        journaled = JournaledNodeActions(self.config, self.store, authority_service)
        local = LocalNodeActions(
            self.config, self.store, readiness_request_factory=self._readiness_request,
            materializer=self.blocked_worktree_recovery.materializer,
        )
        self.node_recovery = NodeRecoveryReconciler(
            self.store, coordinator_epoch=self.coordinator_epoch,
            delivery_observer=lambda episode: observe_repair_delivery(self.store, self.config, episode),
            adapters={
                "observe_readiness": readiness, "narrow_validation": journaled,
                "source_only_recovery": journaled,
                "materialize_dependencies": local, "resume_node": local,
                "repair_source": SourceRepairNodeActions(self.store),
                "request_repair": RepairNodeActions(self.config, NodeRecoveryStore(self.store), authority_service),
            },
        )
        self.node_recovery.recovery.recover_interrupted()

    def _reconcile_authority_work(self) -> list[dict]:
        """Use the existing bounded control pool without holding worker dispatch."""
        from .session_notifications import project_notifications

        if self.node_recovery is not None:
            try:
                self.node_recovery.reconcile_once()
                self._node_recovery_fault = None
            except Exception as error:
                failure = f"{type(error).__name__}: {error}"[:512]
                if failure != self._node_recovery_fault:
                    self.store.record_system_event(
                        "node_recovery.reconcile_failed",
                        {"error": failure, "owner": "authority", "retry": "bounded-next-control-turn"},
                    )
                    self._node_recovery_fault = failure
        result = self.delivery_lifecycle.reconcile_once()
        try:
            project_notifications(self.store, limit=200)
            self._session_notification_fault = None
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"[:512]
            if failure != self._session_notification_fault:
                self.store.record_system_event(
                    "session_notification.projection_failed",
                    {"error": failure, "owner": "authority", "retry": "bounded-next-control-turn"},
                )
                self._session_notification_fault = failure
        return result

    def recover(self) -> int:
        recovered_planning = self.store.recover_interrupted_planning_requests()
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
        return recovered + recovered_planning

    def _reconcile_delivery_lifecycle(self) -> None:
        """Schedule at most one fenced delivery turn without blocking workers."""

        future = self._delivery_future
        if future is not None:
            if not future.done():
                return
            self._delivery_future = None
            try:
                future.result()
            except Exception as error:
                self.store.record_system_event(
                    "delivery_lifecycle.reconcile_failed",
                    {"error": f"{type(error).__name__}: {error}"},
                )

        try:
            self.store.reconcile_delivery_safe_point_waits()
            # GitHub CI observation can take substantially longer than a
            # normal scheduler turn.  One fenced lifecycle call may run at a
            # time, but it must never hold worker dispatch or a deployment
            # safe-point wakeup hostage.
            self._delivery_future = self._delivery_pool.submit(
                self._reconcile_authority_work,
            )
        except Exception as error:
            self.store.record_system_event(
                "delivery_lifecycle.reconcile_failed",
                {"error": f"{type(error).__name__}: {error}"},
            )

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
            self._reconcile_delivery_lifecycle()
            self._dispatch_one_planning_request()
            while len(self._futures) < self.max_workers:
                worker_counter += 1
                worker_id = f"{socket.gethostname()}-{os.getpid()}-{worker_counter}"
                quota = self._latest_quota()
                quota_snapshot_id = self._quota_snapshot_reference(quota)
                active_claude_models = self._active_claude_models()
                admission_waits: dict[tuple[str, str], _AdmissionWait] = {}

                def admissible(spec: dict) -> bool:
                    pending = self._pending_admission_decision(
                        spec,
                        quota,
                        active_claude_models,
                    )
                    if pending is None:
                        return True
                    if quota_snapshot_id is None:
                        # Claim-time turns this into a Codex fallback.  An
                        # admission wait may not rely on an unresolved
                        # effective projection.
                        return True
                    reason_kind, resume_condition, decision = pending
                    wait = _AdmissionWait(
                        task_id=str(spec["task_id"]),
                        node_id=str(spec["node_id"]),
                        model=str(spec["model"]),
                        reason_kind=reason_kind,
                        resume_condition=resume_condition,
                        quota_snapshot_id=quota_snapshot_id,
                        decision=decision,
                    )
                    admission_waits[(wait.task_id, wait.node_id)] = wait
                    return False

                # A saturated Claude pool leaves that exact node pending while
                # the store continues scanning for independent Codex work.
                claimed = self._claim_next_ready_node(
                    worker_id,
                    admissible=admissible,
                )
                self._record_admission_waits(admission_waits.values())
                if claimed is None:
                    break
                decision = (
                    None
                    if claimed.get("blocked_worktree_recovery") is not None
                    else self._claim_time_decision(
                        claimed["spec"], claimed["contract"], quota, active_claude_models
                    )
                )
                if (
                    claimed["spec"].get("executor") == "claude"
                    and decision is not None
                    and decision.action == "claude"
                    and quota_snapshot_id is None
                ):
                    decision = self._missing_quota_reference_decision(decision)
                claim_route = _ClaimRoute(
                    quota,
                    active_claude_models,
                    decision,
                    quota_snapshot_id,
                )
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
        self._delivery_pool.shutdown(wait=True, cancel_futures=False)
        # A coordinator cannot be considered stopped while its recovery
        # companion still owns work.  The authority caller waits for this
        # method's thread before releasing its lease, so preserve correctness
        # over a misleading bounded shutdown claim.
        self._recovery_thread.join()

    def _dispatch_one_planning_request(self) -> bool:
        """Claim and submit at most one durable planning request per turn.

        Planning runs in the same bounded pool as execution.  A slow model
        compile therefore cannot block the MCP response thread, nor can it
        create an unbounded second executor.  The store claim is the durable
        ownership boundary; this method never retries a failed claim locally.
        """

        if len(self._futures) >= self.max_workers or self._planning_in_flight():
            return False
        try:
            claimed = self.store.claim_planning_request(self.coordinator_epoch)
        except Exception as error:
            self._record_planning_system_event(
                "planning.claim_failed",
                {"error": self._planning_error_text(error)},
            )
            return False
        if claimed is None:
            return False
        command_id, attempt, coordinator_epoch = self._planning_claim_values(claimed)
        try:
            command_id, attempt, coordinator_epoch = self._planning_claim_identity(claimed)
        except (KeyError, TypeError, ValueError) as error:
            detail = "planning claim invalid: " + self._planning_error_text(error)
            if command_id is not None and attempt is not None and coordinator_epoch is not None:
                # The durable claim still has a complete fencing identity, so
                # turn corruption into an observable failed receipt now.
                self._fail_planning_request(
                    command_id,
                    attempt,
                    coordinator_epoch,
                    detail,
                )
            else:
                # Without all three fence fields we cannot safely settle the
                # row.  Leave it running for startup recovery to mark
                # indeterminate instead of guessing at a task side effect.
                self._record_planning_system_event(
                    "planning.claim_invalid",
                    {
                        "error": detail,
                        "command_id": command_id,
                        "attempt": attempt,
                        "coordinator_epoch": coordinator_epoch,
                    },
                )
            return False
        try:
            future = self._pool.submit(self._execute_planning_request, claimed)
        except Exception as error:
            self._fail_planning_request(
                command_id,
                attempt,
                coordinator_epoch,
                "planning background submission failed: " + self._planning_error_text(error),
            )
            return False
        self._futures[future] = (f"{_PLANNING_FUTURE_PREFIX}{command_id}", None)
        return True

    def _planning_in_flight(self) -> bool:
        """Return whether the shared pool already owns one planning attempt."""

        return any(
            label.startswith(_PLANNING_FUTURE_PREFIX)
            for label, _model in self._futures.values()
        )

    @staticmethod
    def _planning_claim_values(
        claimed: object,
    ) -> tuple[str | None, int | None, int | None]:
        """Read only individually valid durable claim fence fields."""

        if not isinstance(claimed, dict):
            return None, None, None
        raw_command_id = claimed.get("command_id")
        command_id = (
            raw_command_id
            if isinstance(raw_command_id, str) and raw_command_id.strip()
            else None
        )
        raw_attempt = claimed.get("attempt")
        attempt = (
            raw_attempt
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool) and raw_attempt >= 1
            else None
        )
        raw_epoch = claimed.get("coordinator_epoch")
        coordinator_epoch = (
            raw_epoch
            if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 1
            else None
        )
        return command_id, attempt, coordinator_epoch

    @classmethod
    def _planning_claim_identity(cls, claimed: object) -> tuple[str, int, int]:
        """Return the complete claim fence or reject a malformed claim."""

        if not isinstance(claimed, dict):
            raise ValueError("planning claim must be an object")
        command_id, attempt, coordinator_epoch = cls._planning_claim_values(claimed)
        if command_id is None:
            raise ValueError("planning claim command_id is required")
        if attempt is None:
            raise ValueError("planning claim attempt must be a positive integer")
        if coordinator_epoch is None:
            raise ValueError("planning claim coordinator_epoch must be a positive integer")
        return command_id, attempt, coordinator_epoch

    @staticmethod
    def _planning_error_text(error: Exception) -> str:
        return f"{type(error).__name__}: {error}"

    def _record_planning_system_event(self, event_type: str, payload: dict[str, object]) -> None:
        """Best-effort diagnostics must not turn a planning failure into exit."""

        try:
            self.store.record_system_event(event_type, payload)
        except Exception:
            # The attempt finalizer still owns durable state.  A secondary
            # diagnostics write cannot be allowed to kill the coordinator.
            pass

    def _execute_planning_request(self, claimed: dict) -> None:
        """Compile one claimed request and durably finalize its exact attempt.

        All expected planner, process, validation, and unexpected Python
        errors become a failed planning receipt.  A stale completion is
        intentionally ignored: the store fences it by command, attempt, and
        coordinator epoch, so an old worker cannot overwrite a newer owner.
        """

        command_id: str | None = None
        attempt: int | None = None
        coordinator_epoch: int | None = None
        try:
            command_id, attempt, coordinator_epoch = self._planning_claim_identity(claimed)
            request = claimed["request"]
            if not isinstance(request, dict):
                raise ValueError("planning claim request must be an object")
            submit_request = dict(request)
            request_schema = submit_request.pop("request_schema", None)
            if request_schema is not None and request_schema != "natural-language-planning-v1":
                raise ValueError(f"unsupported planning request schema {request_schema!r}")
            queue = submit_request.get("queue")
            if not isinstance(queue, bool):
                raise ValueError("planning request queue must be a boolean")
            source_thread_id = submit_request.get("source_thread_id")
            context_ref = submit_request.get("context_bundle_ref")
            if source_thread_id is None and context_ref is None:
                context_excerpt = None
            elif isinstance(source_thread_id, str) and isinstance(context_ref, str):
                frozen_context = self.store.get_session_context(source_thread_id, context_ref)
                if (
                    frozen_context.get("source_thread_id") != source_thread_id
                    or frozen_context.get("context_ref") != context_ref
                ):
                    raise ValueError("frozen planning context identity did not match the request")
                context_excerpt = frozen_context.get("context_excerpt")
                if not isinstance(context_excerpt, str):
                    raise ValueError("frozen planning context excerpt is invalid")
            else:
                raise ValueError(
                    "planning source_thread_id and context_bundle_ref must be supplied together"
                )
            # The planning ledger deliberately contains references rather than
            # a second plaintext history copy.  Compile receives only the
            # exact, content-addressed receipt selected by the claimed row.
            submit_request["context_excerpt"] = context_excerpt
            if isinstance(claimed.get("previous_error"), str):
                submit_request["planning_feedback"] = claimed["previous_error"]
            compiled = compile_natural_language_request(
                self.config,
                self.store,
                **submit_request,
            )
            if compiled.command_id != command_id:
                raise ValueError("compiled planning command_id did not match the claimed request")
            if compiled.contract.task_id != claimed.get("task_id"):
                raise ValueError("compiled planning task_id did not match the claimed request")
        except (CommandConflictError, KeyError, PlannerError, OSError, subprocess.SubprocessError, ValueError) as error:
            self._fail_planning_request(
                command_id,
                attempt,
                coordinator_epoch,
                self._planning_error_text(error),
            )
            return
        except Exception as error:
            self._fail_planning_request(
                command_id,
                attempt,
                coordinator_epoch,
                self._planning_error_text(error),
            )
            return
        try:
            # This is the only successful completion path.  The store fences
            # the epoch and atomically creates the task, queues it, binds its
            # session, and settles the planning receipt.
            self.store.materialize_planning_request(
                command_id,
                attempt=attempt,
                coordinator_epoch=coordinator_epoch,
                contract=compiled.contract,
                nodes=list(compiled.nodes),
                result=compiled.result,
                queue=queue,
                source_thread_id=source_thread_id,
            )
        except StateConflictError:
            # A newer authority owns the ledger.  materialize executes all
            # side effects in its transaction, so fencing here means none of
            # task creation, queueing, or session binding was applied.
            return
        except (CommandConflictError, KeyError, OSError, ValueError) as error:
            self._fail_planning_request(
                command_id,
                attempt,
                coordinator_epoch,
                "planning materialization failed: " + self._planning_error_text(error),
            )
        except Exception as error:
            self._fail_planning_request(
                command_id,
                attempt,
                coordinator_epoch,
                "planning materialization failed: " + self._planning_error_text(error),
            )

    def _fail_planning_request(
        self,
        command_id: str | None,
        attempt: int | None,
        coordinator_epoch: int | None,
        error: str,
    ) -> None:
        """Persist one bounded failure, without allowing it to exit the process."""

        if command_id is None or attempt is None or coordinator_epoch is None:
            self._record_planning_system_event(
                "planning.finalize_invalid",
                {"state": "failed", "command_id": command_id, "error": error},
            )
            return
        try:
            self.store.fail_planning_request(
                command_id,
                attempt=attempt,
                coordinator_epoch=coordinator_epoch,
                error=error,
            )
        except StateConflictError:
            return
        except Exception as persistence_error:
            self._record_planning_system_event(
                "planning.failure_persist_failed",
                {
                    "command_id": command_id,
                    "attempt": attempt,
                    "coordinator_epoch": coordinator_epoch,
                    "error": error,
                    "persistence_error": self._planning_error_text(persistence_error),
                },
            )

    def _claim_next_ready_node(
        self,
        worker_id: str,
        *,
        admissible: Callable[[dict], bool] | None = None,
    ) -> dict | None:
        """Prefer ready, admissible Spark work without reserving idle threads.

        The store re-applies task priority, dependency, parallelizability,
        scope-conflict, authority, and lane-capacity checks for both attempts.
        General/control work borrows every slot Spark cannot currently use.
        """

        if self.spark_workers:
            spark = self.store.claim_ready_node(
                worker_id,
                self.coordinator_epoch,
                admissible=admissible,
                execution_lanes=("spark",),
                lane_capacities=self._lane_capacities,
            )
            if spark is not None:
                return spark
        return self.store.claim_ready_node(
            worker_id,
            self.coordinator_epoch,
            admissible=admissible,
            execution_lanes=("general", "control"),
            lane_capacities=self._lane_capacities,
        )

    def _pending_admission_decision(
        self,
        spec: dict,
        quota: QuotaSnapshot | None,
        active_models: tuple[str, ...],
    ) -> tuple[str, str, ClaudeDispatchDecision] | None:
        """Return transient waits; authentication and reserve refusals fallback."""

        if spec.get("executor") != "claude" or quota is None:
            return None
        shared_capacity = (
            spec.get("routing_strategy") != LEGACY_ROUTING_STRATEGY_VERSION
        )
        decision = quota.dispatch_decision(
            str(spec["model"]),
            active_models,
            max_age_seconds=self.quota_ttl_seconds,
            shared_capacity=shared_capacity,
        )
        if decision.action == "defer":
            return (
                "temporary-capacity",
                "a fresh admitted Claude quota snapshot reports enough shared capacity "
                "for the requested units",
                decision,
            )
        if decision.action != "claude":
            return None
        refresh_wait = self._quota_refresh_wait_decision(quota, decision)
        if refresh_wait is None:
            return None
        return (
            "quota-refresh-required",
            "the Claude quota snapshot is newer than the most recent Claude completion "
            "and the route remains admitted",
            refresh_wait,
        )

    def _record_admission_waits(self, waits: Iterable[_AdmissionWait]) -> None:
        for wait in waits:
            decision = wait.decision
            self.store.record_node_admission_deferred(
                wait.task_id,
                wait.node_id,
                model=wait.model,
                reason_kind=wait.reason_kind,
                quota_snapshot_id=wait.quota_snapshot_id,
                reason=decision.reason,
                zone=decision.zone,
                capacity_units=decision.capacity_units,
                active_units=decision.active_units,
                requested_units=decision.requested_units,
                available_units=decision.available_units,
                resume_condition=wait.resume_condition,
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
        if spec.get("routing_policy_version") == ROUTING_V3_POLICY_VERSION:
            return Coordinator._pinned_v3_claude_decision(
                spec,
                contract_raw,
                quota,
                active_models,
                quota_ttl_seconds=quota_ttl_seconds,
            )
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
    def _pinned_v3_claude_decision(
        spec: dict,
        contract_raw: dict,
        quota: QuotaSnapshot | None,
        active_models: tuple[str, ...],
        *,
        quota_ttl_seconds: int,
    ) -> ClaudeDispatchDecision:
        """Recheck only runtime admission for a frozen routing-v3 Claude node.

        The planner has already selected the provider and capability against an
        immutable catalog.  Re-running the legacy strategy here could silently
        replace that selection at claim time, so this path verifies the two
        durable catalog bindings and delegates only freshness, reserve, and
        shared-capacity checks to the quota receipt.
        """

        spec_snapshot_id = spec.get("capability_snapshot_id")
        spec_digest = spec.get("capability_digest")
        contract_snapshot_id = contract_raw.get("capability_snapshot_id")
        contract_digest = contract_raw.get("capability_digest")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                spec_snapshot_id,
                spec_digest,
                contract_snapshot_id,
                contract_digest,
            )
        ):
            return ClaudeDispatchDecision(
                "codex",
                "unknown",
                "routing-v3 Claude node is missing its pinned capability binding",
                0,
            )
        if (
            spec_snapshot_id != contract_snapshot_id
            or spec_digest != contract_digest
        ):
            return ClaudeDispatchDecision(
                "codex",
                "unknown",
                "routing-v3 Claude node capability binding does not match its task contract",
                0,
            )
        if quota is None:
            return ClaudeDispatchDecision(
                "codex",
                "unknown",
                "Claude quota is unknown",
                0,
            )
        return quota.dispatch_decision(
            str(spec["model"]),
            active_models,
            max_age_seconds=quota_ttl_seconds,
            shared_capacity=True,
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

    def _quota_refresh_wait_decision(
        self,
        quota: QuotaSnapshot,
        admitted: ClaudeDispatchDecision,
    ) -> ClaudeDispatchDecision | None:
        """Require post-completion quota evidence without changing providers."""

        last_settled_at = self._latest_completed_claude_at()
        if last_settled_at is None:
            return None
        observed_at = self._timestamp(quota.observed_at)
        if observed_at is not None and observed_at > last_settled_at:
            return None
        observed_text = quota.observed_at if observed_at is not None else "invalid"
        return ClaudeDispatchDecision(
            "defer",
            "unknown",
            (
                "Claude quota snapshot must be newer than the most recent "
                f"Claude completion ({last_settled_at.isoformat()}); "
                f"observed_at={observed_text}"
            ),
            admitted.max_concurrency,
            capacity_units=admitted.capacity_units,
            active_units=admitted.active_units,
            requested_units=admitted.requested_units,
            available_units=admitted.available_units,
        )

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
        wait = self._quota_refresh_wait_decision(quota, decision)
        if wait is None:
            return decision
        # The pre-claim admission filter normally keeps this node pending. If
        # a race still reaches claim-time, preserve the established safe Codex
        # fallback rather than leaving a running lease unowned.
        return ClaudeDispatchDecision(
            "codex",
            wait.zone,
            wait.reason,
            0,
            capacity_units=wait.capacity_units,
            active_units=wait.active_units,
            requested_units=wait.requested_units,
            available_units=wait.available_units,
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
        # Re-evaluate immediately before the provider boundary.  Equality of
        # projected pool values is insufficient: a newer raw row can change
        # recovery ordering or auth/collection diagnostics without changing
        # the selected complete snapshot's fields.
        latest = self._latest_quota()
        decision = self._claude_decision(
            spec,
            contract,
            latest,
            claim_route.active_claude_models,
            quota_ttl_seconds=self.quota_ttl_seconds,
        )
        if decision is not None:
            if decision.action == "codex":
                return decision
            if decision.action == "claude" and self._quota_snapshot_reference(latest) is None:
                return self._missing_quota_reference_decision(decision)
        return None

    @staticmethod
    def _bounded_attribution_text(value: object, *, limit: int = 512) -> str:
        """Keep coordinator-derived details inside the attribution contract bounds."""

        return " ".join(str(value).split())[:limit] or "no additional detail was observed"

    @staticmethod
    def _event_reference(event_type: str, cursor: int | None) -> AttributionReference | None:
        if cursor is None:
            return None
        return AttributionReference(kind="event", ref=event_type, cursor=cursor)

    @staticmethod
    def _artifact_reference(kind: str, ref: str | None) -> AttributionReference | None:
        if not isinstance(ref, str) or not ref:
            return None
        return AttributionReference(kind=kind, ref=ref)

    def _execution_attribution_context(self, claimed: dict) -> _ExecutionAttributionContext:
        cursor = claimed.get("started_event_cursor")
        return _ExecutionAttributionContext(
            claim_event_cursor=cursor if isinstance(cursor, int) and cursor > 0 else None,
            queue_finished_at=(
                claimed.get("started_at")
                if isinstance(claimed.get("started_at"), str)
                else None
            ),
        )

    @staticmethod
    def _readiness_request(worktree: Path) -> ExecutionReadinessRequest:
        """Build explicit, local-only readiness requirements for this target.

        A generic task does not implicitly become a Node or Python task.  A
        pnpm project is recognized by the contract checker itself, while each
        conventional ``src/<package>/__init__.py`` package is resolved from the
        allocated worktree using the checker's isolated, no-import probe.
        """

        source_root = worktree / "src"
        source_resolutions: list[SourceResolutionRequirement] = []
        try:
            children = sorted(source_root.iterdir(), key=lambda path: path.name)
        except OSError:
            children = []
        for child in children:
            if (
                child.is_dir()
                and child.name.isidentifier()
                and (child / "__init__.py").is_file()
            ):
                source_resolutions.append(
                    SourceResolutionRequirement(child.name, ("src",))
                )
        return ExecutionReadinessRequest(
            worktree=worktree,
            source_resolutions=tuple(source_resolutions),
        )

    def _assess_readiness(
        self,
        worktree: Path,
        context: _ExecutionAttributionContext,
    ) -> ExecutionReadinessReport:
        report = assess_execution_readiness(self._readiness_request(worktree))
        report_ref = self.artifacts.put_text(
            canonical_json(report.to_dict()),
            "execution-readiness.json",
        )
        context.readiness_report = report
        context.readiness_report_ref = report_ref
        return report

    def _materialize_worktree_dependencies(
        self,
        worktree: Path,
        context: _ExecutionAttributionContext,
        *,
        timeout_seconds: int,
        require_cached_template: bool = False,
    ) -> str:
        """Materialize local dependencies before readiness or executor work.

        Dependency materialization is filesystem and subprocess IO, so it is
        deliberately performed by the coordinator outside any store
        transaction.  The receipt is content-addressed even when materialize
        reports a non-Node worktree, making the preparation outcome explicit
        in the node result.
        """

        materializer = self.blocked_worktree_recovery.materializer
        try:
            materialization = materializer.materialize(
                worktree,
                timeout_seconds=min(
                    timeout_seconds,
                    PnpmOfflineMaterializer.MAX_TEMPLATE_SEED_SECONDS,
                ),
                require_cached_template=require_cached_template,
            )
        except DirtyWorktreeRecoveryError as error:
            materialization = {
                "schema_version": 1,
                "kind": "pnpm-offline-materialization",
                "status": "blocked",
                "reason": str(error),
            }
            receipt_ref = self.artifacts.put_text(
                canonical_json(materialization),
                "dependency-materialization.json",
            )
            context.dependency_materialization_ref = receipt_ref
            raise
        receipt_ref = self.artifacts.put_text(
            canonical_json(materialization),
            "dependency-materialization.json",
        )
        context.dependency_materialization_ref = receipt_ref
        return receipt_ref

    @staticmethod
    def _readiness_fingerprint(report: ExecutionReadinessReport) -> dict:
        """Return stable readiness inputs for the existing Evidence fingerprint.

        Elapsed wall time is intentionally omitted: it is an observation for
        attribution, not an input that changes source/configuration/runtime
        compatibility of a cached verifier result.
        """

        payload = report.to_dict()
        payload.pop("elapsed_ms", None)
        return payload

    def _quota_snapshot_reference(self, snapshot: QuotaSnapshot | None) -> int | None:
        if not isinstance(snapshot, QuotaSnapshot):
            return None
        resolver = getattr(self.store, "quota_snapshot_reference", None)
        if not callable(resolver):
            return None
        try:
            candidate = resolver(snapshot)
        except (OSError, ValueError, StateConflictError):
            return None
        return candidate if isinstance(candidate, int) and candidate > 0 else None

    @staticmethod
    def _missing_quota_reference_decision(
        admitted: ClaudeDispatchDecision,
    ) -> ClaudeDispatchDecision:
        """Fail closed when quota admission is not tied to a ledger row."""

        return ClaudeDispatchDecision(
            "codex",
            "unknown",
            "Claude quota evidence has no durable snapshot reference",
            0,
            capacity_units=admitted.capacity_units,
            active_units=admitted.active_units,
            requested_units=admitted.requested_units,
            available_units=admitted.available_units,
        )

    def _latest_quota(self) -> QuotaSnapshot | None:
        """Read effective quota under this coordinator's existing TTL policy."""

        return self.store.latest_quota(max_age_seconds=self.quota_ttl_seconds)

    def _candidate_decision(
        self,
        spec: dict,
        *,
        disposition: str,
        reason: str,
        event_cursor: int | None,
        event_type: str = "node.started",
    ) -> CandidateDecision:
        executor = str(spec.get("executor") or "unknown")
        model = spec.get("model")
        model_id = str(model) if isinstance(model, str) and model else None
        references: list[AttributionReference] = []
        event_reference = self._event_reference(event_type, event_cursor)
        if event_reference is not None:
            references.append(event_reference)
        snapshot_id = spec.get("capability_snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            references.append(AttributionReference(kind="snapshot", ref=snapshot_id))
        reasons = [self._bounded_attribution_text(reason)]
        policy = spec.get("routing_policy_version") or spec.get("routing_strategy")
        if isinstance(policy, str) and policy:
            reasons.append(self._bounded_attribution_text(f"routing policy {policy} governed this route"))
        return CandidateDecision(
            candidate_id=f"{executor}:{model_id or 'unknown'}",
            disposition=disposition,  # type: ignore[arg-type]
            reasons=tuple(reasons),
            provider=executor if executor != "unknown" else None,
            model_id=model_id,
            references=tuple(references),
        )

    def _ensure_selected_candidate(
        self,
        context: _ExecutionAttributionContext,
        spec: dict,
        *,
        reason: str,
    ) -> None:
        executor = str(spec.get("executor") or "unknown")
        model = str(spec.get("model") or "unknown")
        candidate_id = f"{executor}:{model}"
        if any(
            candidate.candidate_id == candidate_id and candidate.disposition == "selected"
            for candidate in context.candidate_decisions
        ):
            return
        context.candidate_decisions.append(
            self._candidate_decision(
                spec,
                disposition="selected",
                reason=reason,
                event_cursor=context.claim_event_cursor,
            )
        )

    def _readiness_failure_result(
        self,
        *,
        spec: dict,
        contract: dict,
        report: ExecutionReadinessReport,
        context: _ExecutionAttributionContext,
    ) -> NodeResult:
        context.failure_origin = "environment"
        context.failure_detail = report.summary
        checks = tuple(
            f"BLOCKED: readiness {failure.check_id} ({failure.code})"
            for failure in report.failures
        ) or ("BLOCKED: execution readiness did not produce a passing report",)
        verifier = bool(spec.get("verifier"))
        return NodeResult(
            status="blocked",
            summary=report.summary,
            artifacts={"execution-readiness": str(context.readiness_report_ref)},
            result_kind="verifier" if verifier else "worker",
            changed_paths=(),
            checks=checks,
            verdict="blocked" if verifier else None,
            **governance_receipt_fields(contract),
        )

    def _validated_executor_attribution(
        self,
        result: NodeResult,
        *,
        claimed: dict,
        request: ExecutionRequest | None,
        context: _ExecutionAttributionContext,
    ) -> ExecutionAttribution | None:
        """Return direct executor evidence only when it belongs to this call.

        An executor may provide a fully typed attribution record, but the
        coordinator must not turn an arbitrary flag, a previous attempt, or a
        routing/legacy field into attestation.  Retained direct identities
        therefore need a contract-valid record for this exact durable attempt,
        compatible effective/result providers, and a direct artifact reference
        returned by this executor invocation rather than recovery or cache
        enrichment.
        """

        raw = result.execution_attribution
        if raw is None:
            return None
        try:
            supplied = ExecutionAttribution.from_dict(raw)
        except (TypeError, ValueError):
            return None

        state = supplied.state
        if (state.task_id, state.node_id, state.attempt) != (
            str(claimed["task_id"]),
            str(claimed["node_id"]),
            int(claimed["attempt"]),
        ):
            return None
        if (
            state.event_cursor is not None
            and context.claim_event_cursor is not None
            and state.event_cursor != context.claim_event_cursor
        ):
            return None

        result_provider = (
            result.provider
            if isinstance(result.provider, str) and result.provider
            else None
        )
        effective_provider = None
        if request is not None:
            candidate = request.spec.get("executor")
            effective_provider = candidate if isinstance(candidate, str) and candidate else None
        if effective_provider is None:
            candidate = claimed["spec"].get("executor")
            effective_provider = candidate if isinstance(candidate, str) and candidate else None

        direct_artifacts = context.direct_artifact_refs
        for identity in (supplied.observed_model, supplied.physical_call):
            if identity.status != "attested":
                continue
            if (
                identity.provider is not None
                and result_provider is not None
                and identity.provider != result_provider
            ):
                return None
            if (
                identity.provider is not None
                and effective_provider not in {None, "fixture"}
                and identity.provider != effective_provider
            ):
                return None
            artifact_references = tuple(
                reference
                for reference in identity.provenance.references
                if reference.kind == "artifact"
            )
            if (
                not artifact_references
                or any(reference.ref not in direct_artifacts for reference in artifact_references)
            ):
                return None
        if (
            supplied.observed_model.status == "attested"
            and isinstance(result.actual_model, str)
            and result.actual_model
            and supplied.observed_model.model_id != result.actual_model
        ):
            return None
        return supplied

    @staticmethod
    def _direct_executor_artifact_refs(result: NodeResult) -> frozenset[str]:
        """Freeze only the refs returned by this executor invocation."""

        return frozenset(
            ref for ref in result.artifacts.values() if isinstance(ref, str) and ref
        )

    @staticmethod
    def _with_failed_attempt_recovery_artifacts(
        result: NodeResult,
        recovery_artifacts: dict[str, str],
    ) -> NodeResult:
        """Retain recovery provenance without permitting a retry to overwrite it."""

        overlaps = sorted(set(result.artifacts) & set(recovery_artifacts))
        if overlaps:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt recovery artifacts conflict with executor artifacts: "
                + ", ".join(overlaps)
            )
        return replace(
            result,
            artifacts={**result.artifacts, **recovery_artifacts},
        )

    def _observed_model_identity(
        self,
        result: NodeResult,
        supplied: ExecutionAttribution | None,
    ) -> ObservedModelIdentity:
        """Preserve validated direct evidence; legacy fields stay unattested."""

        if supplied is not None:
            return supplied.observed_model
        model = result.actual_model
        provider = result.provider
        if isinstance(model, str) and model:
            # Older integrations and fixture stubs may still populate the
            # legacy compatibility field.  Preserve that audit fact without
            # promoting it to an independently attested model identity.
            return ObservedModelIdentity.unattested(
                provider=provider if isinstance(provider, str) and provider else None,
                model_id=model,
                provenance=IdentityProvenance(source="legacy_result"),
            )
        return ObservedModelIdentity.unknown()

    def _failure_origin(
        self,
        result: NodeResult,
        *,
        verifier: bool,
        context: _ExecutionAttributionContext,
        supplied: ExecutionAttribution | None = None,
    ) -> str:
        if result.status == "succeeded":
            return "unknown"
        if context.failure_origin is not None:
            return context.failure_origin
        if supplied is not None:
            return supplied.failure.origin
        if verifier and result.status == "failed":
            return "verification"
        # A worker-declared failure with a direct structured response is a
        # model outcome. Other failures remain explicitly unknown rather than
        # being guessed from a free-form summary.
        if (
            result.status == "failed"
            and result.provider in {"claude", "codex"}
            and "structured-result" in result.artifacts
        ):
            return "model"
        return "unknown"

    def _pre_execution_harness_failure_ref(
        self,
        claimed: dict,
        error: Exception,
        context: _ExecutionAttributionContext,
    ) -> str | None:
        """Persist bounded evidence for one internal pre-execution harness fault."""

        if context.execute_started_at is not None or not isinstance(
            error, (NameError, AttributeError, AssertionError, TypeError)
        ):
            return None
        frames = traceback.extract_tb(error.__traceback__)
        if not frames:
            return None
        deepest = Path(frames[-1].filename).expanduser().resolve(strict=False)
        package_root = Path(__file__).resolve().parent
        try:
            relative_module = deepest.relative_to(package_root).as_posix()
        except ValueError:
            return None
        evidence = {
            "kind": "harness-failure",
            "task_id": str(claimed["task_id"]),
            "node_id": str(claimed["node_id"]),
            "attempt": int(claimed["attempt"]),
            "phase": "pre_execution",
            "error_type": type(error).__name__,
            "relative_module": relative_module,
            "line": int(frames[-1].lineno),
            "executor_started": False,
        }
        reference = self.artifacts.put_text(canonical_json(evidence), "harness-failure.json")
        context.harness_failure_ref = reference
        return reference

    def _attach_execution_attribution(
        self,
        *,
        claimed: dict,
        request: ExecutionRequest | None,
        result: NodeResult,
        context: _ExecutionAttributionContext,
    ) -> NodeResult:
        spec = request.spec if request is not None else claimed["spec"]
        supplied = self._validated_executor_attribution(
            result,
            claimed=claimed,
            request=request,
            context=context,
        )
        verifier = bool(spec.get("verifier"))
        self._ensure_selected_candidate(
            context,
            spec,
            reason="the durable coordinator claim selected this effective route",
        )
        claim_reference = self._event_reference("node.started", context.claim_event_cursor)
        requested_model = result.requested_model
        if not isinstance(requested_model, str) or not requested_model:
            raw_model = spec.get("model")
            requested_model = raw_model if isinstance(raw_model, str) and raw_model else None
        requested_provider = result.provider
        if not isinstance(requested_provider, str) or not requested_provider:
            raw_provider = spec.get("executor")
            requested_provider = raw_provider if isinstance(raw_provider, str) else None
        requested = RequestedModelIdentity(
            provider=requested_provider,
            model_id=requested_model,
            provenance=IdentityProvenance(
                source="routing_decision",
                references=(claim_reference,) if claim_reference is not None else (),
            ),
        )

        readiness_reference = self._artifact_reference(
            "readiness_report", context.readiness_report_ref
        )
        dependency_reference = self._artifact_reference(
            "dependency_report", context.dependency_input_ref
        )
        scope_reference = AttributionReference(
            kind="scope_contract",
            ref=f"task:{claimed['task_id']}",
        )
        quota_reference = (
            AttributionReference(
                kind="quota_snapshot",
                ref=f"claude:{context.quota_snapshot_id}",
            )
            if context.quota_snapshot_id is not None
            else self._event_reference(
                "node.routed",
                context.route_event_cursors[-1] if context.route_event_cursors else None,
            )
        )
        conditions = ExecutionConditions(
            dependency=(
                ExecutionCondition(
                    kind="dependency",
                    status="observed",
                    detail="accepted dependency input was materialized for this attempt",
                    references=(dependency_reference,),
                )
                if dependency_reference is not None
                else ExecutionCondition.unknown("dependency")
            ),
            scope=(
                ExecutionCondition(
                    kind="scope",
                    status="observed",
                    detail="worker output was checked against the durable scope contract",
                    references=(scope_reference,),
                )
                if context.scope_checked
                else ExecutionCondition.unknown("scope")
            ),
            provider_quota=(
                ExecutionCondition(
                    kind="provider_quota",
                    status="observed",
                    detail="provider admission evidence is referenced from durable state",
                    references=(quota_reference,),
                )
                if quota_reference is not None
                else ExecutionCondition.unknown("provider_quota")
            ),
            environment_readiness=(
                ExecutionCondition(
                    kind="environment_readiness",
                    status="observed",
                    detail=(
                        "bounded target-worktree readiness passed"
                        if context.readiness_report is not None and context.readiness_report.ready
                        else "bounded target-worktree readiness reported an environment failure"
                    ),
                    references=(readiness_reference,),
                )
                if readiness_reference is not None
                else ExecutionCondition.unknown("environment_readiness")
            ),
        )
        queue = PhaseTiming(
            finished=TimingBoundary(context.queue_finished_at),
        )
        prepare = PhaseTiming(
            started=TimingBoundary(context.prepare_started_at),
            finished=TimingBoundary(context.prepare_finished_at),
            duration_ms=context.prepare_duration_ms,
        )
        execution_phase = PhaseTiming(
            started=TimingBoundary(context.execute_started_at),
            finished=TimingBoundary(context.execute_finished_at),
            duration_ms=context.execute_duration_ms,
        )
        timings = ExecutionTimings(
            queue=queue,
            prepare=prepare,
            execute=PhaseTiming() if verifier else execution_phase,
            verify=execution_phase if verifier else PhaseTiming(),
        )

        failure_origin = self._failure_origin(
            result, verifier=verifier, context=context, supplied=supplied,
        )
        failure_references: list[AttributionReference] = []
        if result.status != "succeeded":
            if context.failure_origin is None and supplied is not None:
                failure_references.extend(supplied.failure.references)
            elif failure_origin == "environment":
                if readiness_reference is not None:
                    failure_references.append(readiness_reference)
                materialization_reference = self._artifact_reference(
                    "artifact", context.dependency_materialization_ref,
                )
                if materialization_reference is not None:
                    failure_references.append(materialization_reference)
                if dependency_reference is not None:
                    failure_references.append(dependency_reference)
            elif failure_origin == "scope":
                failure_references.append(scope_reference)
            elif failure_origin == "quota" and quota_reference is not None:
                failure_references.append(quota_reference)
            elif failure_origin == "tooling_bug" and context.harness_failure_ref is not None:
                failure_reference = self._artifact_reference(
                    "artifact", context.harness_failure_ref
                )
                if failure_reference is not None:
                    failure_references.append(failure_reference)
            else:
                structured_reference = self._artifact_reference(
                    "artifact", result.artifacts.get("structured-result")
                )
                if structured_reference is not None:
                    failure_references.append(structured_reference)
                elif claim_reference is not None:
                    failure_references.append(claim_reference)
        failure_detail = context.failure_detail or (
            supplied.failure.detail if supplied is not None else result.summary
        )
        observed_model = self._observed_model_identity(result, supplied)
        attribution = ExecutionAttribution(
            state=ExecutionStateReference(
                task_id=str(claimed["task_id"]),
                node_id=str(claimed["node_id"]),
                attempt=int(claimed["attempt"]),
                event_cursor=context.claim_event_cursor,
            ),
            requested_model=requested,
            observed_model=observed_model,
            physical_call=(
                supplied.physical_call
                if supplied is not None
                else PhysicalCallIdentity.unknown()
            ),
            candidate_decisions=tuple(context.candidate_decisions),
            failure=FailureAttribution(
                origin=failure_origin,  # type: ignore[arg-type]
                detail=(
                    self._bounded_attribution_text(failure_detail)
                    if result.status != "succeeded"
                    else None
                ),
                references=tuple(failure_references),
            ),
            conditions=conditions,
            timings=timings,
            references=(scope_reference,),
        )
        return replace(
            result,
            actual_model=(
                observed_model.model_id
                if observed_model.status == "attested" and result.actual_model is None
                else result.actual_model
            ),
            execution_attribution=attribution.to_dict(),
        )

    def _execute_claimed(
        self,
        claimed: dict,
        claim_route: _ClaimRoute | None = None,
    ) -> None:
        if claimed.get("blocked_worktree_recovery") is not None:
            self._execute_blocked_worktree_recovery(claimed)
            return
        context = self._execution_attribution_context(claimed)
        request: ExecutionRequest | None = None
        failed_attempt_recovery = claimed.get("failed_attempt_recovery")
        accepted_source_repair = claimed.get("accepted_source_repair")
        failed_attempt_assigned = False
        recovery_artifacts: dict[str, str] = {}
        try:
            spec = claimed["spec"]
            contract = claimed["contract"]
            worktree: Path | None = None
            dependency_input: DependencyInput | None = None
            input_receipt_ref: str | None = None
            prepare_started_monotonic = time.monotonic()
            context.prepare_started_at = now_iso()
            if accepted_source_repair is not None:
                from .accepted_source_repair import prepare_accepted_source_repair

                prepared = prepare_accepted_source_repair(
                    self.store, accepted_source_repair, self.worktrees,
                )
                worktree = prepared.worktree
                dependency_input = prepared.dependency_input
                prepared_ref = self.artifacts.put_text(
                    canonical_json(prepared.receipt), "accepted-source-repair.json",
                )
                self.store.assign_worktree(
                    claimed["task_id"], claimed["node_id"], str(worktree),
                    attempt=claimed["attempt"], coordinator_epoch=claimed["coordinator_epoch"],
                    lease_epoch=claimed["lease_epoch"], recovery_preflight=prepared.receipt,
                )
                recovery_artifacts["accepted-source-repair"] = prepared_ref
                self._materialize_worktree_dependencies(
                    worktree, context, timeout_seconds=int(contract["timeout_seconds"]),
                )
            elif failed_attempt_recovery is not None:
                (
                    worktree,
                    dependency_input,
                    input_receipt_ref,
                    recovery_artifacts,
                ) = self._prepare_failed_attempt_recovery(claimed, context)
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
                self._materialize_worktree_dependencies(
                    worktree,
                    context,
                    timeout_seconds=int(contract["timeout_seconds"]),
                )
            if input_receipt_ref is None and dependency_input is not None:
                input_receipt_ref = self.artifacts.put_text(
                    canonical_json(dependency_input.receipt), "dependency-input.json"
                )
            context.dependency_input_ref = input_receipt_ref
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
            readiness_worktree = worktree or Path(contract["repository"])
            readiness = self._assess_readiness(readiness_worktree, context)
            context.prepare_finished_at = now_iso()
            context.prepare_duration_ms = max(
                0,
                int((time.monotonic() - prepare_started_monotonic) * 1_000),
            )
            if not readiness.ready:
                result = self._readiness_failure_result(
                    spec=spec,
                    contract=contract,
                    report=readiness,
                    context=context,
                )
                if input_receipt_ref is not None:
                    result = self._with_dependency_input_receipt(result, input_receipt_ref)
                if context.dependency_materialization_ref is not None:
                    result = self._with_dependency_materialization_receipt(
                        result,
                        context.dependency_materialization_ref,
                    )
                if recovery_artifacts:
                    result = self._with_failed_attempt_recovery_artifacts(
                        result,
                        recovery_artifacts,
                    )
                    recovery_artifacts = {}
                result = self._attach_execution_attribution(
                    claimed=claimed,
                    request=request,
                    result=result,
                    context=context,
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
                    pass
                return
            cache_spec = {
                **effective_spec_with_dependency_input(spec, dependency_input),
                "execution_readiness": self._readiness_fingerprint(readiness),
            }
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
                        result = self._with_failed_attempt_recovery_artifacts(
                            result,
                            recovery_artifacts,
                        )
                        recovery_artifacts = {}
                    result = replace(
                        result,
                        artifacts={
                            **result.artifacts,
                            "execution-readiness": str(context.readiness_report_ref),
                        },
                    )
                    if input_receipt_ref is not None:
                        result = self._with_dependency_input_receipt(result, input_receipt_ref)
                    if context.dependency_materialization_ref is not None:
                        result = self._with_dependency_materialization_receipt(
                            result,
                            context.dependency_materialization_ref,
                        )
                    if packet_refs:
                        result = replace(
                            result,
                            evidence=tuple(dict.fromkeys((*result.evidence, *packet_refs))),
                        )
                    result = self._attach_execution_attribution(
                        claimed=claimed,
                        request=request,
                        result=result,
                        context=context,
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
                quota = self._latest_quota()
                quota_snapshot_id = self._quota_snapshot_reference(quota)
                decision = self._claim_time_decision(spec, contract, quota, ())
                if (
                    spec.get("executor") == "claude"
                    and decision is not None
                    and decision.action == "claude"
                    and quota_snapshot_id is None
                ):
                    decision = self._missing_quota_reference_decision(decision)
                claim_route = _ClaimRoute(
                    quota,
                    (),
                    decision,
                    quota_snapshot_id,
                )
            if spec.get("executor") == "claude":
                # Resolve again beside the provider boundary.  The raw row is
                # immutable, but a projection with a missing/mismatched ID
                # must never authorize a Claude call.
                context.quota_snapshot_id = self._quota_snapshot_reference(claim_route.quota)
            decision = claim_route.decision
            if (
                spec.get("executor") == "claude"
                and decision is not None
                and decision.action == "claude"
                and context.quota_snapshot_id is None
            ):
                decision = self._missing_quota_reference_decision(decision)
            execute_started_monotonic = time.monotonic()
            context.execute_started_at = now_iso()
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
                    attribution_context=context,
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
                        attribution_context=context,
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
                            attribution_context=context,
                        )
                    else:
                        self._ensure_selected_candidate(
                            context,
                            request.spec,
                            reason="the selected route completed without a provider fallback",
                        )
            context.direct_artifact_refs = self._direct_executor_artifact_refs(result)
            context.execute_finished_at = now_iso()
            context.execute_duration_ms = max(
                0,
                int((time.monotonic() - execute_started_monotonic) * 1_000),
            )
            if worktree is not None and not spec.get("verifier") and result.status in {"failed", "blocked"}:
                result = self._with_observed_failure_paths(request, result)
            scope_before = result.status == "succeeded" and request.worktree is not None
            result = validate_worker_scope(self.worktrees, request, result)
            context.scope_checked = scope_before
            if scope_before and result.status == "failed":
                context.failure_origin = "scope"
                context.failure_detail = result.summary
            result = replace(
                result,
                artifacts={
                    **result.artifacts,
                    "execution-readiness": str(context.readiness_report_ref),
                },
            )
            if context.dependency_materialization_ref is not None:
                result = self._with_dependency_materialization_receipt(
                    result,
                    context.dependency_materialization_ref,
                )
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
                result = self._with_failed_attempt_recovery_artifacts(
                    result,
                    recovery_artifacts,
                )
                recovery_artifacts = {}
            if input_receipt_ref is not None:
                result = self._with_dependency_input_receipt(result, input_receipt_ref)
            if cache_key and result.status == "succeeded":
                result = self._attach_execution_attribution(
                    claimed=claimed,
                    request=request,
                    result=result,
                    context=context,
                )
                self.store.save_evidence(
                    cache_key,
                    result,
                    claimed["task_id"],
                    claimed["node_id"],
                )
        except DependencyInputError as error:
            context.failure_origin = "environment"
            context.failure_detail = f"dependency inputs unavailable: {error}"
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
        except DirtyWorktreeRecoveryError as error:
            context.failure_origin = "environment"
            context.failure_detail = f"dependency materialization blocked: {error}"
            if failed_attempt_recovery is not None and not failed_attempt_assigned:
                result = self._failed_attempt_recovery_failure(
                    claimed, f"failed-attempt recovery rejected: {error}"
                )
            else:
                result = NodeResult(
                    status="blocked",
                    summary=f"dependency materialization blocked: {error}",
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    verdict="blocked" if claimed["spec"].get("verifier") else None,
                    **governance_receipt_fields(claimed["contract"]),
                )
        except WorktreeError as error:
            context.failure_origin = "environment"
            context.failure_detail = f"worktree unavailable: {error}"
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
            harness_failure_ref = self._pre_execution_harness_failure_ref(
                claimed, error, context
            )
            if harness_failure_ref is not None:
                context.failure_origin = "tooling_bug"
                context.failure_detail = "pre-execution Workbench harness failure"
                result = NodeResult(
                    status="blocked",
                    summary="pre-execution Workbench harness failure",
                    artifacts={"harness-failure": harness_failure_ref},
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    verdict="blocked" if claimed["spec"].get("verifier") else None,
                    **governance_receipt_fields(claimed["contract"]),
                )
            elif failed_attempt_recovery is not None and not failed_attempt_assigned:
                context.failure_detail = f"worker crashed: {type(error).__name__}: {error}"
                result = self._failed_attempt_recovery_failure(
                    claimed,
                    f"failed-attempt recovery crashed: {type(error).__name__}: {error}",
                )
            else:
                context.failure_detail = f"worker crashed: {type(error).__name__}: {error}"
                result = NodeResult(
                    status="indeterminate",
                    summary=f"worker crashed: {type(error).__name__}: {error}",
                    result_kind="verifier" if claimed["spec"].get("verifier") else "worker",
                    **governance_receipt_fields(claimed["contract"]),
                )
        if context.readiness_report_ref is not None and "execution-readiness" not in result.artifacts:
            result = replace(
                result,
                artifacts={
                    **result.artifacts,
                    "execution-readiness": context.readiness_report_ref,
                },
            )
        if (
            context.dependency_materialization_ref is not None
            and "dependency-materialization" not in result.artifacts
        ):
            result = self._with_dependency_materialization_receipt(
                result,
                context.dependency_materialization_ref,
            )
        if (
            context.dependency_input_ref is not None
            and "dependency-input" not in result.artifacts
        ):
            result = self._with_dependency_input_receipt(
                result,
                context.dependency_input_ref,
            )
        if recovery_artifacts:
            result = self._with_failed_attempt_recovery_artifacts(
                result,
                recovery_artifacts,
            )
        result = self._attach_execution_attribution(
            claimed=claimed,
            request=request,
            result=result,
            context=context,
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
        context: _ExecutionAttributionContext,
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
        recovery_mode = binding.get("mode")
        if recovery_mode not in {None, "blocked_source_repair"}:
            raise DirtyWorktreeRecoveryError("failed-attempt recovery mode is invalid")
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
        expected_generated_residue_paths = source.get("generated_residue_paths", [])
        source_only = source.get("source_only_ignored", False)
        source_only_extraction = source.get("source_only_extraction", False)
        source_delta_sha256 = source.get("source_delta_sha256")
        if not (
            isinstance(source_worktree, str)
            and isinstance(source_branch, str)
            and isinstance(source_base, str)
            and isinstance(expected_paths, list)
            and all(isinstance(path, str) and path for path in expected_paths)
            and isinstance(expected_generated_residue_paths, list)
            and all(
                isinstance(path, str) and path
                for path in expected_generated_residue_paths
            )
            and isinstance(source_only, bool)
            and isinstance(source_only_extraction, bool)
        ):
            raise DirtyWorktreeRecoveryError("failed-attempt recovery source is invalid")
        if source_only_extraction:
            if (
                source_only is not True
                or not isinstance(source_delta_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", source_delta_sha256) is None
            ):
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt recovery source-only extraction digest is invalid"
                )
        elif source_delta_sha256 is not None:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt recovery source delta is invalid"
            )
        if recovery_mode == "blocked_source_repair" and (
            source_only is not True or source_only_extraction is not True
        ):
            raise DirtyWorktreeRecoveryError(
                "blocked source repair must retain the source-only recovery binding"
            )
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
        if source_only:
            try:
                assert_recovery_source_idle(source_path)
            except RecoveryProcessError as error:
                raise DirtyWorktreeRecoveryError(
                    f"source-only recovery cannot prove the source worktree is idle: {error}"
                ) from error
        ignored_paths = self.failed_attempt_recovery.ignored_paths(source_path)
        unsafe_ignored, observed_generated_residue = partition_recovery_paths(
            ignored_paths
        )
        if unsafe_ignored and not source_only:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt source contains ignored paths that cannot be recovered safely: "
                + summarize_recovery_paths(unsafe_ignored)
            )
        unreported_generated = tuple(
            sorted(
                set(observed_generated_residue)
                - set(expected_generated_residue_paths)
            )
        )
        if unreported_generated and not source_only:
            raise DirtyWorktreeRecoveryError(
                "failed-attempt source contains unreported generated residue: "
                + summarize_recovery_paths(unreported_generated)
            )
        if source_only and expected_generated_residue_paths:
            raise DirtyWorktreeRecoveryError(
                "source-only recovery must not discard generated residue"
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

        expected = tuple(expected_paths)

        def validate_source_paths(paths: tuple[str, ...]) -> None:
            if paths != expected:
                raise DirtyWorktreeRecoveryError(
                    "failed-attempt source changes drifted from its result receipt"
                )
            self._validate_failed_attempt_recovery_scope(contract, spec, paths)

        source_delta = (
            inspect_recovery_source_delta(
                source_path,
                dependency_input.input_tree_sha,
                validate_paths=validate_source_paths,
            )
            if source_only_extraction
            else None
        )
        if source_delta is not None:
            if source_delta.sha256 != source_delta_sha256:
                raise DirtyWorktreeRecoveryError(
                    "source-only recovery source delta drifted before target preparation"
                )
            actual_paths = source_delta.changed_paths
        else:
            actual_paths = tuple(
                sorted(
                    changed_paths_since_input_tree(
                        source_path,
                        dependency_input.input_tree_sha,
                    )
                )
            )
            validate_source_paths(actual_paths)

        def assert_source_delta_current(phase: str) -> None:
            if not source_only_extraction:
                return
            current_delta = inspect_recovery_source_delta(
                source_path,
                dependency_input.input_tree_sha,
                validate_paths=validate_source_paths,
            )
            if current_delta.sha256 != source_delta_sha256:
                raise DirtyWorktreeRecoveryError(
                    "source-only recovery source delta drifted " + phase
                )

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
                generated_residue_ref = (
                    None
                    if source_only
                    else self.failed_attempt_recovery.discard_generated_residue(
                        source_path,
                        tuple(expected_generated_residue_paths),
                    )
                )
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
                if source_only_extraction:
                    assert_source_delta_current("while its clean retry was being prepared")
                elif tuple(
                    sorted(
                        changed_paths_since_input_tree(
                            source_path,
                            dependency_input.input_tree_sha,
                        )
                    )
                ) != expected:
                    raise DirtyWorktreeRecoveryError(
                        "failed-attempt source changed while its clean retry was being prepared"
                    )
                if source_only:
                    self._materialize_worktree_dependencies(
                        target,
                        context,
                        timeout_seconds=int(contract["timeout_seconds"]),
                        require_cached_template=True,
                    )
                assert_source_delta_current("before clean retry assignment")
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
                return (
                    target,
                    dependency_input,
                    dependency_ref,
                    (
                        {"generated-residue": generated_residue_ref}
                        if generated_residue_ref is not None
                        else {}
                    ),
                )
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
                expected_generated_residue_paths=tuple(
                    expected_generated_residue_paths
                ),
                source_only=source_only,
                expected_source_delta_sha256=(
                    source_delta_sha256 if source_only_extraction else None
                ),
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
                source_only=source_only,
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
            if source_only:
                self._materialize_worktree_dependencies(
                    target,
                    context,
                    timeout_seconds=int(contract["timeout_seconds"]),
                    require_cached_template=True,
                )
            assert_source_delta_current("before retry assignment")
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
                    **(
                        {"generated-residue": str(recovery["generated_residue_ref"])}
                        if isinstance(recovery.get("generated_residue_ref"), str)
                        else {}
                    ),
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
        """Bind recoverable source paths without promoting ignored residue to worker output."""

        if request.worktree is None:
            return result
        recoverable_paths = (
            changed_paths_since_input_tree(request.worktree, request.input_tree_sha)
            if request.input_tree_sha is not None
            else self.worktrees.changed_paths(
                request.worktree, request.contract["base_sha"]
            )
        )
        ignored_paths = DirtyWorktreeRecovery.ignored_paths(request.worktree)
        unsafe_ignored, generated_residue_paths = partition_recovery_paths(ignored_paths)
        paths = tuple(sorted(recoverable_paths | set(generated_residue_paths)))
        ignored_artifacts: dict[str, str] = {}
        if unsafe_ignored:
            ignored_artifacts["ignored-worktree-residue"] = self.artifacts.put_text(
                canonical_json(
                    {
                        "schema_version": 1,
                        "kind": "ignored-worktree-residue",
                        "ignored_path_count": len(unsafe_ignored),
                        "paths_sha256": canonical_hash(list(unsafe_ignored)),
                        "sample_paths": list(unsafe_ignored[:8]),
                    }
                ),
                "ignored-worktree-residue.json",
            )
        coordinator_retryable = (
            result.status == "blocked"
            and bool(recoverable_paths)
            and not unsafe_ignored
            and request.contract.get("external_write_permission") is False
            and request.contract.get("destructive_action_permission") is False
        )
        return replace(
            result,
            changed_paths=paths,
            artifacts={**result.artifacts, **ignored_artifacts},
            retryable=result.retryable or coordinator_retryable,
        )

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

        context = self._execution_attribution_context(claimed)
        context.prepare_started_at = now_iso()
        started = time.monotonic()
        try:
            result = self._run_blocked_worktree_recovery(claimed)
        except (DirtyWorktreeRecoveryError, WorktreeError, ValueError, OSError) as error:
            context.failure_origin = "environment"
            context.failure_detail = f"blocked-worktree recovery blocked: {error}"
            result = self._blocked_worktree_recovery_failure(
                claimed,
                f"blocked-worktree recovery blocked: {error}",
            )
        except Exception as error:
            # Before a2 is assigned, failures must restore the a1 receipt
            # rather than leaving the recovery node running or indeterminate.
            context.failure_origin = "environment"
            context.failure_detail = f"blocked-worktree recovery crashed: {type(error).__name__}: {error}"
            result = self._blocked_worktree_recovery_failure(
                claimed,
                f"blocked-worktree recovery crashed: {type(error).__name__}: {error}",
            )
        context.prepare_finished_at = now_iso()
        context.prepare_duration_ms = max(0, int((time.monotonic() - started) * 1_000))
        dependency_ref = result.artifacts.get("dependency-input")
        context.dependency_input_ref = dependency_ref if isinstance(dependency_ref, str) else None
        context.scope_checked = result.status == "succeeded"
        if result.status == "failed":
            context.failure_origin = "verification"
            context.failure_detail = result.summary
        result = self._attach_execution_attribution(
            claimed=claimed,
            request=None,
            result=result,
            context=context,
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
        source_only_extraction = binding.get("source_only_extraction")
        if source_only_extraction is not None and not isinstance(
            source_only_extraction, dict
        ):
            raise WorktreeError("blocked source-only recovery authorization is invalid")
        expected_source_delta_sha256 = (
            source_only_extraction.get("source_delta_sha256")
            if isinstance(source_only_extraction, dict)
            else None
        )
        if expected_source_delta_sha256 is not None and not isinstance(
            expected_source_delta_sha256, str
        ):
            raise WorktreeError("blocked source-only recovery digest is invalid")
        if source_only_extraction is not None and (
            contract.get("external_write_permission") is not False
            or contract.get("destructive_action_permission") is not False
        ):
            raise WorktreeError(
                "blocked source-only recovery requires no external or destructive permissions"
            )
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
            source_only=source_only_extraction is not None,
            expected_source_delta_sha256=expected_source_delta_sha256,
        )
        artifacts = {
            **outcome.artifacts,
            "recovery-snapshot": str(recovery["patch_ref"]),
            **(
                {"generated-residue": str(recovery["generated_residue_ref"])}
                if isinstance(recovery.get("generated_residue_ref"), str)
                else {}
            ),
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
            artifacts["recovery-preparation"] = self.artifacts.put_text(
                canonical_json({
                    "schema_version": 1,
                    "kind": "recovery-preparation-failure",
                    "task_id": claimed["task_id"],
                    "node_id": claimed["node_id"],
                    "source_attempt": recovery["source_attempt"],
                    "recovery_attempt": target_attempt,
                    "phase": "acceptance" if outcome.failure_code else "preparation",
                    "code": outcome.failure_code or "preparation-failed",
                    "executor_started": False,
                    "evidence_refs": dict(artifacts),
                }),
                "recovery-preparation.json",
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
                        **(
                            {"source_only_extraction": source_only_extraction}
                            if source_only_extraction is not None
                            else {}
                        ),
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
        attribution_context: _ExecutionAttributionContext | None = None,
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
        route_cursor = self.store.record_node_route(
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
            quota_snapshot_id=(
                attribution_context.quota_snapshot_id
                if attribution_context is not None
                else None
            ),
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
        if attribution_context is not None:
            if isinstance(route_cursor, int) and route_cursor > 0:
                attribution_context.route_event_cursors.append(route_cursor)
            original_was_executed = fallback_kind.startswith("claude-executor-")
            attribution_context.candidate_decisions.append(
                self._candidate_decision(
                    request.spec,
                    disposition="selected" if original_was_executed else "rejected",
                    reason=(
                        f"the selected provider executed before fallback: {reason}"
                        if original_was_executed
                        else f"the candidate was rejected before execution: {reason}"
                    ),
                    event_cursor=route_cursor if isinstance(route_cursor, int) else None,
                    event_type="node.routed",
                )
            )
            attribution_context.candidate_decisions.append(
                self._candidate_decision(
                    routed_request.spec,
                    disposition="selected",
                    reason=f"Codex fallback selected because {fallback_kind}: {reason}",
                    event_cursor=route_cursor if isinstance(route_cursor, int) else None,
                    event_type="node.routed",
                )
            )
            if attribution_context.quota_snapshot_id is None:
                attribution_context.quota_snapshot_id = self._quota_snapshot_reference(
                    self._latest_quota()
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

    @staticmethod
    def _with_dependency_materialization_receipt(
        result: NodeResult,
        receipt_ref: str,
    ) -> NodeResult:
        return replace(
            result,
            artifacts={**result.artifacts, "dependency-materialization": receipt_ref},
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
                self._latest_quota(),
                os.environ.get("CODEX_WORKBENCH_CLAUDE") or "claude",
                self.quota_ttl_seconds,
            )
        raise ValueError(f"unsupported executor {kind!r}")
