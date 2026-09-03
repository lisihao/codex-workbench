from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid
from typing import Any, Mapping

from .artifacts import ArtifactStore
from .capabilities import CapabilityCatalogError, CapabilityRegistry
from .config import WorkbenchConfig
from .executors import ClaudeExecutor
from .governance import VerificationTier, governance_status
from .model import (
    CODEX_SOL_MODEL,
    DEFAULT_QUOTA_TTL_SECONDS,
    ROUTING_STRATEGY_VERSION,
    RoutingStrategy,
    TaskContract,
)
from .planner import CodexPlanner
from .research import route_research
from .store import WorkbenchStore


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
        capability_snapshot=capability_catalog,
        provider_capacity={"codex": {"capacity": config.max_workers, "active": 0}},
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
        },
        "capability_registry": capability_registry,
        "research": route_research(contract).to_dict(),
        "governance": {
            **governance_status(),
            "verification_tier": contract.verification_tier,
        },
        "nodes": [node.to_dict() for node in nodes],
    }
