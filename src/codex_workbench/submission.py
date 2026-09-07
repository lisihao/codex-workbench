from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import subprocess
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
    NodeSpec,
    ROUTING_STRATEGY_VERSION,
    RoutingComplexity,
    RoutingStrategy,
    RoutingTaskType,
    TaskContract,
    canonical_hash,
)
from .performance import (
    PERFORMANCE_SEMANTIC_VERSION,
    PerformanceRegistry,
    PerformanceRegistryError,
)
from .planner import CodexPlanner
from .radar import WorkbenchRadar
from .research import route_research
from .store import WorkbenchStore
from .squilla_advisor import SquillaAdvisor


_PERFORMANCE_CALIBRATION_TASK_TYPES = tuple(
    str(value) for value in get_args(RoutingTaskType)
)
_PERFORMANCE_CALIBRATION_COMPLEXITIES = tuple(
    str(value) for value in get_args(RoutingComplexity)
)


@dataclass(frozen=True)
class CompiledNaturalLanguageRequest:
    """One fully normalized plan that has not yet materialized a task.

    Compilation is deliberately separate from the durable task transition so a
    slow planner cannot leave a partially created task behind.  ``result`` is
    the prompt-free public receipt; the executable node prompts stay only in
    ``nodes`` until the caller atomically materializes the graph.
    """

    contract: TaskContract
    nodes: tuple[NodeSpec, ...]
    command_id: str
    result: dict[str, Any]


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
            "policy": PERFORMANCE_SEMANTIC_VERSION,
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
            "policy": PERFORMANCE_SEMANTIC_VERSION,
            "reason": str(error),
        }
    return calibration, {
        "status": str(calibration["status"]),
        "snapshot_id": snapshot["snapshot_id"],
        "digest": snapshot["digest"],
        "policy": PERFORMANCE_SEMANTIC_VERSION,
        "activated": bool(refreshed["activated"]),
        "unchanged": bool(refreshed["unchanged"]),
        "event_cursor": snapshot["event_cursor"],
        "external_priors": snapshot["source_provenance"].get("external_priors", {}),
        "advisory_only": True,
        "hard_capability_gates_required": True,
    }
def _squilla_advisor_for_submission(config: WorkbenchConfig) -> SquillaAdvisor | None:
    """Create only an explicitly enabled local advisor; never install assets here."""

    advisor_config = config.effective_squilla_advisor_config
    if not advisor_config.enabled:
        return None
    return SquillaAdvisor(
        runtime_python=advisor_config.runtime_python,
        source_root=advisor_config.source_root,
        bundle_dir=advisor_config.bundle_dir,
        # The configured 45-second default leaves a bounded margin for the
        # observed cold native load; warm runs remain local and batched.
        timeout_seconds=advisor_config.timeout_seconds,
    )


def _planning_request_strategy(
    strategy: RoutingStrategy | dict | None,
    *,
    routing_strategy: str,
    task_type: str,
    complexity: str,
    parallelizable: bool,
    claude_allowed: bool,
) -> RoutingStrategy:
    """Normalize routing controls without probing a model or provider."""

    if strategy is not None:
        if isinstance(strategy, RoutingStrategy):
            return strategy.normalized()
        if isinstance(strategy, dict):
            return RoutingStrategy.from_dict(strategy)
        raise ValueError("strategy must be a routing strategy object")
    return RoutingStrategy(
        version=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
    ).normalized()


def _planning_request_scope(
    value: list[str] | tuple[str, ...],
    name: str,
    *,
    required: bool = False,
) -> list[str]:
    """Return a JSON-safe scope list after cheap boundary validation."""

    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple of strings")
    normalized = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        normalized.append(item)
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _planning_request_text(value: str, name: str, *, required: bool = True) -> str:
    """Validate one user-facing text field without changing its content."""

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if required and not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _planning_request_identifier(value: str | None, name: str) -> str | None:
    """Validate an optional durable identifier before it enters the ledger."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise ValueError(f"{name} must be non-empty and contain no whitespace")
    return value


def _planning_request_context_excerpt(value: str | None) -> str | None:
    """Validate transient planner context without making it durable input."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("context_excerpt must be a string when supplied")
    if len(value) > 20_000:
        raise ValueError("context_excerpt must contain at most 20000 characters")
    return value


def _public_projection(value: Any) -> Any:
    """Remove prompt-bearing fields from an otherwise public receipt value."""

    if isinstance(value, Mapping):
        return {
            str(key): _public_projection(item)
            for key, item in value.items()
            if str(key).lower() not in {"context_excerpt", "nodes"}
            and "prompt" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_public_projection(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_projection(item) for item in value)
    return value


_PUBLIC_COMPILED_RESULT_FIELDS = (
    "ok",
    "task_id",
    "command_id",
    "base_sha",
    "claude_dispatch_available",
    "claude_models_available",
    "routing_strategy",
    "routing_policy",
    "capability_registry",
    "performance",
    "research",
    "governance",
)


def _public_compiled_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-prompt summary allowed in planning status receipts."""

    public = {
        key: _public_projection(result[key])
        for key in _PUBLIC_COMPILED_RESULT_FIELDS
        if key in result
    }
    raw_nodes = result.get("nodes")
    if isinstance(raw_nodes, (list, tuple)):
        public["node_count"] = len(raw_nodes)
    elif isinstance(result.get("node_count"), int) and not isinstance(result["node_count"], bool):
        public["node_count"] = int(result["node_count"])
    else:
        public["node_count"] = 0
    return public


def _public_planning_error(value: Any, state: object) -> dict[str, str]:
    """Project an internal diagnostic to a fixed public failure summary.

    Planner and subprocess diagnostics can include their input or provider
    output.  Their durable text remains available to authorized local repair
    tooling, while the MCP and CLI surface receives only a state-derived
    category and a content fingerprint for support correlation.
    """

    if isinstance(value, str):
        digest_source = value.encode("utf-8")
    else:
        # Store-generated diagnostics are strings.  Keep malformed imported
        # data non-disclosive too, without attempting to serialize it here.
        digest_source = type(value).__name__.encode("utf-8")
    if state == "indeterminate":
        return {
            "type": "planning-indeterminate",
            "summary": "Planning did not settle; inspect authorized local diagnostics before retrying.",
            "error_ref": "sha256:" + sha256(digest_source).hexdigest(),
        }
    return {
        "type": "planning-failed",
        "summary": "Planning failed; inspect authorized local diagnostics before retrying.",
        "error_ref": "sha256:" + sha256(digest_source).hexdigest(),
    }


def planning_request_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Expose the stable public status vocabulary for a planning receipt."""

    public = _public_projection(receipt)
    if not isinstance(public, dict):
        raise TypeError("planning receipt must project to an object")
    state = public.get("state")
    public["ok"] = True
    public["status_tool"] = "workbench_get_request"
    if "status" not in public and isinstance(state, str):
        public["status"] = state
    if receipt.get("error") is not None:
        public["error"] = _public_planning_error(receipt["error"], state)
    request = public.get("request")
    raw_request = receipt.get("request")
    if isinstance(request, dict):
        public["request"] = {
            key: value
            for key, value in request.items()
            if key != "context_excerpt"
        }
        public["context_excerpt_present"] = bool(
            (
                raw_request.get("context_excerpt")
                if isinstance(raw_request, Mapping)
                else None
            )
            or (request.get("source_thread_id") and request.get("context_bundle_ref"))
        )
    result = receipt.get("result")
    if isinstance(result, Mapping):
        public["result"] = _public_compiled_result(result)
    return public


def _freeze_natural_language_request(
    *,
    objective: str,
    repository: str,
    allowed_scope: list[str] | tuple[str, ...],
    forbidden_scope: list[str] | tuple[str, ...],
    acceptance_commands: list[str] | tuple[str, ...],
    task_id: str | None,
    command_id: str | None,
    planner_model: str,
    executor_model: str,
    verifier_model: str,
    timeout_seconds: int,
    retry_limit: int,
    external_write_permission: bool,
    queue: bool,
    base_sha: str | None,
    routing_strategy: str,
    task_type: str,
    complexity: str,
    parallelizable: bool,
    claude_allowed: bool,
    task_points: float,
    verification_tier: VerificationTier,
    strategy: RoutingStrategy | dict | None,
    source_thread_id: str | None,
    context_bundle_ref: str | None,
    context_excerpt: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Normalize durable planning input while retaining private context in memory.

    The frozen ledger request intentionally has no ``context_excerpt``.  The
    session binding plus its content-addressed bundle reference are the durable
    context authority; transient excerpt text is passed to a live compiler only.
    """

    _planning_request_text(objective, "objective")
    transient_context = _planning_request_context_excerpt(context_excerpt)
    resolved_repository = Path(repository).expanduser().resolve(strict=True)
    if not resolved_repository.is_dir():
        raise ValueError("repository must be a directory")
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
    if not resolved_base_sha:
        raise ValueError("base_sha could not be resolved")

    normalized_strategy = _planning_request_strategy(
        strategy,
        routing_strategy=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
    )
    resolved_source_thread_id = _planning_request_identifier(
        source_thread_id,
        "source_thread_id",
    )
    if bool(resolved_source_thread_id) != bool(context_bundle_ref):
        raise ValueError(
            "source_thread_id and context_bundle_ref must be supplied together"
        )
    if context_bundle_ref is not None:
        if not isinstance(context_bundle_ref, str) or not context_bundle_ref.startswith("sha256:"):
            raise ValueError("context_bundle_ref must be a content-addressed artifact ref")
    request_without_ids: dict[str, Any] = {
        "request_schema": "natural-language-planning-v1",
        "objective": objective,
        "repository": str(resolved_repository),
        "allowed_scope": _planning_request_scope(allowed_scope, "allowed_scope", required=True),
        "forbidden_scope": _planning_request_scope(forbidden_scope, "forbidden_scope"),
        "acceptance_commands": _planning_request_scope(
            acceptance_commands,
            "acceptance_commands",
        ),
        "planner_model": planner_model,
        "executor_model": executor_model,
        "verifier_model": verifier_model,
        "timeout_seconds": timeout_seconds,
        "retry_limit": retry_limit,
        "external_write_permission": external_write_permission,
        "queue": queue,
        "base_sha": resolved_base_sha,
        "routing_strategy": normalized_strategy.version,
        "task_type": normalized_strategy.task_type,
        "complexity": normalized_strategy.complexity,
        "parallelizable": normalized_strategy.parallelizable,
        "claude_allowed": normalized_strategy.claude_allowed,
        "task_points": task_points,
        "verification_tier": verification_tier,
        "strategy": normalized_strategy.to_dict(),
        "source_thread_id": resolved_source_thread_id,
        "context_bundle_ref": context_bundle_ref,
    }
    supplied_task_id = _planning_request_identifier(task_id, "task_id")
    supplied_command_id = _planning_request_identifier(command_id, "command_id")
    # The canonical request identity deliberately excludes caller IDs and raw
    # imported conversation text.  A timeout retry with the same frozen
    # request is therefore idempotent even when callers omitted both IDs.
    resolved_command_id = supplied_command_id or f"request-{canonical_hash(request_without_ids)}"
    resolved_task_id = supplied_task_id or f"task-{resolved_command_id}"
    return (
        {
            **request_without_ids,
            "task_id": resolved_task_id,
            "command_id": resolved_command_id,
        },
        transient_context,
    )


def enqueue_natural_language_request(
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
    """Persist a natural-language planning request for asynchronous execution.

    This is intentionally a metadata-only fast path.  It resolves the local
    repository and base commit and writes one complete, immutable request to
    the planning ledger.  Planner compilation, Claude authentication, quota
    probes, and all model calls belong to the background consumer.
    """

    frozen_request, _ = _freeze_natural_language_request(
        objective=objective,
        repository=repository,
        allowed_scope=allowed_scope,
        forbidden_scope=forbidden_scope,
        acceptance_commands=acceptance_commands,
        task_id=task_id,
        command_id=command_id,
        planner_model=planner_model,
        executor_model=executor_model,
        verifier_model=verifier_model,
        timeout_seconds=timeout_seconds,
        retry_limit=retry_limit,
        external_write_permission=external_write_permission,
        queue=queue,
        base_sha=base_sha,
        routing_strategy=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
        task_points=task_points,
        verification_tier=verification_tier,
        strategy=strategy,
        source_thread_id=source_thread_id,
        context_bundle_ref=context_bundle_ref,
        context_excerpt=context_excerpt,
    )
    receipt = store.enqueue_planning_request(
        frozen_request["command_id"],
        frozen_request["task_id"],
        frozen_request,
    )
    if not isinstance(receipt, dict):
        raise TypeError("enqueue_planning_request must return a receipt object")
    return planning_request_receipt(receipt)


def compile_natural_language_request(
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
) -> CompiledNaturalLanguageRequest:
    """Compile a fully normalized plan without materializing a task graph.

    The result deliberately has no node prompts.  Callers that own a durable
    task transition receive the nodes separately and must materialize them in
    the store transaction that owns their planning receipt.
    """

    frozen_request, transient_context = _freeze_natural_language_request(
        objective=objective,
        repository=repository,
        allowed_scope=allowed_scope,
        forbidden_scope=forbidden_scope,
        acceptance_commands=acceptance_commands,
        task_id=task_id,
        command_id=command_id,
        planner_model=planner_model,
        executor_model=executor_model,
        verifier_model=verifier_model,
        timeout_seconds=timeout_seconds,
        retry_limit=retry_limit,
        external_write_permission=external_write_permission,
        queue=queue,
        base_sha=base_sha,
        routing_strategy=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
        task_points=task_points,
        verification_tier=verification_tier,
        strategy=strategy,
        source_thread_id=source_thread_id,
        context_bundle_ref=context_bundle_ref,
        context_excerpt=context_excerpt,
    )
    resolved_task_id = str(frozen_request["task_id"])
    resolved_command_id = str(frozen_request["command_id"])
    resolved_repository = str(frozen_request["repository"])
    resolved_base_sha = str(frozen_request["base_sha"])
    selected_strategy = RoutingStrategy.from_dict(frozen_request["strategy"])
    planner_model = str(frozen_request["planner_model"])
    executor_model = str(frozen_request["executor_model"])
    verifier_model = str(frozen_request["verifier_model"])
    capability_catalog, capability_registry = _capability_catalog_for_submission(config)
    performance_calibration, performance_registry = _performance_calibration_for_submission(
        config,
        store,
        capability_catalog,
        task_type=selected_strategy.task_type,
        complexity=selected_strategy.complexity,
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
        repository=resolved_repository,
        base_sha=resolved_base_sha,
        objective=str(frozen_request["objective"]),
        allowed_scope=tuple(frozen_request["allowed_scope"]),
        forbidden_scope=tuple(frozen_request["forbidden_scope"]),
        acceptance_commands=tuple(frozen_request["acceptance_commands"]),
        planner_model=planner_model,
        executor_model=executor_model,
        verifier_model=verifier_model,
        timeout_seconds=int(frozen_request["timeout_seconds"]),
        retry_limit=int(frozen_request["retry_limit"]),
        external_write_permission=bool(frozen_request["external_write_permission"]),
        destructive_action_permission=False,
        routing_strategy=selected_strategy.version,
        task_type=selected_strategy.task_type,
        complexity=selected_strategy.complexity,
        parallelizable=selected_strategy.parallelizable,
        claude_allowed=selected_strategy.claude_allowed,
        task_points=float(frozen_request["task_points"]),
        verification_tier=frozen_request["verification_tier"],
        source_thread_id=frozen_request["source_thread_id"],
        context_bundle_ref=frozen_request["context_bundle_ref"],
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
    squilla_advisor = _squilla_advisor_for_submission(config)
    nodes = CodexPlanner(
        os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
        model=planner_model,
        squilla_advisor=squilla_advisor,
    ).compile(
        contract,
        claude_models_available=claude_models_available,
        default_executor_model=executor_model,
        verifier_model=contract.verifier_model or CODEX_SOL_MODEL,
        quota_snapshot=quota,
        strategy=contract.strategy,
        context_excerpt=transient_context,
        capability_snapshot=routing_catalog,
        provider_capacity={"codex": {"capacity": config.max_workers, "active": 0}},
        performance_calibration=performance_calibration,
    )
    internal_result = {
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
                        if key not in {"contexts", "candidates"}
                    },
                    "matrix_context_count": len(performance_calibration.get("contexts", ())),
                    "candidate_count": len(performance_calibration.get("candidates", ())),
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
    return CompiledNaturalLanguageRequest(
        contract=contract,
        nodes=tuple(nodes),
        command_id=resolved_command_id,
        result=_public_compiled_result(internal_result),
    )


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
) -> dict[str, Any]:
    """Compile, then keep the historic create, queue, and session-bind flow."""

    compiled = compile_natural_language_request(
        config,
        store,
        objective=objective,
        repository=repository,
        allowed_scope=allowed_scope,
        forbidden_scope=forbidden_scope,
        acceptance_commands=acceptance_commands,
        task_id=task_id,
        command_id=command_id,
        planner_model=planner_model,
        executor_model=executor_model,
        verifier_model=verifier_model,
        timeout_seconds=timeout_seconds,
        retry_limit=retry_limit,
        external_write_permission=external_write_permission,
        queue=queue,
        base_sha=base_sha,
        routing_strategy=routing_strategy,
        task_type=task_type,
        complexity=complexity,
        parallelizable=parallelizable,
        claude_allowed=claude_allowed,
        task_points=task_points,
        verification_tier=verification_tier,
        strategy=strategy,
        source_thread_id=source_thread_id,
        context_bundle_ref=context_bundle_ref,
        context_excerpt=context_excerpt,
    )
    store.create_task(compiled.contract, list(compiled.nodes), compiled.command_id)
    if queue:
        store.queue_task(compiled.contract.task_id)
    if compiled.contract.source_thread_id:
        store.bind_task_to_session(
            compiled.contract.source_thread_id,
            compiled.contract.task_id,
        )
    return compiled.result
