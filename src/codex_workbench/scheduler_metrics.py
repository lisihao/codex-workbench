from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping

from .execution_attribution import ExecutionAttribution
from .model import derive_execution_lane, derive_quota_pool_id

if TYPE_CHECKING:
    from .store import WorkbenchStore


EXECUTION_LANES = ("spark", "general", "control")
_CLAIMABLE_TASK_STATES = frozenset({"queued", "running", "verifying", "needs_fix"})
_TERMINAL_NODE_EVENTS = {
    "node.accepted": "accepted",
    "node.failed": "failed",
    "node.blocked": "blocked",
    "node.indeterminate": "indeterminate",
    "node.cancelled": "cancelled",
}
_FAILURE_ORIGINS = (
    "environment",
    "auth",
    "quota",
    "transport",
    "model",
    "verification",
    "scope",
    "cancel",
    "unknown",
)


def execution_lane_for_spec(spec: Mapping[str, Any]) -> str:
    """Classify a durable node spec without trusting a non-Spark Spark label.

    The lane is re-derived from executor/model/verifier metadata.  A malformed
    persisted label therefore cannot send a non-Spark node through the Spark
    queue or dilute its quality gate.
    """

    derived = derive_execution_lane(
        spec.get("executor", ""),
        spec.get("model", ""),
        verifier=spec.get("verifier") is True,
    )
    if derived == "spark":
        return "spark"
    if derived == "control":
        return "control"
    return "general"


def quota_pool_id_for_spec(spec: Mapping[str, Any]) -> str:
    """Return the durable quota/capacity pool label for a node.

    This is a label for scheduling and observation, not an assertion that a
    provider exposes a remaining subscription balance.  In particular,
    Codex/Spark balances remain unobservable until a provider supplies them.
    """

    return derive_quota_pool_id(
        spec.get("executor", ""),
        spec.get("model", ""),
        verifier=spec.get("verifier") is True,
    )


def build_scheduler_metrics(
    store: WorkbenchStore,
    *,
    now: datetime | None = None,
    window_seconds: int = 3600,
    max_workers: int = 4,
    spark_workers: int | None = None,
) -> dict[str, Any]:
    """Replay the durable task/node/event ledger into scheduler metrics."""

    return compute_scheduler_metrics(
        store.list_tasks(limit=10_000),
        _read_all_events(store),
        now=now,
        window_seconds=window_seconds,
        max_workers=max_workers,
        spark_workers=spark_workers,
    )


def _read_all_events(
    store: WorkbenchStore,
    *,
    page_size: int = 5_000,
) -> list[dict[str, Any]]:
    """Read every event page so a long-lived ledger never hides recent work."""

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
            raise ValueError("event page did not advance its cursor")
        advanced.sort(key=lambda event: int(event["cursor"]))
        events.extend(dict(event) for event in advanced)
        after = int(advanced[-1]["cursor"])
        if len(page) < page_size:
            break
    return events


def compute_scheduler_metrics(
    tasks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_seconds: int = 3600,
    max_workers: int = 4,
    spark_workers: int | None = None,
) -> dict[str, Any]:
    """Compute replayable lane metrics from append-only execution evidence.

    Metrics deliberately use node attempts, not mutable node rows, for past
    completions.  A duplicate event or coordinator restart cannot turn one
    attempt into multiple starts/settlements.  Current queue depth/inflight
    values come from the latest durable node state.
    """

    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    resolved_spark_workers = min(4, max_workers) if spark_workers is None else spark_workers
    if not 0 <= resolved_spark_workers <= max_workers:
        raise ValueError("spark_workers must be between 0 and max_workers")

    observed_at = _coerce_time(now) or datetime.now(UTC)
    window_start = observed_at - timedelta(seconds=window_seconds)
    specs = _node_specs(tasks)
    lanes = {
        lane: _empty_lane_metrics(lane, max_workers, resolved_spark_workers)
        for lane in EXECUTION_LANES
    }
    lane_pools = {lane: set() for lane in EXECUTION_LANES}
    for task in tasks:
        task_state = task.get("state")
        accepted_node_ids = {
            str(node.get("node_id"))
            for node in task.get("nodes", ())
            if node.get("state") == "accepted" and node.get("node_id") is not None
        }
        for node in task.get("nodes", ()):
            lane = execution_lane_for_spec(node)
            lane_pools[lane].add(quota_pool_id_for_spec(node))
            state = node.get("state")
            if state == "pending" and task_state in _CLAIMABLE_TASK_STATES:
                dependencies = {str(value) for value in node.get("depends_on", ())}
                if dependencies.issubset(accepted_node_ids):
                    lanes[lane]["queue_depth"] += 1
                else:
                    lanes[lane]["dependency_blocked"] += 1
            elif state == "running":
                lanes[lane]["inflight"] += 1
            elif state == "cancelled":
                # Older ledgers represent cancellation in current node state
                # rather than a per-attempt settlement event.  Keep that
                # separately observable instead of inventing an attempt.
                lanes[lane]["cancelled_current_nodes"] += 1

    starts: dict[tuple[str, str, int], dict[str, Any]] = {}
    settlements: dict[tuple[str, str, int], dict[str, Any]] = {}
    retries: dict[tuple[str, str, int], dict[str, Any]] = {}
    reworks: dict[tuple[str, str, int], dict[str, Any]] = {}
    lane_physical_call_keys: dict[str, dict[str, int]] = {
        lane: {} for lane in EXECUTION_LANES
    }
    ordered_events = sorted(events, key=lambda event: int(event.get("cursor", 0)))
    for event in ordered_events:
        event_type = event.get("event_type")
        task_id = event.get("task_id")
        node_id = event.get("node_id")
        payload = event.get("payload")
        if not isinstance(task_id, str) or not isinstance(node_id, str) or not isinstance(payload, dict):
            continue
        attempt = _attempt(
            payload,
            fallback_field="verifier_attempt" if event_type == "task.repair_scheduled" else None,
        )
        if attempt is None:
            continue
        key = (task_id, node_id, attempt)
        timestamp = _coerce_time(event.get("created_at"))
        if timestamp is None:
            continue
        spec = specs.get((task_id, node_id), {})
        lane = _event_lane(payload, spec)
        pool = _event_pool(payload, spec)
        lane_pools[lane].add(pool)
        record = {"at": timestamp, "lane": lane, "pool": pool}
        if event_type == "node.started":
            starts.setdefault(key, record)
        elif event_type in _TERMINAL_NODE_EVENTS:
            settlements.setdefault(
                key,
                {
                    **record,
                    "status": _TERMINAL_NODE_EVENTS[str(event_type)],
                    "attribution": _terminal_attribution(payload),
                },
            )
        elif event_type == "node.retry_scheduled":
            retries.setdefault(key, record)
        elif event_type == "task.repair_scheduled":
            reworks.setdefault(key, record)

    for key, record in starts.items():
        if record["at"] >= window_start:
            lanes[record["lane"]]["started"] += 1
        settled = settlements.get(key)
        if settled is None and record["at"] >= window_start:
            lanes[record["lane"]]["unfinished"] += 1
        end = settled["at"] if settled is not None else observed_at
        if end <= record["at"]:
            continue
        overlap_start = max(record["at"], window_start)
        overlap_end = min(end, observed_at)
        if overlap_end > overlap_start:
            lanes[record["lane"]]["busy_seconds"] += (overlap_end - overlap_start).total_seconds()

    for key, settled in settlements.items():
        if settled["at"] >= window_start:
            lane_metrics = lanes[settled["lane"]]
            lane_metrics[settled["status"]] += 1
            attribution = settled.get("attribution")
            if not isinstance(attribution, Mapping):
                attribution = _unknown_terminal_attribution()
            _record_terminal_attribution(
                lane_metrics,
                attribution,
                status=str(settled["status"]),
                physical_call_keys=lane_physical_call_keys[settled["lane"]],
            )
    for key, record in retries.items():
        if record["at"] >= window_start:
            lanes[record["lane"]]["retry"] += 1
    for key, record in reworks.items():
        if record["at"] >= window_start:
            lanes[record["lane"]]["rework"] += 1

    duration_hours = window_seconds / 3600
    for lane, metrics in lanes.items():
        capacity = int(metrics["capacity"])
        if capacity > 0:
            metrics["utilization"] = round(
                metrics["busy_seconds"] / (capacity * window_seconds), 6
            )
        else:
            metrics["utilization"] = None
        metrics["busy_seconds"] = round(metrics["busy_seconds"], 6)
        metrics["accepted_per_hour"] = round(metrics["accepted"] / duration_hours, 6)
        metrics["quota_pool_ids"] = sorted(lane_pools[lane])
        physical = metrics["attribution"]["physical_call_usage"]
        physical["unique_attested_physical_calls"] = len(lane_physical_call_keys[lane])
        physical["deduplicated_retry_or_fallback_attempts"] = sum(
            max(0, count - 1) for count in lane_physical_call_keys[lane].values()
        )

    global_busy_seconds = sum(float(metrics["busy_seconds"]) for metrics in lanes.values())
    global_physical_call_keys: dict[str, int] = {}
    for keys in lane_physical_call_keys.values():
        for key, count in keys.items():
            global_physical_call_keys[key] = global_physical_call_keys.get(key, 0) + count
    global_failure_origins = {
        origin: sum(int(metrics["failure_origins"][origin]) for metrics in lanes.values())
        for origin in _FAILURE_ORIGINS
    }
    global_attribution = _empty_attribution_metrics()
    for metrics in lanes.values():
        _merge_attribution_metrics(global_attribution, metrics["attribution"])
    global_attribution["physical_call_usage"]["unique_attested_physical_calls"] = len(
        global_physical_call_keys
    )
    global_attribution["physical_call_usage"]["deduplicated_retry_or_fallback_attempts"] = sum(
        max(0, count - 1) for count in global_physical_call_keys.values()
    )
    return {
        "status": "ok",
        "source": "append-only-events",
        "observed_at": observed_at.isoformat(),
        "window": {
            "start": window_start.isoformat(),
            "end": observed_at.isoformat(),
            "seconds": window_seconds,
        },
        "global": {
            "max_workers": max_workers,
            "busy_seconds": round(global_busy_seconds, 6),
            "utilization": round(global_busy_seconds / (max_workers * window_seconds), 6),
            "outcomes": {
                status: sum(int(metrics[status]) for metrics in lanes.values())
                for status in ("accepted", "failed", "blocked", "indeterminate", "cancelled", "unfinished")
            },
            "cancelled_current_nodes": sum(
                int(metrics["cancelled_current_nodes"]) for metrics in lanes.values()
            ),
            "failure_origins": global_failure_origins,
            "attribution": global_attribution,
        },
        "lanes": lanes,
        "quota_pools": {
            "codex-general": _unobservable_quota(
                "Codex general-pool subscription remaining balance is not observable"
            ),
            "codex-spark": _unobservable_quota(
                "GPT-5.3-Codex-Spark remaining balance is not observable"
            ),
            "codex-control": _unobservable_quota(
                "Codex control-pool subscription remaining balance is not observable"
            ),
        },
    }


def _empty_lane_metrics(lane: str, max_workers: int, spark_workers: int) -> dict[str, Any]:
    if lane == "spark":
        capacity = spark_workers
        capacity_kind = "dedicated"
    elif lane == "control":
        capacity = max_workers
        capacity_kind = "shared-global"
    else:
        capacity = max_workers
        capacity_kind = "shared-global"
    return {
        "capacity": capacity,
        "capacity_kind": capacity_kind,
        "quota_pool_ids": [],
        "queue_depth": 0,
        "dependency_blocked": 0,
        "inflight": 0,
        "started": 0,
        "accepted": 0,
        "failed": 0,
        "blocked": 0,
        "indeterminate": 0,
        "cancelled": 0,
        "unfinished": 0,
        "cancelled_current_nodes": 0,
        "retry": 0,
        "rework": 0,
        "busy_seconds": 0.0,
        "failure_origins": {origin: 0 for origin in _FAILURE_ORIGINS},
        "attribution": _empty_attribution_metrics(),
        # Active thread occupancy is not a subscription balance.
        "quota_remaining": None,
    }


def _unknown_terminal_attribution() -> dict[str, Any]:
    return {
        "present": False,
        "valid": False,
        "observed_model_status": "unknown",
        "failure_origin": "unknown",
        "physical_call_status": "unknown",
        "physical_call_usage_key": None,
    }


def _terminal_attribution(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only aggregate-safe attribution facts from a settled result."""

    raw_result = payload.get("result")
    result = raw_result if isinstance(raw_result, Mapping) else {}
    raw = result.get("execution_attribution")
    if raw is None:
        return _unknown_terminal_attribution()
    if not isinstance(raw, Mapping):
        return {**_unknown_terminal_attribution(), "present": True}
    try:
        attribution = ExecutionAttribution.from_dict(raw)
    except (TypeError, ValueError):
        return {**_unknown_terminal_attribution(), "present": True}
    return {
        "present": True,
        "valid": True,
        "observed_model_status": attribution.observed_model.status,
        "failure_origin": attribution.failure.origin,
        "physical_call_status": attribution.physical_call.status,
        "physical_call_usage_key": attribution.physical_call.usage_deduplication_key,
    }


def _empty_attribution_metrics() -> dict[str, Any]:
    return {
        "attributed_terminal_attempts": 0,
        "attested_observed_model_attempts": 0,
        "unattested_observed_model_attempts": 0,
        "unknown_observed_model_attempts": 0,
        "invalid_attribution_attempts": 0,
        "physical_call_usage": {
            "attested_call_attempts": 0,
            "unattested_call_attempts": 0,
            "unknown_call_attempts": 0,
            "unique_attested_physical_calls": 0,
            "deduplicated_retry_or_fallback_attempts": 0,
            # These are deliberately separate unavailable measurements.  A
            # physical call ID can deduplicate execution evidence but cannot
            # turn subscription quota or relative routing cost into dollars.
            "api_dollars": {"status": "unknown", "amount": None, "unit": "USD"},
            "subscription_tokens": {"status": "unknown", "amount": None, "unit": "tokens"},
            "quota_estimate": {
                "status": "unknown",
                "amount": None,
                "unit": "provider-specific quota",
            },
        },
    }


def _record_terminal_attribution(
    metrics: dict[str, Any],
    attribution: Mapping[str, Any],
    *,
    status: str,
    physical_call_keys: dict[str, int],
) -> None:
    """Keep infra failure and physical-call evidence out of model routing data."""

    if attribution.get("present"):
        metrics["attribution"]["attributed_terminal_attempts"] += 1
        if not attribution.get("valid"):
            metrics["attribution"]["invalid_attribution_attempts"] += 1
        else:
            identity_status = attribution.get("observed_model_status")
            if identity_status == "attested":
                metrics["attribution"]["attested_observed_model_attempts"] += 1
            elif identity_status == "unattested":
                metrics["attribution"]["unattested_observed_model_attempts"] += 1
            else:
                metrics["attribution"]["unknown_observed_model_attempts"] += 1
    if status != "accepted":
        origin = attribution.get("failure_origin")
        metrics["failure_origins"][origin if origin in _FAILURE_ORIGINS else "unknown"] += 1
    physical = metrics["attribution"]["physical_call_usage"]
    physical_status = attribution.get("physical_call_status")
    if physical_status not in {"attested", "unattested", "unknown"}:
        physical_status = "unknown"
    physical[f"{physical_status}_call_attempts"] += 1
    key = attribution.get("physical_call_usage_key")
    if physical_status == "attested" and isinstance(key, str) and key:
        physical_call_keys[key] = physical_call_keys.get(key, 0) + 1


def _merge_attribution_metrics(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for field in (
        "attributed_terminal_attempts",
        "attested_observed_model_attempts",
        "unattested_observed_model_attempts",
        "unknown_observed_model_attempts",
        "invalid_attribution_attempts",
    ):
        target[field] += int(source.get(field, 0))
    source_physical = source.get("physical_call_usage")
    if not isinstance(source_physical, Mapping):
        return
    target_physical = target["physical_call_usage"]
    for field in ("attested_call_attempts", "unattested_call_attempts", "unknown_call_attempts"):
        target_physical[field] += int(source_physical.get(field, 0))


def _node_specs(tasks: list[dict[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    specs: dict[tuple[str, str], Mapping[str, Any]] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str):
            continue
        for node in task.get("nodes", ()):
            node_id = node.get("node_id")
            if isinstance(node_id, str):
                specs[(task_id, node_id)] = node
    return specs


def _event_lane(payload: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    if spec:
        return execution_lane_for_spec(spec)
    lane = payload.get("execution_lane")
    if lane in EXECUTION_LANES:
        return str(lane)
    return execution_lane_for_spec(spec)


def _event_pool(payload: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    if spec:
        return quota_pool_id_for_spec(spec)
    pool = payload.get("quota_pool_id")
    if isinstance(pool, str) and pool.strip():
        return pool.strip()
    return quota_pool_id_for_spec(spec)


def _attempt(payload: Mapping[str, Any], *, fallback_field: str | None = None) -> int | None:
    value = payload.get("attempt")
    if value is None and fallback_field is not None:
        value = payload.get(fallback_field)
    if isinstance(value, bool):
        return None
    try:
        attempt = int(value)
    except (TypeError, ValueError):
        return None
    return attempt if attempt > 0 else None


def _coerce_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _unobservable_quota(reason: str) -> dict[str, Any]:
    return {"status": "N/A", "remaining": None, "reason": reason}
