"""Benchmark-backed, conservative performance calibration for Workbench routing.

This module intentionally owns no scheduler or SQLite schema.  The durable
Workbench event log is the raw runtime ledger; this module materializes
content-addressed advisory snapshots from it.  A later routing owner may
consume the returned lower bounds, but must still apply all capability, role,
scope, and quota gates before dispatching a model.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
from statistics import median
import tempfile
from typing import Any, Iterable

from .ai_frontier import ai_frontier_prior_records
from .model_identities import catalog_with_model_identities, derive_model_identities
from .radar import radar_prior_records
from .store import WorkbenchStore


PERFORMANCE_SNAPSHOT_SCHEMA_VERSION = 1
PERFORMANCE_SNAPSHOT_PRODUCER = "codex-workbench.performance"
PERFORMANCE_SNAPSHOT_SOURCE = "benchmark-prior-plus-runtime-ledger-v1"
BASELINE_RESOURCE_PACKAGE = "codex_workbench.data"
BASELINE_RESOURCE_NAME = "model-performance-baseline-v1.json"
_FAMILY_TRANSFER_MULTIPLIER = 0.25
_CONSERVATIVE_Z = 1.96
_GENERATION_ID = re.compile(r"^performance-[0-9a-f]{16,64}$")
_CODING_TASK_TYPES = frozenset({"implementation", "debugging", "tests", "docs"})
_REASONING_TASK_TYPES = frozenset({"architecture", "review", "research", "exploration"})
_TERMINAL_EVENTS = frozenset(
    {"node.accepted", "node.failed", "node.blocked", "node.indeterminate"}
)
_FINAL_TASK_STATES = frozenset({"accepted", "needs_fix"})
_PERFORMANCE_TASK_STATES = frozenset({"accepted", "needs_fix", "blocked", "cancelled"})


class PerformanceRegistryError(ValueError):
    """A performance baseline or persisted snapshot is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_benchmark_baseline() -> dict[str, Any]:
    """Load and validate the shipped, versioned public benchmark baseline."""

    try:
        text = (
            resources.files(BASELINE_RESOURCE_PACKAGE)
            .joinpath(BASELINE_RESOURCE_NAME)
            .read_text(encoding="utf-8")
        )
        raw = json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError) as error:
        raise PerformanceRegistryError("benchmark baseline resource is unreadable") from error
    return validate_benchmark_baseline(raw)


def validate_benchmark_baseline(raw: Mapping[str, Any] | object) -> dict[str, Any]:
    """Reject malformed baseline data before it becomes a routing prior."""

    if not isinstance(raw, Mapping):
        raise PerformanceRegistryError("benchmark baseline must be an object")
    if raw.get("schema_version") != 1:
        raise PerformanceRegistryError("unsupported benchmark baseline schema version")
    if raw.get("producer") != PERFORMANCE_SNAPSHOT_PRODUCER:
        raise PerformanceRegistryError("invalid benchmark baseline producer")
    _require_text(raw.get("baseline_id"), "baseline_id")
    _require_text(raw.get("purpose"), "purpose")
    records = raw.get("records")
    if not isinstance(records, list) or not records:
        raise PerformanceRegistryError("benchmark baseline must contain records")

    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, Mapping):
            raise PerformanceRegistryError("benchmark baseline record must be an object")
        record = dict(item)
        record_id = _require_text(record.get("record_id"), "record_id")
        if record_id in seen:
            raise PerformanceRegistryError("benchmark baseline contains duplicate record IDs")
        seen.add(record_id)
        for field in (
            "source_url",
            "benchmark",
            "benchmark_version",
            "domain",
            "provider",
            "model_id",
            "model_family",
            "agent_scaffold",
            "score_kind",
            "provenance",
            "quality_evidence",
        ):
            _require_text(record.get(field), f"record {record_id} {field}")
        if not str(record["source_url"]).startswith("https://"):
            raise PerformanceRegistryError(f"record {record_id} source_url must use https")
        if record["provenance"] not in {
            "vendor_report",
            "independent",
            "community_observation",
        }:
            raise PerformanceRegistryError(f"record {record_id} has invalid provenance")
        task_types = record.get("task_types")
        if not isinstance(task_types, list) or not task_types or not all(
            isinstance(value, str) and value.strip() for value in task_types
        ):
            raise PerformanceRegistryError(f"record {record_id} task_types must be a non-empty string list")
        score = record.get("score")
        if score is not None and (
            not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1
        ):
            raise PerformanceRegistryError(f"record {record_id} score must be a percentage in [0, 1] or null")
        external_signals = record.get("external_signals")
        if external_signals is not None:
            _validate_external_signals(external_signals, record_id)
        for field in ("transfer_weight", "effective_sample_strength"):
            value = record.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
                raise PerformanceRegistryError(f"record {record_id} {field} must be non-negative")
        if score is None and float(record["effective_sample_strength"]) != 0:
            raise PerformanceRegistryError(
                f"record {record_id} cannot assign pseudo-observations without a published score"
            )
        if score is not None and record.get("routing_prior_eligible", True) is True:
            if float(record["effective_sample_strength"]) <= 0:
                raise PerformanceRegistryError(
                    f"record {record_id} needs positive effective_sample_strength when routable"
                )
        validated.append(json.loads(json.dumps(record)))

    return {
        "schema_version": 1,
        "baseline_id": str(raw["baseline_id"]),
        "producer": PERFORMANCE_SNAPSHOT_PRODUCER,
        "purpose": str(raw["purpose"]),
        "records": validated,
    }


def read_all_events(store: WorkbenchStore, *, page_size: int = 500) -> list[dict[str, Any]]:
    """Read every append-only event page without relying on a fixed history limit."""

    if page_size <= 0:
        raise ValueError("event page_size must be positive")
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        page = store.read_events(after=after, limit=page_size)
        if not page:
            break
        advanced = [
            event
            for event in page
            if isinstance(event, Mapping)
            and isinstance(event.get("cursor"), int)
            and int(event["cursor"]) > after
        ]
        if not advanced:
            raise PerformanceRegistryError("event page did not advance its cursor")
        advanced.sort(key=lambda event: int(event["cursor"]))
        events.extend(dict(event) for event in advanced)
        after = int(advanced[-1]["cursor"])
        if len(page) < page_size:
            break
    return events


def read_all_tasks(
    store: WorkbenchStore,
    events: Iterable[Mapping[str, Any]],
    *,
    fallback_limit: int = 50_000,
) -> list[dict[str, Any]]:
    """Recover task snapshots for every task ID seen in the durable event ledger.

    WorkbenchStore exposes cursor pagination for the append-only event ledger.
    Task rows are current-state snapshots, so event IDs are the authoritative
    way to avoid silently truncating historical calibration data.  A large
    current-state list remains a compatibility fallback for imported ledgers.
    """

    if fallback_limit <= 0:
        raise ValueError("task fallback_limit must be positive")
    task_ids = {
        str(event["task_id"])
        for event in events
        if event.get("task_id") is not None and str(event.get("task_id")).strip()
    }
    known = {
        str(task["task_id"]): dict(task)
        for task in store.list_tasks(limit=fallback_limit)
        if isinstance(task, Mapping) and task.get("task_id") is not None
    }
    get_task = getattr(store, "get_task", None)
    if callable(get_task):
        for task_id in sorted(task_ids - set(known)):
            try:
                task = get_task(task_id)
            except KeyError:
                # Deleted/foreign event history cannot be calibrated because
                # there is no durable task contract or node spec left to bind.
                continue
            if isinstance(task, Mapping):
                known[task_id] = dict(task)
    return [known[task_id] for task_id in sorted(known)]


@dataclass(frozen=True)
class _Attempt:
    task_id: str
    node_id: str
    attempt: int
    cursor: int
    status: str
    provider: str
    model_id: str
    agent_name: str
    agent_version: str
    reasoning_effort: str
    task_type: str
    complexity: str
    task_state: str | None
    duration_seconds: float | None
    quality_outcome_eligible: bool


def compute_runtime_metrics(
    events: Iterable[Mapping[str, Any]],
    tasks: Iterable[Mapping[str, Any]],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize model-version/task buckets from terminal node events.

    Fixture, deterministic, verifier, cached-Evidence, and unattested-model
    samples remain visible in the exclusion ledger but never influence quality
    calibration.  The function has no side effects and can therefore be used
    for deterministic snapshot regeneration and focused tests.
    """

    active_baseline = validate_benchmark_baseline(
        baseline if baseline is not None else load_benchmark_baseline()
    )
    ordered = sorted(
        (dict(event) for event in events if isinstance(event, Mapping)),
        key=lambda event: int(event.get("cursor", 0)),
    )
    task_index = {
        str(task["task_id"]): dict(task)
        for task in tasks
        if isinstance(task, Mapping) and task.get("task_id") is not None
    }
    node_index: dict[tuple[str, str], dict[str, Any]] = {}
    task_states: dict[str, str | None] = {}
    for task_id, task in task_index.items():
        state = task.get("state")
        task_states[task_id] = str(state) if isinstance(state, str) else None
        for node in task.get("nodes", ()):
            if isinstance(node, Mapping) and node.get("node_id") is not None:
                node_index[(task_id, str(node["node_id"]))] = dict(node)

    starts: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    terminals: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    reused: set[tuple[str, str]] = set()
    retry_scheduled: dict[tuple[str, str], int] = defaultdict(int)
    terminal_states_from_events: dict[str, str] = {}

    for event in ordered:
        event_type = str(event.get("event_type", ""))
        task_id = event.get("task_id")
        node_id = event.get("node_id")
        payload = event.get("payload")
        payload_mapping = payload if isinstance(payload, Mapping) else {}
        if event_type == "task.state_changed" and task_id is not None:
            to = payload_mapping.get("to")
            if isinstance(to, str):
                terminal_states_from_events[str(task_id)] = to
        if task_id is None or node_id is None:
            continue
        key = (str(task_id), str(node_id))
        if event_type == "node.evidence_reused":
            reused.add(key)
        if event_type in {"node.retry_scheduled", "task.repair_scheduled"}:
            retry_scheduled[key] += 1
        if event_type == "node.started":
            attempt = _attempt_number(payload_mapping)
            if attempt is not None:
                starts[(key[0], key[1], attempt)] = event
        if event_type in _TERMINAL_EVENTS:
            attempt = _attempt_number(payload_mapping)
            if attempt is not None:
                terminals[(key[0], key[1], attempt)] = event

    for task_id, state in terminal_states_from_events.items():
        task_states.setdefault(task_id, state)
        if task_states.get(task_id) is None:
            task_states[task_id] = state

    excluded: dict[str, int] = defaultdict(int)
    attempts: list[_Attempt] = []
    for (task_id, node_id, attempt), event in sorted(
        terminals.items(), key=lambda item: (int(item[1].get("cursor", 0)), item[0])
    ):
        spec = node_index.get((task_id, node_id))
        if spec is None:
            excluded["missing_node_spec"] += 1
            continue
        if (
            spec.get("verifier") is True
            or str(spec.get("executor", "")).lower() in {"fixture", "deterministic"}
            or str(spec.get("model", "")).lower() == "fixture"
        ):
            excluded["fixture_deterministic_or_verifier"] += 1
            continue
        if (task_id, node_id) in reused:
            excluded["evidence_reused"] += 1
            continue
        payload = event.get("payload")
        result = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(result, Mapping):
            excluded["missing_result"] += 1
            continue
        if result.get("result_kind") == "verifier":
            excluded["verifier_result"] += 1
            continue
        model_id = _text(result.get("actual_model"))
        if model_id is None:
            excluded["missing_actual_model"] += 1
            continue
        provider = _text(result.get("provider")) or _text(spec.get("executor"))
        if provider not in {"codex", "claude"}:
            excluded["unsupported_provider"] += 1
            continue
        task = task_index.get(task_id, {})
        contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else {}
        task_type = _normalized_task_type(spec.get("task_type") or contract.get("task_type"))
        complexity = _normalized_complexity(spec.get("complexity") or contract.get("complexity"))
        start = starts.get((task_id, node_id, attempt))
        start_payload = (
            start.get("payload")
            if isinstance(start, Mapping) and isinstance(start.get("payload"), Mapping)
            else {}
        )
        agent_version = (
            _text(result.get("agent_version"))
            or _text(spec.get("agent_version"))
            or "unattested"
        )
        reasoning_effort = (
            _text(result.get("model_reasoning_effort"))
            or _text(start_payload.get("model_reasoning_effort"))
            or _text(spec.get("model_reasoning_effort"))
            or "unspecified"
        )
        attempts.append(
            _Attempt(
                task_id=task_id,
                node_id=node_id,
                attempt=attempt,
                cursor=int(event.get("cursor", 0)),
                status=str(event.get("event_type", "")).removeprefix("node."),
                provider=provider,
                model_id=model_id,
                agent_name=_text(result.get("agent_name")) or _text(spec.get("agent_name")) or provider,
                agent_version=agent_version,
                reasoning_effort=reasoning_effort,
                task_type=task_type,
                complexity=complexity,
                task_state=task_states.get(task_id),
                duration_seconds=_duration_seconds(start, event),
                # A successful process can still produce a semantically bad
                # worker result (for example invalid structured output), which
                # is useful quality evidence.  A non-zero/unknown process exit,
                # auth/quota block, timeout, or indeterminate result is an
                # operational outcome until an explicit failure-origin receipt
                # exists; do not silently charge it to model quality.
                quality_outcome_eligible=(
                    event.get("event_type") in {"node.accepted", "node.failed"}
                    and isinstance(result.get("exit_code"), int)
                    and not isinstance(result.get("exit_code"), bool)
                    and int(result["exit_code"]) == 0
                    and agent_version != "unattested"
                ),
            )
        )

    grouped: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    logical: dict[tuple[str, str], list[_Attempt]] = defaultdict(list)
    for attempt in attempts:
        group = grouped.setdefault(_attempt_group_key(attempt), _empty_group(attempt))
        group["attempt_count"] += 1
        if attempt.status == "failed":
            group["failed_count"] += 1
        elif attempt.status == "blocked":
            group["blocked_count"] += 1
        elif attempt.status == "indeterminate":
            group["indeterminate_count"] += 1
        if attempt.duration_seconds is not None:
            group["durations"].append(attempt.duration_seconds)
        logical[(attempt.task_id, attempt.node_id)].append(attempt)

    for node_attempts in logical.values():
        node_attempts.sort(key=lambda item: (item.attempt, item.cursor))
        first = node_attempts[0]
        final = node_attempts[-1]
        first_group = grouped[_attempt_group_key(first)]
        final_group = grouped[_attempt_group_key(final)]
        terminal_task = final.task_state in _FINAL_TASK_STATES
        if terminal_task and first.quality_outcome_eligible:
            first_group["first_pass_total"] += 1
            if len(node_attempts) == 1 and final.task_state == "accepted" and first.status == "accepted":
                first_group["first_pass_accepted"] += 1
        if terminal_task and final.quality_outcome_eligible:
            final_group["final_total"] += 1
            if final.task_state == "accepted" and final.status == "accepted":
                final_group["final_accepted"] += 1

        first_group["rework_count"] += max(0, len(node_attempts) - 1)
        first_group["quality_rework_count"] += sum(
            1 for node_attempt in node_attempts[:-1]
            if node_attempt.quality_outcome_eligible
        )
        first_group["retry_scheduled_count"] += retry_scheduled[(first.task_id, first.node_id)]

        # Every non-final attempt failed to close its logical node.  Attribute
        # that rework to the model that performed it.  The final attempt is a
        # success only when the whole task reached accepted, not merely when a
        # worker returned a local success receipt.
        for index, node_attempt in enumerate(node_attempts):
            group = grouped[_attempt_group_key(node_attempt)]
            if not node_attempt.quality_outcome_eligible:
                group["quality_unresolved"] += 1
            elif index < len(node_attempts) - 1:
                group["quality_failures"] += 1
            elif node_attempt.task_state == "accepted" and node_attempt.status == "accepted":
                group["quality_successes"] += 1
            elif node_attempt.task_state == "needs_fix":
                group["quality_failures"] += 1
            else:
                group["quality_unresolved"] += 1

    metrics = [
        _finish_group(group, active_baseline)
        for _, group in sorted(grouped.items(), key=lambda item: item[0])
    ]
    relevant = [event for event in ordered if _is_performance_relevant_event(event)]
    calibration_cursor = max((int(event.get("cursor", 0)) for event in relevant), default=0)
    return {
        "event_cursor": calibration_cursor,
        "metrics": metrics,
        "ledger": {
            "source": "workbench-store.append-only-events",
            "calibration_event_count": len(relevant),
            "eligible_terminal_attempts": len(attempts),
            "excluded_terminal_attempts": dict(sorted(excluded.items())),
            "logical_nodes": len(logical),
        },
        # Scan progress is useful operational evidence, but it is explicitly
        # outside the content-addressed body.  A quota heartbeat or unrelated
        # system event must not create an otherwise identical generation.
        "scan_progress": {
            "scanned_event_cursor": max(
                (int(event.get("cursor", 0)) for event in ordered), default=0
            ),
            "events_read": len(ordered),
            "tasks_read": len(task_index),
            "calibration_cursor": calibration_cursor,
            "calibration_event_count": len(relevant),
        },
    }


def build_performance_snapshot(
    events: Iterable[Mapping[str, Any]],
    tasks: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
    *,
    quota: object | None = None,
    baseline: Mapping[str, Any] | None = None,
    radar_status: Mapping[str, Any] | None = None,
    ai_frontier_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, content-addressed performance generation."""

    active_baseline = validate_benchmark_baseline(
        baseline if baseline is not None else load_benchmark_baseline()
    )
    events = list(events)
    tasks = list(tasks)
    identities = derive_model_identities(events, tasks, catalog)
    observed_catalog = catalog_with_model_identities(catalog, identities)
    radar_records = radar_prior_records(radar_status or {}, observed_catalog)
    ai_frontier_records = ai_frontier_prior_records(ai_frontier_status or {}, observed_catalog)
    external_records = [*radar_records, *ai_frontier_records]
    if external_records:
        active_baseline = validate_benchmark_baseline(
            {
                **active_baseline,
                "records": [*active_baseline["records"], *external_records],
            }
        )
    runtime = compute_runtime_metrics(events, tasks, baseline=active_baseline)
    catalog_identity = {
        "catalog_id": _text(catalog.get("catalog_id")) if isinstance(catalog, Mapping) else None,
        "catalog_digest": _text(catalog.get("digest")) if isinstance(catalog, Mapping) else None,
    }
    baseline_digest = canonical_hash(active_baseline)
    sources = [
        {
            "record_id": record["record_id"],
            "source_url": record["source_url"],
            "benchmark": record["benchmark"],
            "benchmark_version": record["benchmark_version"],
            "domain": record["domain"],
            "provenance": record["provenance"],
            "quality_evidence": record["quality_evidence"],
        }
        for record in active_baseline["records"]
    ]
    source_provenance: dict[str, Any] = {
        "model_identities": identities,
        "benchmark_records": sources,
        "runtime_ledger": {
            "kind": "append-only-events-and-current-task-contracts",
            "event_cursor": runtime["event_cursor"],
        },
    }
    if radar_status is not None:
        source_provenance["external_priors"] = {
            "codex_radar": _radar_provenance(radar_status, len(radar_records))
        }
    if ai_frontier_status is not None:
        source_provenance.setdefault("external_priors", {})[
            "ai_frontier"
        ] = _ai_frontier_provenance(
            ai_frontier_status,
            len(ai_frontier_records),
        )
        imported_ids = {str(record["model_id"]) for record in ai_frontier_records}
        routable = [record for record in observed_catalog.get("models", []) if record.get("routable") is True]
        matched = [
            str(record["model_id"])
            for record in routable
            if str(record.get("identity", {}).get("canonical_model_id") or record["model_id"]) in imported_ids
        ]
        source_provenance["external_priors"]["ai_frontier"].update({
            "imported_model_count": len(imported_ids),
            "matched_selection_ids": matched,
            "routable_model_count": len(routable),
            "model_coverage_rate": len(matched) / len(routable) if routable else None,
            "used_for_prior": bool(ai_frontier_records),
        })
    body = {
        "schema_version": PERFORMANCE_SNAPSHOT_SCHEMA_VERSION,
        "producer": PERFORMANCE_SNAPSHOT_PRODUCER,
        "source": PERFORMANCE_SNAPSHOT_SOURCE,
        "event_cursor": runtime["event_cursor"],
        "catalog": catalog_identity,
        "baseline": {
            **active_baseline,
            "digest": baseline_digest,
        },
        "ledger": runtime["ledger"],
        "metrics": runtime["metrics"],
        "pools": _quota_pools(quota),
        "source_provenance": source_provenance,
        "advisory_policy": {
            "quality_first": True,
            "hard_capability_gates_required": True,
            "routing_override_permitted": False,
            "note": (
                "Performance posterior is advisory input only. Capability, role, "
                "scope, quota, and verification gates remain authoritative."
            ),
        },
    }
    digest = canonical_hash(body)
    snapshot = {
        **body,
        "snapshot_id": f"performance-{digest[:16]}",
        "digest": digest,
        "scan_progress": {
            **runtime["scan_progress"],
            "claude_observed_at": _quota_field(quota, "observed_at") if quota is not None else None,
        },
    }
    return validate_performance_snapshot(snapshot)


def validate_performance_snapshot(raw: Mapping[str, Any] | object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PerformanceRegistryError("performance snapshot must be an object")
    if raw.get("schema_version") != PERFORMANCE_SNAPSHOT_SCHEMA_VERSION:
        raise PerformanceRegistryError("unsupported performance snapshot schema version")
    if raw.get("producer") != PERFORMANCE_SNAPSHOT_PRODUCER:
        raise PerformanceRegistryError("invalid performance snapshot producer")
    if raw.get("source") != PERFORMANCE_SNAPSHOT_SOURCE:
        raise PerformanceRegistryError("invalid performance snapshot source")
    snapshot_id = _require_text(raw.get("snapshot_id"), "snapshot_id")
    if _GENERATION_ID.fullmatch(snapshot_id) is None:
        raise PerformanceRegistryError("invalid performance snapshot generation ID")
    digest = _require_text(raw.get("digest"), "digest")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PerformanceRegistryError("invalid performance snapshot digest")
    for field in ("catalog", "baseline", "ledger", "pools", "source_provenance", "advisory_policy"):
        if not isinstance(raw.get(field), Mapping):
            raise PerformanceRegistryError(f"performance snapshot {field} must be an object")
    if not isinstance(raw.get("metrics"), list):
        raise PerformanceRegistryError("performance snapshot metrics must be a list")
    if not isinstance(raw.get("event_cursor"), int) or int(raw["event_cursor"]) < 0:
        raise PerformanceRegistryError("performance snapshot event_cursor must be non-negative")
    if "scan_progress" in raw and not isinstance(raw["scan_progress"], Mapping):
        raise PerformanceRegistryError("performance snapshot scan_progress must be an object")
    baseline = validate_benchmark_baseline(raw["baseline"])
    baseline_digest = _require_text(raw["baseline"].get("digest"), "baseline digest")
    if baseline_digest != canonical_hash(baseline):
        raise PerformanceRegistryError("performance snapshot baseline digest does not match its contents")
    body = {
        key: raw[key]
        for key in (
            "schema_version",
            "producer",
            "source",
            "event_cursor",
            "catalog",
            "baseline",
            "ledger",
            "metrics",
            "pools",
            "source_provenance",
            "advisory_policy",
        )
    }
    expected = canonical_hash(body)
    if expected != digest or snapshot_id != f"performance-{digest[:16]}":
        raise PerformanceRegistryError("performance snapshot digest does not match its contents")
    return json.loads(json.dumps(dict(raw)))


@dataclass(frozen=True)
class PerformanceRegistry:
    """Versioned materialized views of the durable runtime performance ledger."""

    state_root: Path
    event_page_size: int = 500
    task_fallback_limit: int = 50_000

    @property
    def root(self) -> Path:
        return self.state_root / "performance"

    @property
    def generations_dir(self) -> Path:
        return self.root / "generations"

    @property
    def active_path(self) -> Path:
        return self.root / "active.json"

    def refresh(
        self,
        store: WorkbenchStore,
        catalog: Mapping[str, Any],
        *,
        radar_status: Mapping[str, Any] | None = None,
        ai_frontier_status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the full durable ledger and atomically activate its generation."""

        events = read_all_events(store, page_size=self.event_page_size)
        tasks = read_all_tasks(store, events, fallback_limit=self.task_fallback_limit)
        snapshot = build_performance_snapshot(
            events,
            tasks,
            catalog,
            quota=_latest_quota(store),
            radar_status=radar_status,
            ai_frontier_status=ai_frontier_status,
        )
        current = self.active()
        unchanged = current is not None and current["digest"] == snapshot["digest"]
        effective = current if unchanged else snapshot
        if not unchanged:
            self._write_generation(snapshot)
        self._activate(effective["snapshot_id"])
        return {
            "ok": True,
            "snapshot": effective,
            "active_generation_id": effective["snapshot_id"],
            "activated": not unchanged,
            "unchanged": unchanged,
            # Report the current full-ledger scan even when its changes are
            # intentionally non-material to the active generation identity.
            "scan_progress": snapshot["scan_progress"],
        }

    def active(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        pointer = self._read_pointer()
        return self.load_generation(pointer["active_generation_id"])

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        self._validate_generation_id(generation_id)
        path = self.generations_dir / f"{generation_id}.json"
        if not path.exists():
            raise PerformanceRegistryError(
                f"performance snapshot generation {generation_id!r} is missing"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerformanceRegistryError(
                f"performance snapshot generation {generation_id!r} is unreadable"
            ) from error
        snapshot = validate_performance_snapshot(raw)
        if snapshot["snapshot_id"] != generation_id:
            raise PerformanceRegistryError("performance generation filename does not match content")
        return snapshot

    def status(self) -> dict[str, Any]:
        active: dict[str, Any] | None = None
        error: str | None = None
        try:
            active = self.active()
        except PerformanceRegistryError as exc:
            error = str(exc)
        generations = self._generation_ids()
        return {
            "ok": active is not None and error is None,
            "active_generation_id": active["snapshot_id"] if active is not None else None,
            "active": active,
            "generation_count": len(generations),
            "generations": generations,
            "error": error,
        }

    def calibrate(
        self,
        catalog: Mapping[str, Any],
        task_type: str,
        complexity: str,
    ) -> dict[str, Any]:
        """Return conservative per-candidate advisory quality, never a route.

        A new CLI version is deliberately isolated from metrics observed under
        an older version.  It gets its weak public prior until enough locally
        attested executions accumulate in its own bucket.
        """

        snapshot = self.active()
        active_baseline = (
            validate_benchmark_baseline(snapshot["baseline"])
            if snapshot is not None
            else load_benchmark_baseline()
        )
        normalized_task_type = _normalized_task_type(task_type)
        normalized_complexity = _normalized_complexity(complexity)
        return self._calibrate_context(
            snapshot,
            catalog,
            active_baseline,
            task_type=normalized_task_type,
            complexity=normalized_complexity,
        )

    def calibrate_matrix(
        self,
        catalog: Mapping[str, Any],
        task_types: Iterable[object],
        complexities: Iterable[object],
    ) -> dict[str, Any]:
        """Materialize deterministic per-DAG-node calibration contexts.

        The active generation is read once, then every task-type and complexity
        pair is calibrated against that same immutable ledger snapshot.  This
        intentionally keeps runtime evidence in its exact bucket rather than
        transferring it across neighboring DAG node contexts.
        """

        if isinstance(task_types, (str, bytes)) or isinstance(complexities, (str, bytes)):
            raise PerformanceRegistryError(
                "performance calibration matrix axes must be iterable collections, not strings"
            )
        normalized_task_types = sorted({_normalized_task_type(value) for value in task_types})
        normalized_complexities = sorted(
            {_normalized_complexity(value) for value in complexities}
        )
        if not normalized_task_types or not normalized_complexities:
            raise PerformanceRegistryError(
                "performance calibration matrix requires task_types and complexities"
            )
        snapshot = self.active()
        active_baseline = (
            validate_benchmark_baseline(snapshot["baseline"])
            if snapshot is not None
            else load_benchmark_baseline()
        )
        contexts = [
            self._calibrate_context(
                snapshot,
                catalog,
                active_baseline,
                task_type=task_type,
                complexity=complexity,
            )
            for task_type in normalized_task_types
            for complexity in normalized_complexities
        ]
        return {
            "status": "ok" if snapshot is not None else "cold-start",
            "snapshot_id": snapshot["snapshot_id"] if snapshot is not None else None,
            "task_types": normalized_task_types,
            "complexities": normalized_complexities,
            "contexts": contexts,
            "advisory_only": True,
            "hard_capability_gates_required": True,
            "quality_gate_bypass_permitted": False,
        }

    @staticmethod
    def _calibrate_context(
        snapshot: Mapping[str, Any] | None,
        catalog: Mapping[str, Any],
        active_baseline: Mapping[str, Any],
        *,
        task_type: str,
        complexity: str,
    ) -> dict[str, Any]:
        metric_index: dict[tuple[str, str, str, str, str, str], Mapping[str, Any]] = {}
        if snapshot is not None:
            for metric in snapshot["metrics"]:
                key = metric.get("key")
                if isinstance(key, Mapping):
                    metric_index[
                        (
                            str(key.get("provider")),
                            str(key.get("model_id")),
                            str(key.get("agent_version")),
                            _text(key.get("reasoning_effort")) or "unspecified",
                            str(key.get("task_type")),
                            str(key.get("complexity")),
                        )
                    ] = metric

        candidates: list[dict[str, Any]] = []
        if snapshot is not None:
            catalog = catalog_with_model_identities(
                catalog, snapshot["source_provenance"].get("model_identities", {})
            )
        models = catalog.get("models", ()) if isinstance(catalog, Mapping) else ()
        agents = catalog.get("agents", {}) if isinstance(catalog, Mapping) else {}
        for record in models if isinstance(models, list) else ():
            if not isinstance(record, Mapping):
                continue
            provider = _text(record.get("provider"))
            model_id = _text(record.get("model_id"))
            if provider not in {"codex", "claude"} or model_id is None:
                continue
            agent_version = _text(record.get("agent_cli_version"))
            if agent_version is None and isinstance(agents, Mapping):
                agent = agents.get(provider)
                if isinstance(agent, Mapping):
                    agent_version = _text(agent.get("cli_version"))
            agent_version = agent_version or "unattested"
            reasoning_effort = _preferred_effort(record)
            canonical_model_id = (
                _text(record.get("identity", {}).get("canonical_model_id")) or model_id
            )
            key = (
                provider,
                canonical_model_id,
                agent_version,
                reasoning_effort or "unspecified",
                task_type,
                complexity,
            )
            metric = metric_index.get(key)
            prior = _prior_for(
                active_baseline,
                provider=provider,
                model_id=canonical_model_id,
                model_family=_text(record.get("model_family")) or _model_family(model_id),
                task_type=task_type,
                reasoning_effort=reasoning_effort,
            )
            if metric is None:
                posterior = _posterior(prior["alpha"], prior["beta"])
                quality = {
                    "prior": prior,
                    "posterior": {
                        **posterior,
                        "runtime_sample_count": 0,
                        "runtime_successes": 0,
                        "runtime_failures": 0,
                    },
                }
                runtime = _empty_runtime_metrics()
            else:
                raw_runtime = metric.get("runtime")
                if not isinstance(raw_runtime, Mapping):
                    raise PerformanceRegistryError(
                        "performance metric runtime must be an object"
                    )
                runtime = dict(raw_runtime)
                quality_calibration = runtime.get("quality_calibration")
                if not isinstance(quality_calibration, Mapping):
                    raise PerformanceRegistryError(
                        "performance metric quality_calibration must be an object"
                    )
                successes = int(quality_calibration.get("successes", 0))
                failures = int(quality_calibration.get("failures", 0))
                quality = {
                    "prior": prior,
                    "posterior": {
                        **_posterior(
                            float(prior["alpha"]) + successes,
                            float(prior["beta"]) + failures,
                        ),
                        "runtime_sample_count": successes + failures,
                        "runtime_successes": successes,
                        "runtime_failures": failures,
                    },
                }
            candidates.append(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "canonical_model_id": canonical_model_id,
                    "model_family": _text(record.get("model_family")) or _model_family(model_id),
                    "reasoning_effort": reasoning_effort,
                    "agent_version": agent_version,
                    "routable": record.get("routable") is True,
                    "quality": quality,
                    "runtime": runtime,
                }
            )

        candidates.sort(key=lambda item: (item["provider"], item["model_id"], item["agent_version"]))
        return {
            "status": "ok" if snapshot is not None else "cold-start",
            "snapshot_id": snapshot["snapshot_id"] if snapshot is not None else None,
            "task_type": task_type,
            "complexity": complexity,
            "candidates": candidates,
            "advisory_only": True,
            "hard_capability_gates_required": True,
            "quality_gate_bypass_permitted": False,
        }

    def _write_generation(self, snapshot: Mapping[str, Any]) -> Path:
        valid = validate_performance_snapshot(snapshot)
        path = self.generations_dir / f"{valid['snapshot_id']}.json"
        if path.exists():
            existing = self.load_generation(valid["snapshot_id"])
            if existing["digest"] != valid["digest"]:
                raise PerformanceRegistryError("performance generation ID collision")
            return path
        self._atomic_write_json(path, valid)
        return path

    def _activate(self, generation_id: str) -> None:
        self._validate_generation_id(generation_id)
        pointer = {
            "schema_version": PERFORMANCE_SNAPSHOT_SCHEMA_VERSION,
            "producer": PERFORMANCE_SNAPSHOT_PRODUCER,
            "active_generation_id": generation_id,
        }
        self._atomic_write_json(self.active_path, pointer)

    def _read_pointer(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerformanceRegistryError("active performance pointer is unreadable") from error
        if not isinstance(raw, Mapping):
            raise PerformanceRegistryError("active performance pointer must be an object")
        if raw.get("schema_version") != PERFORMANCE_SNAPSHOT_SCHEMA_VERSION:
            raise PerformanceRegistryError("active performance pointer has unsupported schema")
        if raw.get("producer") != PERFORMANCE_SNAPSHOT_PRODUCER:
            raise PerformanceRegistryError("active performance pointer has invalid producer")
        generation_id = _require_text(raw.get("active_generation_id"), "active_generation_id")
        self._validate_generation_id(generation_id)
        return dict(raw)

    def _generation_ids(self) -> list[str]:
        if not self.generations_dir.exists():
            return []
        return sorted(
            path.stem
            for path in self.generations_dir.glob("performance-*.json")
            if _GENERATION_ID.fullmatch(path.stem) is not None
        )

    @staticmethod
    def _validate_generation_id(generation_id: str) -> None:
        if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
            raise PerformanceRegistryError("invalid performance generation ID")

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(canonical_json(dict(payload)) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _attempt_group_key(attempt: _Attempt) -> tuple[str, str, str, str, str, str]:
    return (
        attempt.provider,
        attempt.model_id,
        attempt.agent_version,
        attempt.reasoning_effort,
        attempt.task_type,
        attempt.complexity,
    )


def _empty_group(attempt: _Attempt) -> dict[str, Any]:
    return {
        "key": {
            "provider": attempt.provider,
            "model_id": attempt.model_id,
            "agent_name": attempt.agent_name,
            "agent_version": attempt.agent_version,
            "reasoning_effort": attempt.reasoning_effort,
            "task_type": attempt.task_type,
            "complexity": attempt.complexity,
        },
        "attempt_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
        "indeterminate_count": 0,
        "first_pass_total": 0,
        "first_pass_accepted": 0,
        "final_total": 0,
        "final_accepted": 0,
        "rework_count": 0,
        "quality_rework_count": 0,
        "retry_scheduled_count": 0,
        "quality_successes": 0,
        "quality_failures": 0,
        "quality_unresolved": 0,
        "durations": [],
    }


def _finish_group(group: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    key = group["key"]
    prior = _prior_for(
        baseline,
        provider=str(key["provider"]),
        model_id=str(key["model_id"]),
        model_family=_model_family(str(key["model_id"])),
        task_type=str(key["task_type"]),
        reasoning_effort=(
            None
            if str(key.get("reasoning_effort", "unspecified")) == "unspecified"
            else str(key["reasoning_effort"])
        ),
    )
    successes = int(group["quality_successes"])
    failures = int(group["quality_failures"])
    posterior = _posterior(prior["alpha"] + successes, prior["beta"] + failures)
    durations = [float(value) for value in group["durations"]]
    return {
        "key": dict(key),
        "runtime": {
            "attempt_count": int(group["attempt_count"]),
            "first_pass": _rate(
                int(group["first_pass_accepted"]),
                int(group["first_pass_total"]),
            ),
            "final_acceptance": _rate(
                int(group["final_accepted"]),
                int(group["final_total"]),
            ),
            "outcomes": {
                "failed": int(group["failed_count"]),
                "blocked": int(group["blocked_count"]),
                "indeterminate": int(group["indeterminate_count"]),
            },
            "rework_count": int(group["rework_count"]),
            "quality_rework_count": int(group["quality_rework_count"]),
            "retry_scheduled_count": int(group["retry_scheduled_count"]),
            "quality_calibration": {
                "successes": successes,
                "failures": failures,
                "unresolved": int(group["quality_unresolved"]),
                "sample_count": successes + failures,
            },
            "duration_seconds": _duration_summary(durations),
        },
        "prior": prior,
        "posterior": {
            **posterior,
            "runtime_sample_count": successes + failures,
            "runtime_successes": successes,
            "runtime_failures": failures,
        },
    }


def _prior_for(
    baseline: Mapping[str, Any],
    *,
    provider: str,
    model_id: str,
    model_family: str | None,
    task_type: str,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    exact: list[Mapping[str, Any]] = []
    family: list[Mapping[str, Any]] = []
    declarative: list[Mapping[str, Any]] = []
    for record in baseline["records"]:
        if record["provider"] != provider or task_type not in record["task_types"]:
            continue
        record_effort = _text(record.get("reasoning_effort"))
        if record_effort is not None and record_effort != reasoning_effort:
            continue
        if record["model_id"] == model_id:
            if record.get("score") is None:
                declarative.append(record)
            elif record.get("routing_prior_eligible", True) is True:
                exact.append(record)
        elif (
            model_family is not None
            and record["model_family"] == model_family
            and record.get("external_snapshot_id") is None
        ):
            if record.get("score") is None:
                declarative.append(record)
            elif record.get("routing_prior_eligible", True) is True:
                family.append(record)

    selected: list[tuple[Mapping[str, Any], float, str]] = [
        (record, float(record["transfer_weight"]), "exact-model")
        for record in exact
    ]
    if not selected:
        selected = [
            (
                record,
                float(record["transfer_weight"]) * _FAMILY_TRANSFER_MULTIPLIER,
                "family-transfer",
            )
            for record in family
        ]
    if selected:
        alpha = 1.0
        beta = 1.0
        evidence: list[dict[str, Any]] = []
        signal_sums: dict[str, float] = defaultdict(float)
        signal_weights: dict[str, float] = defaultdict(float)
        signal_sources: set[str] = set()
        for record, multiplier, match_kind in selected:
            selected_effort = _text(record.get("reasoning_effort"))
            strength = float(record["effective_sample_strength"]) * multiplier
            score = float(record["score"])
            alpha += score * strength
            beta += (1 - score) * strength
            evidence_item: dict[str, Any] = {
                "record_id": record["record_id"],
                "source_url": record["source_url"],
                "benchmark": record["benchmark"],
                "benchmark_version": record["benchmark_version"],
                "domain": record["domain"],
                "provenance": record["provenance"],
                "match_kind": match_kind,
                "effective_sample_strength": round(strength, 6),
                "agent_scaffold": record["agent_scaffold"],
                **(
                    {"reasoning_effort": selected_effort}
                    if selected_effort is not None
                    else {}
                ),
            }
            for field in ("source_id", "lineage_id", "metric_kind", "correlation_group"):
                if _text(record.get(field)) is not None:
                    evidence_item[field] = record[field]
            evidence.append(evidence_item)

            external = record.get("external_signals")
            if not isinstance(external, Mapping) or strength <= 0:
                continue
            source_id = _text(record.get("source_id")) or _text(record.get("lineage_id"))
            if source_id is not None:
                signal_sources.add(source_id)
            for field in (
                "quality_mean",
                "consistency_mean",
                "consistency_std_mean",
                "observed_cost_mean",
                "cost_surprise_mean",
            ):
                value = external.get(field)
                if field == "observed_cost_mean" and value is None:
                    # Read old snapshots produced before the neutral cost
                    # field name was adopted; never emit that legacy label.
                    value = external.get("observed_cost_usd_mean")
                value = _external_signal_number(value, field)
                if value is not None:
                    signal_sums[field] += value * strength
                    signal_weights[field] += strength
        external_signals = {
            field: (
                round(signal_sums[field] / signal_weights[field], 8)
                if signal_weights[field] > 0
                else None
            )
            for field in (
                "quality_mean",
                "consistency_mean",
                "consistency_std_mean",
                "observed_cost_mean",
                "cost_surprise_mean",
            )
        }
        external_signals["source_count"] = len(signal_sources)
        return {
            "kind": "benchmark-backed-weak-prior",
            "evidence_status": "available",
            "alpha": round(alpha, 8),
            "beta": round(beta, 8),
            "evidence": evidence,
            "external_signals": external_signals,
        }

    if declarative:
        return {
            "kind": "declarative-conservative-prior",
            "evidence_status": "unavailable",
            "alpha": 1.0,
            "beta": 3.0,
            "evidence": [
                {
                    "record_id": record["record_id"],
                    "source_url": record["source_url"],
                    "benchmark": record["benchmark"],
                    "provenance": record["provenance"],
                    "quality_evidence": record["quality_evidence"],
                    "note": "No published quality score was converted into a pass-rate prior.",
                }
                for record in declarative
            ],
            "external_signals": _empty_external_signals(),
        }
    return {
        "kind": "generic-conservative-prior",
        "evidence_status": "unavailable",
        "alpha": 1.0,
        "beta": 2.0,
        "evidence": [],
        "external_signals": _empty_external_signals(),
    }


def _empty_external_signals() -> dict[str, float | int | None]:
    return {
        "quality_mean": None,
        "consistency_mean": None,
        "consistency_std_mean": None,
        "observed_cost_mean": None,
        "cost_surprise_mean": None,
        "source_count": 0,
    }


def _external_signal_number(value: object, field: str) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if field in {"quality_mean", "consistency_mean", "consistency_std_mean"}:
        return number if 0 <= number <= 1 else None
    if field in {"observed_cost_mean", "observed_cost_usd_mean"}:
        return number if number >= 0 else None
    return number


def _validate_external_signals(value: object, record_id: str) -> None:
    if not isinstance(value, Mapping):
        raise PerformanceRegistryError(
            f"record {record_id} external_signals must be an object"
        )
    for field in (
        "quality_mean",
        "consistency_mean",
        "consistency_std_mean",
        "observed_cost_mean",
        "cost_surprise_mean",
    ):
        candidate = value.get(field)
        if field == "observed_cost_mean" and candidate is None:
            # Accept the legacy input key while keeping the canonical output
            # neutral because this source does not publish a USD unit.
            candidate = value.get("observed_cost_usd_mean")
        if candidate is not None:
            if _external_signal_number(candidate, field) is None:
                raise PerformanceRegistryError(
                    f"record {record_id} external_signals {field} is invalid"
                )
    source_count = value.get("source_count")
    if source_count is not None and (
        isinstance(source_count, bool)
        or not isinstance(source_count, int)
        or source_count < 0
    ):
        raise PerformanceRegistryError(
            f"record {record_id} external_signals source_count is invalid"
        )


def _preferred_effort(record: Mapping[str, Any]) -> str | None:
    reasoning = record.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return None
    for field in ("preferred_effort", "default_effort"):
        value = _text(reasoning.get(field))
        if value is not None:
            return value
    supported = reasoning.get("supported_efforts")
    if isinstance(supported, list) and len(supported) == 1:
        return _text(supported[0])
    return None


def _radar_provenance(status: Mapping[str, Any], imported_records: int) -> dict[str, Any]:
    return {
        "provider": "codex-radar-provider",
        "state": status.get("state"),
        "routing_prior_eligible": status.get("routing_prior_eligible") is True,
        "snapshot_id": status.get("snapshot_id"),
        "digest": status.get("digest"),
        "fetched_at": status.get("fetched_at"),
        "source_updated_at": status.get("source_updated_at"),
        "transfer_multiplier": status.get("transfer_multiplier", 0.0),
        "imported_record_count": imported_records,
        "attribution": status.get("attribution"),
        "offline_last_known_good": status.get("offline_cache_available") is True,
        "iq_used_as_pass_rate": False,
    }


def _ai_frontier_provenance(
    status: Mapping[str, Any],
    imported_records: int,
) -> dict[str, Any]:
    """Persist AI Frontier freshness and source identity without routing authority."""

    return {
        "provider": "ai-frontier-provider",
        "state": status.get("state"),
        "routing_prior_eligible": status.get("routing_prior_eligible") is True,
        "snapshot_id": status.get("snapshot_id"),
        "digest": status.get("digest"),
        "fetched_at": status.get("fetched_at"),
        "source_updated_at": status.get("source_updated_at"),
        "transfer_multiplier": status.get("transfer_multiplier", 0.0),
        "imported_record_count": imported_records,
        "offline_last_known_good": status.get("offline_cache_available") is True,
        "authorization_status": status.get("snapshot_authorization"),
        "quality_is_accuracy_pseudo_evidence": True,
        "quality_is_local_first_pass": False,
        "consistency_is_success_rate": False,
        "cost_is_quota_admission": False,
    }


def _posterior(alpha: float, beta: float) -> dict[str, float]:
    total = alpha + beta
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1))
    lower = max(0.0, mean - _CONSERVATIVE_Z * math.sqrt(variance))
    return {
        "alpha": round(alpha, 8),
        "beta": round(beta, 8),
        "mean": round(mean, 8),
        "lower_bound_95": round(lower, 8),
    }


def _rate(accepted: int, total: int) -> dict[str, int | float | None]:
    return {
        "accepted": accepted,
        "total": total,
        "rate": round(accepted / total, 8) if total else None,
    }


def _duration_summary(durations: list[float]) -> dict[str, float | int | None]:
    if not durations:
        return {"sample_count": 0, "mean": None, "p50": None}
    return {
        "sample_count": len(durations),
        "mean": round(sum(durations) / len(durations), 6),
        "p50": round(float(median(durations)), 6),
    }


def _empty_runtime_metrics() -> dict[str, Any]:
    """Explicit zero-value runtime contract for a bucket with no observations."""

    return {
        "attempt_count": 0,
        "first_pass": _rate(0, 0),
        "final_acceptance": _rate(0, 0),
        "outcomes": {"failed": 0, "blocked": 0, "indeterminate": 0},
        "rework_count": 0,
        "retry_scheduled_count": 0,
        "quality_calibration": {
            "successes": 0,
            "failures": 0,
            "unresolved": 0,
            "sample_count": 0,
        },
        "duration_seconds": _duration_summary([]),
    }


def _quota_pools(quota: object | None) -> dict[str, Any]:
    unavailable = {
        "status": "not-observable",
        "remaining": None,
        "remaining_display": "N/A",
    }
    pools: dict[str, Any] = {
        "codex": {
            **unavailable,
            "reason": "No passive Codex subscription remaining-quota collector is available.",
        },
        "spark": {
            **unavailable,
            "reason": (
                "Spark has a separate, demand-adjusted research-preview rate limit; "
                "Workbench does not fabricate a remaining balance."
            ),
            "separate_rate_limit": True,
            "source_url": "https://openai.com/index/introducing-gpt-5-3-codex-spark/",
        },
    }
    if quota is None or not _compatible_quota(quota):
        pools["claude"] = {
            **unavailable,
            "reason": "No compatible current Claude subscription quota snapshot is available.",
        }
        return pools
    pools["claude"] = {
        "status": "observed",
        "remaining": {
            "five_hour": _quota_field(quota, "five_hour_remaining"),
            "weekly_all": _quota_field(quota, "weekly_all_remaining"),
            "weekly_sonnet": _quota_field(quota, "weekly_sonnet_remaining"),
            "weekly_fable": _quota_field(quota, "weekly_fable_remaining"),
        },
        "window_ids": {
            "five_hour": _quota_field(quota, "five_hour_window_id"),
            "weekly": _quota_field(quota, "weekly_window_id"),
        },
        "source": _quota_field(quota, "source"),
        "producer": _quota_field(quota, "producer"),
        "claude_version": _quota_field(quota, "claude_version"),
    }
    return pools


def _compatible_quota(quota: object) -> bool:
    checker = getattr(quota, "has_compatible_subscription_provenance", None)
    if callable(checker):
        return bool(checker()) and _quota_field(quota, "auth_ok") is True and (
            _quota_field(quota, "auth_method") == "native-subscription"
        )
    return False


def _quota_field(quota: object, name: str) -> Any:
    return getattr(quota, name, None)


def _latest_quota(store: WorkbenchStore) -> object | None:
    reader = getattr(store, "latest_quota", None)
    return reader() if callable(reader) else None


def _attempt_number(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("attempt")
    if isinstance(value, int) and value > 0:
        return value
    return None


def _is_performance_relevant_event(event: Mapping[str, Any]) -> bool:
    """Whether an event can alter calibration or scheduling evidence.

    ``quota.updated`` is intentionally not a cursor driver: the canonical
    quota-pool payload below carries the actual remaining balance and window
    identity.  Its observed-at heartbeat is scan metadata only.
    """

    event_type = event.get("event_type")
    if event_type in _TERMINAL_EVENTS | {
        "node.started",
        "node.retry_scheduled",
        "task.repair_scheduled",
        "node.evidence_reused",
    }:
        return True
    if event_type != "task.state_changed":
        return False
    payload = event.get("payload")
    return isinstance(payload, Mapping) and payload.get("to") in _PERFORMANCE_TASK_STATES


def _duration_seconds(
    started: Mapping[str, Any] | None,
    terminal: Mapping[str, Any],
) -> float | None:
    if started is None:
        return None
    start = _parse_time(started.get("created_at"))
    end = _parse_time(terminal.get("created_at"))
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 6)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalized_task_type(value: object) -> str:
    text = _text(value)
    return text.lower() if text is not None else "implementation"


def _normalized_complexity(value: object) -> str:
    text = _text(value)
    return text.lower() if text is not None else "standard"


def _model_family(model_id: str) -> str | None:
    normalized = model_id.lower()
    for family in ("spark", "luna", "terra", "sol", "fable", "opus", "sonnet"):
        if family in normalized:
            return family
    return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_text(value: object, name: str) -> str:
    text = _text(value)
    if text is None:
        raise PerformanceRegistryError(f"performance {name} must be a non-empty string")
    return text
