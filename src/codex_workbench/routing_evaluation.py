"""Offline, deterministic comparisons of capability-routing decisions.

The evaluator is deliberately a comparison harness, not a production router.
It runs the same :func:`route_capability_snapshot` policy for every supplied
calibration variant and changes only the advisory performance calibration.
Consequently the result can show whether an input calibration changes a route,
while it cannot prove that any model actually delivered a better task outcome
or saved money.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from .performance import (
    PERFORMANCE_SEMANTIC_VERSION,
    PerformanceRegistry,
    load_benchmark_baseline,
    validate_benchmark_baseline,
)
from .routing_v3 import RankedCandidate, RoutingV3Decision, route_capability_snapshot


_VARIANT_ORDER = ("declared_baseline", "without_ai_frontier", "current")
_CALIBRATION_ID_KEYS = (
    "performance_snapshot_id",
    "snapshot_id",
    "generation",
    "performance_generation_id",
)
_CALIBRATION_DIGEST_KEYS = (
    "performance_digest",
    "digest",
    "snapshot_digest",
    "performance_snapshot_digest",
)
# These fields identify or embed a performance generation.  They are not
# routing constraints, so an explicitly supplied comparator snapshot is
# allowed to replace them while all other request fields remain untouched.
_REQUEST_CALIBRATION_PIN_KEYS = frozenset(
    {
        "performance_calibration",
        "performance_snapshot_id",
        "performance_generation_id",
        "performance_digest",
        "performance_snapshot_digest",
    }
)


def evaluate_routes(
    catalog: Mapping[str, Any] | Any,
    requests: Iterable[Mapping[str, Any] | Any],
    *,
    calibrations: Mapping[str, Mapping[str, Any] | Any | None] | None = None,
) -> dict[str, Any]:
    """Compare deterministic routes for one catalog and several input variants.

    ``catalog`` and every item in ``requests`` use the same plain-mapping
    boundary as :func:`route_capability_snapshot`.  ``calibrations`` maps a
    variant label (normally ``without_ai_frontier`` or ``current``) to a
    ``PerformanceRegistry.calibrate_matrix``-compatible mapping.  The first
    variant is always ``declared_baseline`` and deliberately routes with
    declared capability quality only; it is not a claim about an historical
    production router.

    All requests are copied before routing.  Only performance-calibration pins
    are removed, because a supplied comparator calibration intentionally binds
    that one input.  Quota, authentication, role, scope, feature, and
    concurrency fields are passed through unchanged.  The function performs no
    model, network, filesystem, or durable-state operations.
    """

    catalog_mapping = _as_mapping(catalog, label="catalog")
    request_mappings = _materialize_requests(requests)
    variant_calibrations = _variant_calibrations(calibrations)

    normalized_calibrations: dict[str, dict[str, Any] | None] = {}
    route_calibrations: dict[str, dict[str, Any]] = {}
    for label in variant_calibrations:
        calibration = variant_calibrations[label]
        if calibration is None:
            normalized_calibrations[label] = None
            route_calibrations[label] = {}
            continue
        normalized = _normalize_calibration(calibration, label=label)
        _validate_calibration_binding(normalized, label=label)
        normalized_calibrations[label] = normalized
        route_calibrations[label] = _route_calibration(normalized)

    strategies: dict[str, dict[str, Any]] = {}
    rows_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for label in variant_calibrations:
        rows: list[dict[str, Any]] = []
        calibration = normalized_calibrations[label]
        for index, request in enumerate(request_mappings):
            request_mapping = _as_mapping(request, label=f"request[{index}]")
            route_request = _strip_calibration_pins(request_mapping)
            decision = route_capability_snapshot(
                catalog_mapping,
                route_request,
                # An empty mapping intentionally disables an inherited
                # catalog/request calibration for the declared baseline.
                performance_calibration=route_calibrations[label],
            )
            rows.append(
                _decision_row(
                    label,
                    index=index,
                    request=request_mapping,
                    decision=decision,
                    calibration=calibration,
                )
            )
        rows_by_strategy[label] = rows

    baseline_rows = rows_by_strategy["declared_baseline"]
    for label in variant_calibrations:
        rows = rows_by_strategy[label]
        coverage = _coverage(rows)
        decision_change = _decision_change(
            baseline_rows,
            rows,
            baseline_label="declared_baseline",
        )
        strategy = {
            "name": label,
            "label": label,
            "rows": rows,
            "request_count": len(rows),
            "sample_type": _strategy_sample_type(label, normalized_calibrations[label]),
            "sample_types": sorted({str(row["sample_type"]) for row in rows}),
            "sample_type_counts": _sample_type_counts(rows),
            "source_snapshot": _strategy_source_snapshot(
                label,
                rows,
                normalized_calibrations[label],
            ),
            "coverage": coverage,
            "decision_change": decision_change,
            "decision_change_rate": decision_change["rate"],
            "decision_change_numerator": decision_change["numerator"],
            "decision_change_denominator": decision_change["denominator"],
            "source_contribution": _source_contribution_summary(rows),
            # Route choices are not task outcomes and contain no counterfactual
            # execution or cost observations.
            "delivery_improvement_proven": False,
            "actual_savings": None,
        }
        strategies[label] = strategy

    ai_frontier_incremental = None
    if {"without_ai_frontier", "current"}.issubset(variant_calibrations):
        ai_frontier_incremental = _decision_change(
            rows_by_strategy["without_ai_frontier"],
            rows_by_strategy["current"],
            baseline_label="without_ai_frontier",
        )
        ai_frontier_incremental.update(
            {
                "candidate_strategy": "current",
                "delivery_improvement_proven": False,
                "actual_savings": None,
            }
        )

    # The signature is an implementation detail used only while calculating
    # the rates; do not leak it into the public/JSON result shape.
    for rows in rows_by_strategy.values():
        for row in rows:
            row.pop("_decision_signature", None)

    coverage_by_strategy = {
        label: strategy["coverage"] for label, strategy in strategies.items()
    }
    change_by_strategy = {
        label: strategy["decision_change"] for label, strategy in strategies.items()
    }
    result: dict[str, Any] = {
        "status": "ok" if request_mappings else "insufficient-data",
        "request_count": len(request_mappings),
        "variant_order": list(variant_calibrations),
        "baseline_strategy": "declared_baseline",
        "strategies": strategies,
        # These aliases keep the aggregate fields easy for a CLI/table
        # renderer to consume without losing the detailed per-strategy rows.
        "coverage": coverage_by_strategy,
        "decision_change_rates": change_by_strategy,
        "decision_change_rate": change_by_strategy,
        "comparison": {
            "baseline_strategy": "declared_baseline",
            "strategies": change_by_strategy,
            "ai_frontier_incremental": ai_frontier_incremental,
        },
        "ai_frontier_incremental": ai_frontier_incremental,
        "rows": rows_by_strategy,
        "delivery_improvement_proven": False,
        "actual_savings": None,
    }
    return result


def calibration_for_requests(
    snapshot: Mapping[str, Any] | None,
    catalog: Mapping[str, Any] | Any,
    requests: Iterable[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Build an in-memory calibration matrix for the supplied request contexts.

    This is the read-only adapter for a snapshot produced by
    :func:`build_performance_snapshot`.  It calls the existing pure
    ``PerformanceRegistry._calibrate_context`` implementation once for each
    distinct ``(task_type, complexity)`` pair and adds the immutable snapshot
    identity to the matrix and each context.  It never constructs a registry,
    touches its state root, or performs model/network work.

    ``snapshot=None`` is supported for an explicit cold-start comparator: the
    shipped benchmark baseline is used and the returned identity is ``None``.
    """

    catalog_mapping = _as_mapping(catalog, label="catalog")
    request_mappings = tuple(
        _as_mapping(request, label=f"request[{index}]")
        for index, request in enumerate(_materialize_requests(requests))
    )
    contexts_requested = sorted(
        {
            (
                _request_context_value(request, "task_type", "implementation"),
                _request_context_value(request, "complexity", "standard"),
            )
            for request in request_mappings
        }
    )

    snapshot_mapping = _as_mapping(snapshot, label="performance snapshot") if snapshot is not None else None
    context_snapshot = None
    if snapshot_mapping is not None:
        context_snapshot = dict(snapshot_mapping)
        source_provenance = context_snapshot.get("source_provenance")
        if source_provenance is None:
            context_snapshot["source_provenance"] = {}
        elif not isinstance(source_provenance, Mapping):
            raise TypeError("performance snapshot source_provenance must be a mapping")
    raw_baseline = snapshot_mapping.get("baseline") if snapshot_mapping is not None else None
    baseline = (
        validate_benchmark_baseline(raw_baseline)
        if isinstance(raw_baseline, Mapping)
        else load_benchmark_baseline()
    )
    calibration_catalog = _catalog_for_calibration(catalog_mapping)
    contexts: list[dict[str, Any]] = []
    snapshot_id = snapshot_mapping.get("snapshot_id") if snapshot_mapping is not None else None
    snapshot_digest = snapshot_mapping.get("digest") if snapshot_mapping is not None else None
    for task_type, complexity in contexts_requested:
        context = PerformanceRegistry._calibrate_context(
            context_snapshot,
            calibration_catalog,
            baseline,
            task_type=task_type,
            complexity=complexity,
        )
        context = dict(context)
        if snapshot_id is not None:
            context["performance_snapshot_id"] = snapshot_id
        if snapshot_digest is not None:
            context["performance_digest"] = snapshot_digest
            # ``digest`` is the identity spelling used by existing matrix
            # payloads and is retained alongside the explicit route spelling.
            context["digest"] = snapshot_digest
        contexts.append(context)

    return {
        "status": (
            "insufficient-data"
            if not request_mappings
            else "ok"
            if snapshot_mapping is not None
            else "cold-start"
        ),
        "snapshot_id": snapshot_id,
        "digest": snapshot_digest,
        "performance_snapshot_id": snapshot_id,
        "performance_digest": snapshot_digest,
        "semantic_version": PERFORMANCE_SEMANTIC_VERSION,
        "calibration_policy": {
            "semantic_version": PERFORMANCE_SEMANTIC_VERSION,
            "local_outcomes_only": True,
            "external_evidence_updates_beta": False,
        },
        "task_types": sorted({task_type for task_type, _ in contexts_requested}),
        "complexities": sorted({complexity for _, complexity in contexts_requested}),
        "contexts": contexts,
        "advisory_only": True,
        "hard_capability_gates_required": True,
        "quality_gate_bypass_permitted": False,
    }


def _as_mapping(value: Mapping[str, Any] | Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return dict(mapped)
    if is_dataclass(value) and not isinstance(value, type):
        mapped = asdict(value)
        if isinstance(mapped, Mapping):
            return dict(mapped)
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    raise TypeError(f"{label} must be a mapping or expose to_dict()")


def _request_context_value(
    request: Mapping[str, Any],
    key: str,
    default: str,
) -> str:
    value = request.get(key)
    if value is None:
        return default
    text = str(value).strip().lower()
    return text or default


def _catalog_for_calibration(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the v3 capability spelling to PerformanceRegistry's model input."""

    raw_models = catalog.get("models")
    if isinstance(raw_models, Iterable) and not isinstance(raw_models, (str, bytes, Mapping)):
        adapted = dict(catalog)
        adapted["models"] = [dict(record) if isinstance(record, Mapping) else record for record in raw_models]
        return adapted
    raw_records = catalog.get("capabilities") or catalog.get("records") or ()
    if isinstance(raw_records, (str, bytes, Mapping)) or not isinstance(raw_records, Iterable):
        return dict(catalog)
    models: list[dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            continue
        record = dict(raw_record)
        if record.get("model_id") is None:
            for key in ("model", "model_name", "id"):
                if record.get(key) is not None:
                    record["model_id"] = record[key]
                    break
        if record.get("agent_cli_version") is None and record.get("agent_version") is not None:
            record["agent_cli_version"] = record["agent_version"]
        models.append(record)
    adapted = dict(catalog)
    adapted["models"] = models
    return adapted


def _materialize_requests(
    requests: Iterable[Mapping[str, Any] | Any],
) -> tuple[Mapping[str, Any] | Any, ...]:
    if isinstance(requests, (str, bytes, Mapping)):
        raise TypeError("requests must be an iterable of request mappings")
    try:
        return tuple(requests)
    except TypeError as error:
        raise TypeError("requests must be an iterable of request mappings") from error


def _variant_calibrations(
    calibrations: Mapping[str, Mapping[str, Any] | Any | None] | None,
) -> dict[str, Mapping[str, Any] | Any | None]:
    if calibrations is None:
        supplied: dict[str, Mapping[str, Any] | Any | None] = {}
    elif isinstance(calibrations, Mapping):
        supplied = dict(calibrations)
    else:
        raise TypeError("calibrations must be a mapping of variant labels")

    for label in supplied:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("calibration variant labels must be non-empty strings")
    if "declared_baseline" in supplied and supplied["declared_baseline"] is not None:
        raise ValueError("declared_baseline calibration must be None")

    ordered: dict[str, Mapping[str, Any] | Any | None] = {
        "declared_baseline": None
    }
    for label in _VARIANT_ORDER[1:]:
        if label in supplied:
            ordered[label] = supplied[label]
    for label in sorted(set(supplied) - set(_VARIANT_ORDER)):
        ordered[label] = supplied[label]
    return ordered


def _normalize_calibration(value: Any, *, label: str) -> dict[str, Any]:
    calibration = _as_mapping(value, label=f"calibration[{label}]")
    raw_contexts = calibration.get("contexts")
    if raw_contexts is not None:
        calibration["contexts"] = _context_list(raw_contexts)
    if label == "without_ai_frontier" and _has_current_v2_semantics(calibration):
        return _without_ai_frontier_public_evidence(calibration)
    return calibration


def _has_current_v2_semantics(calibration: Mapping[str, Any]) -> bool:
    policy = calibration.get("calibration_policy")
    return (
        calibration.get("semantic_version") == PERFORMANCE_SEMANTIC_VERSION
        and isinstance(policy, Mapping)
        and policy.get("local_outcomes_only") is True
        and policy.get("external_evidence_updates_beta") is False
    )


def _without_ai_frontier_public_evidence(
    calibration: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a v2 comparator without only AI Frontier public records.

    This deliberately changes neither local posterior counts nor declared
    policy quality.  Legacy calibration is never passed here: it stays
    audit-only and is not reinterpreted as v2 evidence.
    """

    result = dict(calibration)
    raw_evidence = result.get("public_evidence")
    if isinstance(raw_evidence, Iterable) and not isinstance(
        raw_evidence, (str, bytes, Mapping)
    ):
        result["public_evidence"] = [
            dict(record)
            for record in raw_evidence
            if isinstance(record, Mapping) and not _is_ai_frontier_record(record)
        ]
    raw_candidates = result.get("candidates")
    if isinstance(raw_candidates, Iterable) and not isinstance(
        raw_candidates, (str, bytes, Mapping)
    ):
        result["candidates"] = [
            _without_ai_frontier_public_evidence(candidate)
            if isinstance(candidate, Mapping)
            else candidate
            for candidate in raw_candidates
        ]
    raw_contexts = result.get("contexts")
    if isinstance(raw_contexts, Iterable) and not isinstance(
        raw_contexts, (str, bytes, Mapping)
    ):
        result["contexts"] = [
            _without_ai_frontier_public_evidence(context)
            if isinstance(context, Mapping)
            else context
            for context in raw_contexts
        ]
    return result


def _is_ai_frontier_record(record: Mapping[str, Any]) -> bool:
    return str(record.get("source") or "").strip().lower() == "ai-frontier"


def _context_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        flattened: list[dict[str, Any]] = []
        for key, raw_context in value.items():
            if not isinstance(raw_context, Mapping):
                raise TypeError("performance calibration context must be a mapping")
            # Support both {"task/complexity": context} and the useful
            # nested {task: {complexity: context}} spelling while retaining
            # the native calibrate_matrix list shape unchanged.
            if _looks_like_context(raw_context):
                context = dict(raw_context)
                task_type, complexity = _context_key(key)
                if task_type is not None:
                    context.setdefault("task_type", task_type)
                if complexity is not None:
                    context.setdefault("complexity", complexity)
                flattened.append(context)
                continue
            for nested_key, nested_context in raw_context.items():
                if not isinstance(nested_context, Mapping):
                    raise TypeError("performance calibration context must be a mapping")
                context = dict(nested_context)
                task_type = str(key)
                _, complexity = _context_key(nested_key)
                context.setdefault("task_type", task_type)
                if complexity is None:
                    complexity = str(nested_key)
                context.setdefault("complexity", complexity)
                flattened.append(context)
        return flattened
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError("performance calibration contexts must be a sequence or mapping")
    contexts: list[dict[str, Any]] = []
    for raw_context in value:
        if not isinstance(raw_context, Mapping):
            raise TypeError("performance calibration context must be a mapping")
        contexts.append(dict(raw_context))
    return contexts


def _looks_like_context(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in ("task_type", "complexity", "candidates", "snapshot_id", "performance_snapshot_id")
    )


def _context_key(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return str(value[0]), str(value[1])
    if isinstance(value, str):
        for separator in ("/", "|", ":"):
            if separator in value:
                task_type, complexity = value.split(separator, 1)
                return task_type.strip() or None, complexity.strip() or None
    return None, None


def _validate_calibration_binding(calibration: Mapping[str, Any], *, label: str) -> None:
    """Reject one calibration mixing generations across its contexts.

    ``calibrate_matrix`` in the current registry carries a snapshot ID but may
    omit its digest.  Such a shape is valid at this boundary and is retained as
    source metadata; the routing adapter below omits the incomplete identity
    from the call rather than manufacturing a digest.
    """

    scopes = [calibration]
    raw_contexts = calibration.get("contexts")
    if isinstance(raw_contexts, Iterable) and not isinstance(raw_contexts, (str, bytes, Mapping)):
        scopes.extend(context for context in raw_contexts if isinstance(context, Mapping))
    identities = [_binding(scope) for scope in scopes]
    identities = [identity for identity in identities if identity[0] is not None or identity[1] is not None]
    for index, (snapshot_id, digest) in enumerate(identities):
        for other_id, other_digest in identities[index + 1 :]:
            if snapshot_id is not None and other_id is not None and snapshot_id != other_id:
                raise ValueError(
                    f"calibration[{label}] contexts use mismatched performance snapshots"
                )
            if digest is not None and other_digest is not None and digest != other_digest:
                raise ValueError(
                    f"calibration[{label}] contexts use mismatched performance digests"
                )


def _binding(value: Mapping[str, Any]) -> tuple[Any, Any]:
    return _first(value, _CALIBRATION_ID_KEYS), _first(value, _CALIBRATION_DIGEST_KEYS)


def _route_calibration(calibration: Mapping[str, Any]) -> dict[str, Any]:
    """Return a routing-safe calibration without inventing missing identity."""

    result = dict(calibration)
    scopes: list[Mapping[str, Any]] = [result]
    raw_contexts = result.get("contexts")
    if isinstance(raw_contexts, list):
        contexts = [dict(context) for context in raw_contexts]
        result["contexts"] = contexts
        scopes.extend(contexts)
    # The current calibrate_matrix payload can contain only a generation ID.
    # route_capability_snapshot requires an ID/digest pair, so remove all
    # incomplete identities from this call; source metadata remains available
    # from the normalized calibration used to build the result rows.
    has_complete_identity = any(
        snapshot_id is not None and digest is not None
        for snapshot_id, digest in (_binding(scope) for scope in scopes)
    )
    if not has_complete_identity:
        _remove_bindings(result)
        if isinstance(result.get("contexts"), list):
            for context in result["contexts"]:
                if isinstance(context, dict):
                    _remove_bindings(context)
    return result


def _remove_bindings(value: dict[str, Any]) -> None:
    for key in _CALIBRATION_ID_KEYS + _CALIBRATION_DIGEST_KEYS:
        value.pop(key, None)


def _strip_calibration_pins(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in _REQUEST_CALIBRATION_PIN_KEYS
    }


def _decision_row(
    label: str,
    *,
    index: int,
    request: Mapping[str, Any],
    decision: RoutingV3Decision,
    calibration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    selected = decision.selected
    source_request_id = _source_request_id(request, index=index)
    sample_type = _sample_type(request)
    selected_summary = _candidate_summary(selected) if selected is not None else None
    route_model = selected.model if selected is not None else None
    route_provider = selected.provider if selected is not None else None
    performance_snapshot_id, performance_digest = _calibration_for_request(
        calibration,
        request,
    )
    if performance_snapshot_id is None:
        performance_snapshot_id = decision.performance_snapshot_id
    if performance_digest is None:
        performance_digest = decision.performance_digest
    source_snapshot = {
        "catalog_snapshot_id": decision.catalog_snapshot_id,
        "catalog_digest": decision.catalog_digest,
        "performance_snapshot_id": performance_snapshot_id,
        "performance_digest": performance_digest,
        "calibration_snapshot_id": performance_snapshot_id,
        "calibration_digest": performance_digest,
        "snapshot_id": performance_snapshot_id or decision.catalog_snapshot_id,
        "digest": performance_digest or decision.catalog_digest,
        "strategy": label,
    }
    candidates = [_candidate_summary(candidate) for candidate in decision.ranked_candidates]
    excluded_candidates = [candidate.to_dict() for candidate in decision.rejected_candidates]
    excluded_reasons = _excluded_reasons(excluded_candidates)
    route_detail = {
        "provider": route_provider,
        "model": route_model,
        "capability_id": selected.capability_id if selected is not None else None,
    }
    return {
        "variant": label,
        "strategy": label,
        "row_index": index,
        "request_id": source_request_id,
        "source_request_id": source_request_id,
        "sample_type": sample_type,
        "strategy_sample_type": _strategy_sample_type(label, calibration),
        "accepted": decision.accepted,
        "reason": decision.reason,
        # ``route`` is the selected model for convenient table rendering;
        # route_detail carries provider/capability identity without ambiguity.
        "route": route_model,
        "route_model": route_model,
        "provider": route_provider,
        "model": route_model,
        "route_detail": route_detail,
        "candidate_rank": selected.rank if selected is not None else None,
        "selected_rank": selected.rank if selected is not None else None,
        "quality_band": (
            selected.quality_equivalence_band if selected is not None else None
        ),
        "quality_equivalence_band": (
            selected.quality_equivalence_band if selected is not None else None
        ),
        "candidates": candidates,
        "selected_candidate": selected_summary,
        "excluded_candidates": excluded_candidates,
        "excluded_reasons": excluded_reasons,
        "source_snapshot": source_snapshot,
        "source_contribution": _source_contribution(selected),
        "comparable": True,
        "delivery_improvement_proven": False,
        "actual_savings": None,
        "_decision_signature": _decision_signature(decision),
    }


def _source_request_id(request: Mapping[str, Any], *, index: int) -> Any:
    for key in ("source_request_id", "request_id", "task_id", "id"):
        if key in request and request[key] is not None:
            return request[key]
    return f"request-{index}"


def _sample_type(request: Mapping[str, Any]) -> Any:
    for key in ("sample_type", "sample", "request_sample_type"):
        if key in request and request[key] is not None:
            return request[key]
    if request.get("synthetic") is True:
        return "synthetic"
    if request.get("real") is True:
        return "real"
    return "unspecified"


def _strategy_sample_type(
    label: str,
    calibration: Mapping[str, Any] | None,
) -> Any:
    if label == "declared_baseline":
        return "declared"
    if calibration is not None:
        sample_type = _first(calibration, ("sample_type", "calibration_sample_type"))
        if sample_type is not None:
            return sample_type
    return "calibrated"


def _calibration_for_request(
    calibration: Mapping[str, Any] | None,
    request: Mapping[str, Any],
) -> tuple[Any, Any]:
    if calibration is None:
        return None, None
    snapshot_id, digest = _binding(calibration)
    task_type = request.get("task_type")
    complexity = request.get("complexity")
    raw_contexts = calibration.get("contexts")
    if isinstance(raw_contexts, Iterable) and not isinstance(raw_contexts, (str, bytes, Mapping)):
        for raw_context in raw_contexts:
            if not isinstance(raw_context, Mapping):
                continue
            if (
                _normalized(raw_context.get("task_type")) == _normalized(task_type)
                and _normalized(raw_context.get("complexity")) == _normalized(complexity)
            ):
                context_id, context_digest = _binding(raw_context)
                snapshot_id = context_id if context_id is not None else snapshot_id
                digest = context_digest if context_digest is not None else digest
                break
    return snapshot_id, digest


def _candidate_summary(candidate: RankedCandidate) -> dict[str, Any]:
    return {
        "rank": candidate.rank,
        "provider": candidate.provider,
        "model": candidate.model,
        "capability_id": candidate.capability_id,
        "quality_score": candidate.quality_score,
        "ranking_quality_score": candidate.ranking_quality_score,
        "quality_gap": candidate.quality_gap,
        "quality_equivalence_tolerance": candidate.quality_equivalence_tolerance,
        "quality_equivalence_band": candidate.quality_equivalence_band,
        "quality_band": candidate.quality_equivalence_band,
        "quality_source": candidate.quality_source,
        "performance_snapshot_id": candidate.performance_snapshot_id,
        "performance_digest": candidate.performance_digest,
        "external_source_count": candidate.external_source_count,
        "external_quality_mean": candidate.external_quality_mean,
        "external_consistency_mean": candidate.external_consistency_mean,
        "external_consistency_std_mean": candidate.external_consistency_std_mean,
    }


def _excluded_reasons(excluded_candidates: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for candidate in excluded_candidates:
        for reason in candidate.get("reasons", ()):
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _source_contribution(candidate: RankedCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    values = {
        "external_source_count": candidate.external_source_count,
        "external_quality_mean": candidate.external_quality_mean,
        "external_consistency_mean": candidate.external_consistency_mean,
        "external_consistency_std_mean": candidate.external_consistency_std_mean,
        "external_observed_cost_mean": candidate.external_observed_cost_mean,
        "external_cost_surprise_mean": candidate.external_cost_surprise_mean,
    }
    available = any(
        key != "external_source_count" and value is not None
        for key, value in values.items()
    ) or int(values["external_source_count"] or 0) > 0
    return {"available": available, **values}


def _source_contribution_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    contributions = [row["source_contribution"] for row in rows if row["source_contribution"]]
    available = [item for item in contributions if item.get("available")]
    total_source_count = sum(int(item.get("external_source_count") or 0) for item in available)
    return {
        "available": bool(available),
        "rows_with_source_contribution": len(available),
        "denominator": len(rows),
        "external_source_count_total": total_source_count,
    }


def _sample_type_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        sample_type = str(row["sample_type"])
        counts[sample_type] = counts.get(sample_type, 0) + 1
    return counts


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    numerator = sum(1 for row in rows if row["accepted"])
    denominator = len(rows)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
        "accepted": numerator,
        "total": denominator,
    }


def _decision_change(
    baseline_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    baseline_label: str,
) -> dict[str, Any]:
    comparable_pairs = [
        (baseline, row)
        for baseline, row in zip(baseline_rows, rows)
        if baseline.get("comparable") is True and row.get("comparable") is True
    ]
    changed = sum(
        1
        for baseline, row in comparable_pairs
        if baseline["_decision_signature"] != row["_decision_signature"]
    )
    denominator = len(comparable_pairs)
    return {
        "baseline_strategy": baseline_label,
        "numerator": changed,
        "denominator": denominator,
        "comparable_rows": denominator,
        "rate": changed / denominator if denominator else None,
    }


def _strategy_source_snapshot(
    label: str,
    rows: list[dict[str, Any]],
    calibration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if rows:
        source = dict(rows[0]["source_snapshot"])
    else:
        snapshot_id, digest = _binding(calibration or {})
        source = {
            "catalog_snapshot_id": None,
            "catalog_digest": None,
            "performance_snapshot_id": snapshot_id,
            "performance_digest": digest,
            "calibration_snapshot_id": snapshot_id,
            "calibration_digest": digest,
            "snapshot_id": snapshot_id,
            "digest": digest,
        }
    source["strategy"] = label
    return source


def _decision_signature(decision: RoutingV3Decision) -> tuple[Any, ...]:
    selected = decision.selected
    return (
        decision.accepted,
        selected.provider if selected is not None else None,
        selected.model if selected is not None else None,
        selected.capability_id if selected is not None else None,
    )


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _normalized(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


__all__ = ["calibration_for_requests", "evaluate_routes"]
