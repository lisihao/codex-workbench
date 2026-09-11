"""Compact, read-only current-task observations for bounded list pages.

The projection deliberately reads only scalar task and node state plus the
metadata columns of the latest material event.  It does not deserialize task
contracts, node results, steering instructions, event payloads, or artifact
bodies.
"""

from __future__ import annotations

from collections.abc import Sequence
import sqlite3
from typing import Any

from .model import now_iso


_MAX_PAGE_SIZE = 100
_MAX_STATE_CHARS = 24
_MAX_NODE_ID_CHARS = 64
_MAX_EVENT_TYPE_CHARS = 64
_MAX_TIMESTAMP_CHARS = 40

_MATERIAL_EVENT_TYPES = (
    "node.started",
    "node.accepted",
    "node.failed",
    "node.blocked",
    "node.indeterminate",
    "worktree.allocated",
    "node.blocked_worktree_recovery_rolled_back",
    "task.acceptance_amended",
    "task.state_changed",
    "delivery_objective.stage_succeeded",
)
_MATERIAL_EVENT_TYPES_SQL = ", ".join(
    f"'{event_type}'" for event_type in _MATERIAL_EVENT_TYPES
)

# Keep this literal predicate in the read query below.  SQLite can then match
# the partial-index predicate instead of falling back to the broad event index.
MATERIAL_EVENT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS events_task_material_cursor_idx "
    "ON events(task_id, cursor) "
    f"WHERE event_type IN ({_MATERIAL_EVENT_TYPES_SQL})"
)


def current_task_observations(
    connection: sqlite3.Connection,
    task_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Return bounded current status metadata for one SQLite read page.

    ``connection`` is owned by the caller; this function neither opens a
    transaction nor commits it.  The caller must keep the page at most 100
    task IDs so that the projection has a fixed query and response bound.
    Missing IDs are represented explicitly as ``observation_unavailable`` and
    are never filled from a historical task or event row.
    """

    requested = tuple(task_ids)
    if len(requested) > _MAX_PAGE_SIZE:
        raise ValueError(f"task_ids must contain at most {_MAX_PAGE_SIZE} IDs")
    if any(type(task_id) is not str or not task_id for task_id in requested):
        raise ValueError("task_ids must contain non-empty strings")

    observed_at = _bounded_text(now_iso(), _MAX_TIMESTAMP_CHARS, "unknown")
    if not requested:
        return {}

    # Preserve caller order for deterministic missing-task entries while
    # avoiding duplicate SQL work for a repeated task ID.
    unique_task_ids = tuple(dict.fromkeys(requested))
    placeholders = ", ".join("?" for _ in unique_task_ids)
    task_rows = connection.execute(
        f"""
        SELECT task_id, state, state_revision
        FROM tasks
        WHERE task_id IN ({placeholders})
        """,
        unique_task_ids,
    ).fetchall()
    task_by_id = {str(row["task_id"]): row for row in task_rows}

    node_rows = connection.execute(
        f"""
        SELECT task_id,
               MAX(CASE
                   WHEN state = 'running' AND recovery_json IS NOT NULL THEN 1
                   ELSE 0
               END) AS recovery_preparation,
               MAX(CASE
                   WHEN state = 'running'
                    AND recovery_json IS NULL
                    AND json_extract(spec_json, '$.verifier') = 1
                   THEN 1 ELSE 0
               END) AS verification,
               MAX(CASE
                   WHEN state = 'running'
                    AND recovery_json IS NULL
                    AND COALESCE(json_extract(spec_json, '$.verifier'), 0) != 1
                   THEN 1 ELSE 0
               END) AS execution
        FROM nodes
        WHERE task_id IN ({placeholders})
        GROUP BY task_id
        """,
        unique_task_ids,
    ).fetchall()
    active_phases: dict[str, set[str]] = {task_id: set() for task_id in unique_task_ids}
    for row in node_rows:
        task_id = str(row["task_id"])
        if row["recovery_preparation"]:
            active_phases[task_id].add("recovery_preparation")
        if row["verification"]:
            active_phases[task_id].add("verification")
        if row["execution"]:
            active_phases[task_id].add("execution")

    events_by_task: dict[str, dict[str, Any] | None] = {}
    event_query = f"""
        SELECT cursor, event_type, node_id, created_at
        FROM events
        WHERE task_id = ?
          AND event_type IN ({_MATERIAL_EVENT_TYPES_SQL})
        ORDER BY cursor DESC
        LIMIT 1
    """
    for task_id in unique_task_ids:
        row = connection.execute(event_query, (task_id,)).fetchone()
        events_by_task[task_id] = (
            {
                "cursor": int(row["cursor"]),
                "event_type": _bounded_text(
                    row["event_type"], _MAX_EVENT_TYPE_CHARS, "unknown"
                ),
                "node_id": _bounded_optional_text(row["node_id"], _MAX_NODE_ID_CHARS),
                "created_at": _bounded_text(
                    row["created_at"], _MAX_TIMESTAMP_CHARS, "unknown"
                ),
            }
            if row is not None
            else None
        )

    observations: dict[str, dict[str, Any]] = {}
    for task_id in requested:
        row = task_by_id.get(task_id)
        if row is None:
            observations[task_id] = {
                "state": "observation_unavailable",
                "reason": "task_not_found",
                "active_phases": [],
                "last_material_event": None,
                "observed_at": observed_at,
            }
            continue
        observations[task_id] = {
            "state": _bounded_text(row["state"], _MAX_STATE_CHARS, "unknown"),
            "revision": int(row["state_revision"]),
            "active_phases": [
                phase
                for phase in ("recovery_preparation", "verification", "execution")
                if phase in active_phases[task_id]
            ],
            "last_material_event": events_by_task[task_id],
            "observed_at": observed_at,
        }
    return observations


def _bounded_text(value: object, maximum: int, fallback: str) -> str:
    """Return a bounded text field without exposing non-text sentinels."""

    if not isinstance(value, str) or not value:
        return fallback
    if len(value) <= maximum:
        return value
    return f"{value[: maximum - 1]}…"


def _bounded_optional_text(value: object, maximum: int) -> str | None:
    """Return a bounded nullable text field."""

    if value is None:
        return None
    return _bounded_text(value, maximum, "unknown")


__all__ = ["MATERIAL_EVENT_INDEX_SQL", "current_task_observations", "now_iso"]
