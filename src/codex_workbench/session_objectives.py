"""Read-only unfinished task and delivery summaries for one Codex session.

The notification route ledger is the durable record of every task associated
with a session.  The current active binding adds a task only before its route
event has been projected.  Routed tasks keep their permanent SQLite row order
when the active pointer changes.  This module only reads those existing
records; it never binds tasks, backfills notifications, or schedules work.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import WorkbenchStore


DEFAULT_SESSION_OBJECTIVE_LIMIT = 20
MAX_SESSION_OBJECTIVE_LIMIT = 50
MAX_SESSION_OBJECTIVE_RESPONSE_BYTES = 64 * 1024
_MAX_SQLITE_ROW_REFERENCE = 9_223_372_036_854_775_807


def _objective_page_reference(value: object) -> int:
    """Parse the opaque page cursor without treating it as a task ID."""

    if value is None:
        return -1
    if value == "active":
        return 0
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise ValueError("objective_cursor must be 'active' or a decimal string")
    if value.startswith("0") or len(value) > 19:
        raise ValueError("objective_cursor is not a valid route reference")
    reference = int(value)
    if not 1 <= reference <= _MAX_SQLITE_ROW_REFERENCE:
        raise ValueError("objective_cursor is outside the SQLite row reference range")
    return reference


def _objective_limit(value: object) -> int:
    """Validate the bounded page size before issuing the ledger query."""

    if value is None:
        return DEFAULT_SESSION_OBJECTIVE_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_SESSION_OBJECTIVE_LIMIT:
        raise ValueError(
            "objective_limit must be between 1 and "
            f"{MAX_SESSION_OBJECTIVE_LIMIT}"
        )
    return value


def _next_cursor(reference: int) -> str:
    return "active" if reference == 0 else str(reference)


def list_session_objectives(
    store: WorkbenchStore,
    source_thread_id: str,
    active_task_id: str | None,
    *,
    limit: object = None,
    cursor: object = None,
) -> dict[str, Any]:
    """Return a bounded page of unfinished objectives for one session.

    A task remains visible while its task state is not settled, or while a
    linked delivery objective is not settled.  The latter deliberately keeps
    an accepted implementation task visible until its delivery reaches
    ``complete`` or ``cancelled``.

    Args:
        store: Authority-owned SQLite ledger.
        source_thread_id: Originating Codex thread whose permanent routes are read.
        active_task_id: Current binding already read with the session receipt.
        limit: Optional maximum number of summaries in this page.
        cursor: Optional opaque pointer returned by a preceding page.

    Returns:
        Only task identity/state and delivery identity/stage/state, plus a
        cursor when another bounded page is available.
    """

    if (
        not isinstance(source_thread_id, str)
        or not source_thread_id
    ):
        raise ValueError("source_thread_id must be a non-empty string")
    if active_task_id is not None and (
        not isinstance(active_task_id, str)
        or not active_task_id
    ):
        raise ValueError("active_task_id must be a non-empty string or null")

    page_limit = _objective_limit(limit)
    page_reference = _objective_page_reference(cursor)
    with store.connection() as connection:
        rows = connection.execute(
            """
            WITH associated AS (
                SELECT route.rowid AS objective_ref, route.task_id,
                       CASE WHEN route.task_id = ? THEN 1 ELSE 0 END AS active
                FROM session_notification_routes AS route
                WHERE route.source_thread_id = ?
                UNION ALL
                SELECT 0 AS objective_ref, ? AS task_id, 1 AS active
                WHERE ? IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM session_notification_routes AS route
                      WHERE route.source_thread_id = ? AND route.task_id = ?
                  )
            )
            SELECT associated.objective_ref, associated.active,
                   task.task_id, task.state AS task_state,
                   task.state_revision AS task_revision,
                   delivery.objective_id AS delivery_objective_id,
                   delivery.stage AS delivery_stage,
                   delivery.state AS delivery_state
            FROM associated
            JOIN tasks AS task ON task.task_id = associated.task_id
            LEFT JOIN delivery_objectives AS delivery ON delivery.task_id = task.task_id
            WHERE associated.objective_ref > ?
              AND (
                  task.state NOT IN ('accepted', 'cancelled')
                  OR (
                      delivery.objective_id IS NOT NULL
                      AND delivery.state NOT IN ('complete', 'cancelled')
                  )
              )
            ORDER BY associated.objective_ref ASC
            LIMIT ?
            """,
            (
                active_task_id,
                source_thread_id,
                active_task_id,
                active_task_id,
                source_thread_id,
                active_task_id,
                page_reference,
                page_limit + 1,
            ),
        ).fetchall()

    summaries: list[tuple[int, dict[str, Any]]] = []
    for row in rows[:page_limit]:
        delivery = (
            {
                "objective_id": str(row["delivery_objective_id"]),
                "stage": str(row["delivery_stage"]),
                "state": str(row["delivery_state"]),
            }
            if row["delivery_objective_id"] is not None
            else None
        )
        summaries.append(
            (
                int(row["objective_ref"]),
                {
                    "task_id": str(row["task_id"]),
                    "state": str(row["task_state"]),
                    "state_revision": int(row["task_revision"]),
                    "active": bool(row["active"]),
                    "delivery_objective": delivery,
                },
            )
        )

    while summaries:
        has_more = len(rows) > len(summaries)
        payload = {
            "objectives": [summary for _, summary in summaries],
            "next_objective_cursor": (
                _next_cursor(summaries[-1][0]) if has_more else None
            ),
        }
        if (
            len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            <= MAX_SESSION_OBJECTIVE_RESPONSE_BYTES
        ):
            return payload
        summaries.pop()

    if rows:
        raise ValueError("session objective summary cannot fit within the response limit")
    return {"objectives": [], "next_objective_cursor": None}
