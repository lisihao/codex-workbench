from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid
from typing import Any, Mapping, get_args

from .artifacts import ArtifactStore
from .ai_frontier import WorkbenchAIFrontier
from .capabilities import CapabilityCatalogError, CapabilityRegistry
from .config import WorkbenchConfig
from .executors import ClaudeExecutor
from .governance import VerificationTier, governance_status
from .model import (
    CODEX_SOL_MODEL,
    DEFAULT_QUOTA_TTL_SECONDS,
    ROUTING_STRATEGY_VERSION,
    RoutingComplexity,
    RoutingStrategy,
    RoutingTaskType,
    TaskContract,
)
from .performance import PerformanceRegistry, PerformanceRegistryError
from .planner import CodexPlanner
from .radar import WorkbenchRadar
from .research import route_research
from .store import WorkbenchStore


_PERFORMANCE_CALIBRATION_TASK_TYPES = tuple(
    str(value) for value in get_args(RoutingTaskType)
)
_PERFORMANCE_CALIBRATION_COMPLEXITIES = tuple(
    str(value) for value in get_args(RoutingComplexity)
)


def _catalog_claude_families(catalog: Mapping[str, Any] | None) -> frozenset[str]:
    if catalog is None:
        return frozenset(("opus", "sonnet", "fable"))
    models = catalog.get("models")
    if not isinstance(models, list):
        return frozenset()
    families: set[str] = set()
    for raw in models:
        if not isinstance(raw, Mapping) or str(raw.get("provider", "")).lower() != "claude":
            continue
        if raw.get("status") != "available" or raw.get("routable") is not True:
            continue
        model = str(raw.get("model_id", raw.get("model", ""))).lower()
        for family in ("opus", "sonnet", "fable"):
            if family in model:
                families.add(family)
    return frozenset(families)


def _capability_catalog_for_submission(
    config: WorkbenchConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Load the active catalog or passively establish it for a new task.

    This is deliberately before planning and deliberately metadata-only.  The
    registry probes only ``--version``/``--help``/Codex model metadata; it does
    not log in, send a prompt, or consume model quota.  A failed first probe is
    visible in the submission receipt and keeps the pre-existing v2 path.
    """

    registry = CapabilityRegistry(
        config.state_root,
        codex_binary=os.environ.get("CODEX_WORKBENCH_CODEX") or "codex",
        claude_binary=os.environ.get("CODEX_WORKBENCH_CLAUDE") or "claude",
    )
    active: dict[str, Any] | None = None
    active_error: str | None = None
    refresh: dict[str, Any] | None = None
    try:
        active = registry.active()
    except CapabilityCatalogError as error:
        active_error = str(error)
    if active is None and active_error is None:
        refresh = registry.refresh(activate_safe=True)
        candidate = refresh.get("catalog") if isinstance(refresh, Mapping) else None
        if refresh.get("ok") is True and isinstance(candidate, Mapping):
            active = dict(candidate)
        else:
            active_error = str(refresh.get("error", "passive capability refresh did not produce an active catalog"))
    if active is None:
        return None, {
            "mode": "legacy-v2",
            "status": "unavailable",
            "active_catalog_id": None,
            "capability_digest": None,
            "refresh_attempted": refresh is not None,
            "refresh_ok": bool(refresh and refresh.get("ok") is True),
            "reason": active_error or "no active capability catalog",
        }
    return active, {
        "mode": "model-routing-v3",
        "status": "active",
        "active_catalog_id": active.get("catalog_id"),
        "capability_digest": active.get("digest"),
        "refresh_attempted": refresh is not None,
        "refresh_ok": bool(refresh is None or refresh.get("ok") is True),
        "probe_errors": list(active.get("probe_errors", ())),
    }


def _performance_calibration_for_submission(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    catalog: Mapping[str, Any] | None,
    *,
    task_type: str,
    complexity: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Pin a content-addressed performance generation before planning.

    Calibration is intentionally advisory: a failure does not turn a normal
    governed submission into an apparent model failure.  With no active
    capability catalog there is no exact provider/model/version identity to
    bind, so the result remains explicitly unavailable rather than inventing a
    cross-version score.
    """

    if catalog is None:
        return None, {
            "status": "unavailable",
            "snapshot_id": None,
            "digest": None,
            "policy": "benchmark-prior-plus-runtime-ledger-v1",
            "reason": "no active capability catalog to bind performance calibration",
        }
    registry = PerformanceRegistry(config.state_root)
    try:
        radar_status = WorkbenchRadar(
            state_root=config.effective_radar_state_root,
            authorization_file=config.effective_radar_authorization_file,
            enabled=config.radar_enabled,
            stale_after_seconds=config.radar_stale_after_seconds,
            expire_after_seconds=config.radar_expire_after_seconds,
        ).status()
        ai_frontier_status = WorkbenchAIFrontier(
            state_root=config.effective_ai_frontier_state_root,
            authorization_file=config.effective_ai_frontier_authorization_file,
            enabled=config.ai_frontier_enabled,
            stale_after_seconds=config.ai_frontier_stale_after_seconds,
            expire_after_seconds=config.ai_frontier_expire_after_seconds,
        ).status()
        refreshed = registry.refresh(
            store,
            catalog,
            radar_status=radar_status,
            ai_frontier_status=ai_frontier_status,
        )
        snapshot = refreshed["snapshot"]
        calibration = registry.calibrate(catalog, task_type, complexity)
        calibration_matrix = registry.calibrate_matrix(
            catalog,
            _PERFORMANCE_CALIBRATION_TASK_TYPES,
            _PERFORMANCE_CALIBRATION_COMPLEXITIES,
        )
        snapshot_id = str(snapshot["snapshot_id"])
        snapshot_digest = str(snapshot["digest"])
        if calibration.get("snapshot_id") != snapshot_id:
            raise PerformanceRegistryError(
                "current-task performance calibration does not match the refreshed generation"
            )
        if calibration_matrix.get("snapshot_id") != snapshot_id:
            raise PerformanceRegistryError(
                "performance calibration matrix does not match the refreshed generation"
            )
        raw_contexts = calibration_matrix.get("contexts")
        if not isinstance(raw_contexts, list):
            raise PerformanceRegistryError("performance calibration matrix contexts are missing")
        expected_contexts = {
            (candidate_task_type, candidate_complexity)
            for candidate_task_type in _PERFORMANCE_CALIBRATION_TASK_TYPES
            for candidate_complexity in _PERFORMANCE_CALIBRATION_COMPLEXITIES
        }
        observed_contexts = {
            (str(context.get("task_type")), str(context.get("complexity")))
            for context in raw_contexts
            if isinstance(context, Mapping)
        }
        if observed_contexts != expected_contexts or len(raw_contexts) != len(expected_contexts):
            raise PerformanceRegistryError(
                "performance calibration matrix does not cover every routing task type and complexity"
            )
        calibration = {
            **calibration,
            "performance_snapshot_id": snapshot_id,
            "performance_digest": snapshot_digest,
            "digest": snapshot_digest,
            "contexts": [
                {
                    **context,
                    "performance_snapshot_id": snapshot_id,
                    "performance_digest": snapshot_digest,
                    "digest": snapshot_digest,
                }
                for context in raw_contexts
            ],
        }
    except PerformanceRegistryError as error:
        return None, {
            "status": "unavailable",
            "snapshot_id": None,
            "digest": None,
            "policy": "benchmark-prior-plus-runtime-ledger-v1",
            "reason": str(error),
        }
    return calibration, {
        "status": str(calibration["status"]),
        "snapshot_id": snapshot["snapshot_id"],
        "digest": snapshot["digest"],
        "policy": "benchmark-prior-plus-runtime-ledger-v1",
        "activated": bool(refreshed["activated"]),
        "unchanged": bool(refreshed["unchanged"]),
        "event_cursor": snapshot["event_cursor"],
        "external_priors": snapshot["source_provenance"].get("external_priors", {}),
        "advisory_only": True,
        "hard_capability_gates_required": True,
    }


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
    routing_strategy: str = ROUTING_STRATEGY_VERSION,
    task_type: str = "implementation",
    complexity: str = "standard",
    parallelizable: bool = True,
    claude_allowed: bool = True,
    task_points: float = 1.0,
    verification_tier: VerificationTier = "L2",
    strategy: RoutingStrategy | dict | None = None,
    source_thread_id: str | None = None,
    context_bundle_ref: str | None = None,
    context_excerpt: str | None = None,
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
    if strategy is not None:
        selected_strategy = (
            RoutingStrategy.from_dict(strategy)
            if isinstance(strategy, dict)
            else strategy.normalized()
        )
        routing_strategy = selected_strategy.version
        task_type = selected_strategy.task_type
        complexity = selected_strategy.complexity
        parallelizable = selected_strategy.parallelizable
        claude_allowed = selected_strategy.claude_allowed
    capability_catalog, capability_registry = _capability_catalog_for_submission(config)
    performance_calibration, performance_registry = _performance_calibration_for_submission(
        config,
        store,
        capability_catalog,
        task_type=task_type,
        complexity=complexity,
    )
    routing_catalog = (
        {
            **capability_catalog,
            "performance_calibration": performance_calibration,
        }
        if capability_catalog is not None
        else None
    )
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
        routing_strategy=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
        task_points=task_points,
        verification_tier=verification_tier,
        source_thread_id=source_thread_id,
        context_bundle_ref=context_bundle_ref,
        capability_snapshot_id=(
            str(capability_catalog["catalog_id"])
            if capability_catalog is not None
            else None
        ),
        capability_digest=(
            str(capability_catalog["digest"])
            if capability_catalog is not None
            else None
        ),
        performance_snapshot_id=performance_registry["snapshot_id"],
        performance_digest=performance_registry["digest"],
        performance_policy=performance_registry["policy"],
        performance_status=performance_registry["status"],
    )
    contract.validate()
    artifacts = ArtifactStore(config.state_root / "artifacts")
    quota = store.latest_quota()
    catalog_claude_families = _catalog_claude_families(capability_catalog)
    quota_admitted_models = tuple(
        model
        for model in ("opus", "sonnet", "fable")
        if model in catalog_claude_families
        if quota is not None
        and quota.dispatch_decision(
            model,
            max_age_seconds=DEFAULT_QUOTA_TTL_SECONDS,
        ).action == "claude"
    )
    claude_authenticated = False
    if quota_admitted_models and contract.claude_allowed:
        claude_authenticated, _ = ClaudeExecutor(
            artifacts,
            quota,
            os.environ.get("CODEX_WORKBENCH_CLAUDE") or "claude",
        ).authentication()
    claude_models_available = tuple(
        model for model in quota_admitted_models if claude_authenticated
    )
    nodes = CodexPlanner(
        os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
        model=planner_model,
    ).compile(
        contract,
        claude_models_available=claude_models_available,
        default_executor_model=executor_model,
        verifier_model=contract.verifier_model or CODEX_SOL_MODEL,
        quota_snapshot=quota,
        strategy=contract.strategy,
        context_excerpt=context_excerpt,
        capability_snapshot=routing_catalog,
        provider_capacity={"codex": {"capacity": config.max_workers, "active": 0}},
        performance_calibration=performance_calibration,
    )
    resolved_command_id = command_id or f"request-{uuid.uuid4()}"
    store.create_task(contract, nodes, resolved_command_id)
    if queue:
        store.queue_task(resolved_task_id)
    if source_thread_id:
        store.bind_task_to_session(source_thread_id, resolved_task_id)
    return {
        "ok": True,
        "task_id": resolved_task_id,
        "command_id": resolved_command_id,
        "base_sha": resolved_base_sha,
        "claude_dispatch_available": bool(claude_models_available),
        "claude_models_available": claude_models_available,
        "routing_strategy": contract.strategy.to_dict(),
        "routing_policy": {
            "version": "model-routing-v3" if capability_catalog is not None else contract.strategy.version,
            "catalog_id": contract.capability_snapshot_id,
            "capability_digest": contract.capability_digest,
            "performance_snapshot_id": contract.performance_snapshot_id,
            "performance_digest": contract.performance_digest,
            "performance_policy": contract.performance_policy,
            "performance_status": contract.performance_status,
        },
        "capability_registry": capability_registry,
        "performance": {
            **performance_registry,
            # The complete 24-context matrix is an internal deterministic
            # normalizer input.  Returning it through MCP would waste caller
            # context tokens, so expose only the requested task bucket plus a
            # count proving the matrix was present.
            "calibration": (
                {
                    **{
                        key: value
                        for key, value in performance_calibration.items()
                        if key != "contexts"
                    },
                    "matrix_context_count": len(performance_calibration.get("contexts", ())),
                }
                if performance_calibration is not None
                else None
            ),
        },
        "research": route_research(contract).to_dict(),
        "governance": {
            **governance_status(),
            "verification_tier": contract.verification_tier,
        },
        "nodes": [node.to_dict() for node in nodes],
    }
