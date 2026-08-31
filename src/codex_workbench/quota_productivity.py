from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .store import WorkbenchStore


_NON_RUNTIME_SOURCE_MARKERS = ("fixture", "test", "controlled", "simulation")
_CLAUDE_MODEL_MARKERS = ("claude", "opus", "sonnet", "fable")


def build_quota_productivity(store: WorkbenchStore) -> dict[str, Any]:
    """Project accepted Claude output per measured subscription usage window."""

    return compute_quota_productivity(
        store.list_quota_snapshots(limit=5_000),
        store.list_tasks(limit=5_000),
        store.read_events(limit=50_000),
    )


def compute_quota_productivity(
    snapshots: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime = [snapshot for snapshot in snapshots if _is_runtime_source(snapshot.get("source"))]
    accepted_at = {
        str(event["task_id"]): _parse_time(event["created_at"])
        for event in events
        if event.get("task_id")
        and event.get("event_type") == "task.state_changed"
        and event.get("payload", {}).get("to") == "accepted"
    }
    accepted_tasks = [
        task
        for task in tasks
        if task.get("state") == "accepted"
        and str(task.get("task_id")) in accepted_at
        and _used_claude(task)
    ]
    windows = [
        *_window_metrics(
            runtime,
            accepted_tasks,
            accepted_at,
            window_field="five_hour_window_id",
            remaining_field="five_hour_remaining",
            kind="five-hour",
        ),
        *_window_metrics(
            runtime,
            accepted_tasks,
            accepted_at,
            window_field="weekly_window_id",
            remaining_field="weekly_all_remaining",
            kind="weekly-all",
        ),
    ]
    measured = [window for window in windows if window["status"] == "ok"]
    return {
        "status": "ok" if measured else "insufficient-evidence",
        "accepted_claude_tasks": len(accepted_tasks),
        "measured_windows": len(measured),
        "windows": windows,
    }


def _window_metrics(
    snapshots: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    accepted_at: dict[str, datetime],
    *,
    window_field: str,
    remaining_field: str,
    kind: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for snapshot in snapshots:
        window_id = snapshot.get(window_field)
        remaining = snapshot.get(remaining_field)
        observed = _parse_time(snapshot.get("observed_at"))
        if window_id and remaining is not None and observed is not None:
            grouped.setdefault(str(window_id), []).append((observed, float(remaining)))

    results: list[dict[str, Any]] = []
    for window_id, samples in sorted(grouped.items()):
        samples.sort(key=lambda sample: sample[0])
        first_at, first_remaining = samples[0]
        last_at, last_remaining = samples[-1]
        if len(samples) < 2:
            results.append(
                {
                    "kind": kind,
                    "window_id": window_id,
                    "status": "insufficient-evidence",
                    "sample_count": len(samples),
                }
            )
            continue
        if last_remaining > first_remaining:
            results.append(
                {
                    "kind": kind,
                    "window_id": window_id,
                    "status": "invalid-window",
                    "sample_count": len(samples),
                    "reason": "remaining quota increased inside one named window",
                }
            )
            continue
        consumed = first_remaining - last_remaining
        points = sum(
            _task_points(task)
            for task in tasks
            if first_at <= accepted_at[str(task["task_id"])] <= last_at
        )
        results.append(
            {
                "kind": kind,
                "window_id": window_id,
                "status": "ok",
                "sample_count": len(samples),
                "first_observed_at": first_at.isoformat(),
                "last_observed_at": last_at.isoformat(),
                "remaining_start": first_remaining,
                "remaining_end": last_remaining,
                "consumed_percent": consumed,
                "accepted_points": points,
                "accepted_points_per_10_percent": (
                    round(points * 10 / consumed, 4) if consumed > 0 else None
                ),
            }
        )
    return results


def _used_claude(task: dict[str, Any]) -> bool:
    for node in task.get("nodes", ()):
        result = node.get("result") or {}
        model = str(result.get("actual_model") or node.get("effective_model") or "").lower()
        if any(marker in model for marker in _CLAUDE_MODEL_MARKERS):
            return True
    return False


def _task_points(task: dict[str, Any]) -> float:
    value = task.get("contract", {}).get("task_points", 1.0)
    try:
        points = float(value)
    except (TypeError, ValueError):
        return 1.0
    return points if points > 0 else 1.0


def _is_runtime_source(source: object) -> bool:
    text = str(source or "").strip().lower()
    return bool(text) and not any(marker in text for marker in _NON_RUNTIME_SOURCE_MARKERS)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
