"""Durable task-event notifications routed to originating sessions.

This module stores an Authority-owned outbox and acknowledgement state.  It
does not open a chat, wake a model, or claim that a host displayed a
notification.  Store integration executes :data:`SCHEMA_SQL` in its existing
schema transaction and calls the functions below from its existing
transactions/control turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import sqlite3
from typing import Any

from .model import now_iso


_MAX_PROJECT_LIMIT = 200
_MAX_LIST_LIMIT = 50
_MAX_METADATA_CHARS = 128
_MAX_TIMESTAMP_CHARS = 40
_UNROUTED_SOURCE_THREAD = None

NOTIFICATION_EVENT_TYPES = (
    "responsibility.handoff_proposed",
    "responsibility.handoff_claimed",
    "responsibility.deferred",
    "responsibility.fulfilled",
    "responsibility.cancelled",
    "node.blocked",
    "node.failed",
    "node.blocked_worktree_recovery_rolled_back",
    "node_recovery.needs_action",
    "node.blocked_source_repair_queued",
    "delivery_objective.decision_required",
    "node_recovery.action_reconciled",
    "node_recovery.action_settled",
    "node.accepted",
)
_ROUTE_EVENT_TYPE = "context.task_bound"
_NOTIFICATION_EVENT_TYPES_SQL = ", ".join(
    f"'{event_type}'" for event_type in NOTIFICATION_EVENT_TYPES
)

# Store executes this text with its existing schema transaction.  Keep each
# statement semicolon-delimited and avoid semicolons inside comments or string
# literals because the repository's schema runner splits on semicolons.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_notification_projection (
    projection_id INTEGER PRIMARY KEY CHECK(projection_id = 1),
    event_cursor INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO session_notification_projection(projection_id, event_cursor)
VALUES(1, 0);
CREATE TABLE IF NOT EXISTS session_notification_routes (
    source_thread_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_thread_id, task_id)
);
CREATE INDEX IF NOT EXISTS session_notification_routes_task_idx
ON session_notification_routes(task_id, source_thread_id);
CREATE TABLE IF NOT EXISTS session_notifications (
    notification_id TEXT PRIMARY KEY,
    source_thread_id TEXT,
    task_id TEXT NOT NULL,
    node_id TEXT,
    event_cursor INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason_kind TEXT,
    episode_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending', 'acknowledged')),
    acknowledged_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS session_notifications_task_event_thread_idx
ON session_notifications(task_id, event_cursor, COALESCE(source_thread_id, ''));
CREATE INDEX IF NOT EXISTS session_notifications_thread_state_cursor_idx
ON session_notifications(source_thread_id, state, event_cursor, notification_id);
CREATE INDEX IF NOT EXISTS session_notifications_task_cursor_idx
ON session_notifications(task_id, event_cursor, notification_id);
""".strip()


class SessionNotificationError(ValueError):
    """Base error for invalid notification inputs or durable ownership."""


def register_task_session(
    connection: sqlite3.Connection,
    task_id: str,
    source_thread_id: str,
    *,
    backfill_limit: int = _MAX_PROJECT_LIMIT,
) -> dict[str, Any]:
    """Persist one permanent task-to-session route and attach old outbox rows.

    The caller supplies an already validated session binding and owns the
    transaction.  Existing routes are never removed when a session's active
    task changes.  It attaches at most ``backfill_limit`` already projected rows per call;
    an unrouted row is promoted in place when possible, otherwise an
    idempotent route-specific copy is added.  Later control turns continue
    the bounded backfill.
    """

    task_id = _identifier(task_id, "task_id")
    source_thread_id = _identifier(source_thread_id, "source_thread_id")
    _bounded_backfill_limit(backfill_limit)
    timestamp = now_iso()
    connection.execute(
        """
        INSERT OR IGNORE INTO session_notification_routes(
            source_thread_id, task_id, created_at
        ) VALUES(?, ?, ?)
        """,
        (source_thread_id, task_id, timestamp),
    )
    backfilled = _backfill_route_notifications(
        connection,
        task_id=task_id,
        source_thread_id=source_thread_id,
        limit=backfill_limit,
    )
    return {
        "source_thread_id": source_thread_id,
        "task_id": task_id,
        "route_registered": True,
        "backfilled": backfilled,
    }


def _backfill_route_notifications(
    connection: sqlite3.Connection,
    *,
    task_id: str | None = None,
    source_thread_id: str | None = None,
    limit: int,
) -> int:
    """Attach at most ``limit`` projected events to missing task routes."""

    clauses = [
        "NOT EXISTS ("
        "SELECT 1 FROM session_notifications AS target "
        "WHERE target.task_id = n.task_id "
        "AND target.event_cursor = n.event_cursor "
        "AND target.source_thread_id = r.source_thread_id"
        ")",
    ]
    parameters: list[object] = []
    if task_id is not None:
        clauses.append("r.task_id = ?")
        parameters.append(task_id)
    if source_thread_id is not None:
        clauses.append("r.source_thread_id = ?")
        parameters.append(source_thread_id)
    parameters.append(limit)
    rows = connection.execute(
        f"""
        SELECT r.source_thread_id AS target_source_thread_id,
               n.notification_id, n.source_thread_id, n.task_id, n.node_id,
               n.event_cursor, n.event_type, n.created_at, n.reason_kind,
               n.episode_id
        FROM session_notification_routes AS r
        JOIN session_notifications AS n ON n.task_id = r.task_id
        WHERE {' AND '.join(clauses)}
        ORDER BY n.event_cursor, r.source_thread_id
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    attached = 0
    for row in rows:
        target_source_thread_id = str(row["target_source_thread_id"])
        new_notification_id = _notification_id(
            str(row["task_id"]), int(row["event_cursor"]), target_source_thread_id
        )
        if row["source_thread_id"] is None:
            changed = connection.execute(
                """
                UPDATE session_notifications
                SET notification_id = ?, source_thread_id = ?
                WHERE notification_id = ? AND source_thread_id IS NULL
                """,
                (new_notification_id, target_source_thread_id, row["notification_id"]),
            ).rowcount
            if changed:
                attached += int(changed)
                continue
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO session_notifications(
                notification_id, source_thread_id, task_id, node_id,
                event_cursor, event_type, created_at, reason_kind, episode_id,
                state, acknowledged_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
            """,
            (
                new_notification_id,
                target_source_thread_id,
                row["task_id"],
                row["node_id"],
                int(row["event_cursor"]),
                row["event_type"],
                row["created_at"],
                row["reason_kind"],
                row["episode_id"],
            ),
        ).rowcount
        attached += int(inserted or 0)
    return attached


def _bounded_backfill_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_PROJECT_LIMIT
    ):
        raise ValueError(f"backfill_limit must be between 0 and {_MAX_PROJECT_LIMIT}")
    return value


def project_notifications(store: Any, limit: int = _MAX_PROJECT_LIMIT) -> dict[str, int]:
    """Project at most ``limit`` new durable events into the session outbox.

    The global cursor advances over existing events in cursor order.  Only
    the fixed notification event set produces outbox rows; ``context.task_bound``
    is consumed solely to recover routes created before this projection was
    installed.  Event payloads are reduced in SQLite to the fixed route and
    metadata scalars and are never persisted in the notification tables.
    """

    _bounded_limit(limit, _MAX_PROJECT_LIMIT, "limit")
    with store.transaction() as connection:
        projection = connection.execute(
            """
            SELECT event_cursor
            FROM session_notification_projection
            WHERE projection_id = 1
            """
        ).fetchone()
        if projection is None:
            connection.execute(
                """
                INSERT INTO session_notification_projection(projection_id, event_cursor)
                VALUES(1, 0)
                """
            )
            event_cursor = 0
        else:
            event_cursor = int(projection["event_cursor"])

        rows = connection.execute(
            f"""
            SELECT cursor, event_type, task_id, node_id, created_at,
                   CASE
                       WHEN event_type = '{_ROUTE_EVENT_TYPE}' AND json_valid(payload_json)
                       THEN json_extract(payload_json, '$.source_thread_id')
                   END AS bound_source_thread_id,
                   CASE
                       WHEN event_type IN ({_NOTIFICATION_EVENT_TYPES_SQL})
                            AND json_valid(payload_json)
                       THEN json_extract(payload_json, '$.reason_kind')
                   END AS reason_kind,
                   CASE
                       WHEN event_type IN ({_NOTIFICATION_EVENT_TYPES_SQL})
                            AND json_valid(payload_json)
                       THEN json_extract(payload_json, '$.episode_id')
                   END AS episode_id
            FROM events
            WHERE cursor > ?
            ORDER BY cursor
            LIMIT ?
            """,
            (event_cursor, limit),
        ).fetchall()
        created = 0
        for row in rows:
            current_cursor = int(row["cursor"])
            event_cursor = current_cursor
            event_type = str(row["event_type"])
            task_id = row["task_id"]
            if event_type == _ROUTE_EVENT_TYPE:
                if isinstance(task_id, str) and isinstance(row["bound_source_thread_id"], str):
                    bound_source_thread_id = row["bound_source_thread_id"]
                    if _valid_identifier(bound_source_thread_id):
                        route_result = register_task_session(
                            connection,
                            task_id,
                            bound_source_thread_id,
                            backfill_limit=0,
                        )
                        created += int(route_result["backfilled"])
                continue
            if (
                event_type not in NOTIFICATION_EVENT_TYPES
                or not isinstance(task_id, str)
                or not task_id
            ):
                continue
            reason_kind = _bounded_optional_text(row["reason_kind"])
            episode_id = _bounded_optional_text(row["episode_id"])
            routes = connection.execute(
                """
                SELECT source_thread_id
                FROM session_notification_routes
                WHERE task_id = ?
                ORDER BY source_thread_id
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
            source_thread_ids: tuple[str | None, ...] = (
                tuple(str(route["source_thread_id"]) for route in routes)
                if routes
                else (_UNROUTED_SOURCE_THREAD,)
            )
            for source_thread_id in source_thread_ids:
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO session_notifications(
                        notification_id, source_thread_id, task_id, node_id,
                        event_cursor, event_type, created_at, reason_kind,
                        episode_id, state, acknowledged_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
                    """,
                    (
                        _notification_id(task_id, current_cursor, source_thread_id),
                        source_thread_id,
                        task_id,
                        row["node_id"],
                        current_cursor,
                        event_type,
                        _bounded_text(row["created_at"], _MAX_TIMESTAMP_CHARS, "unknown"),
                        reason_kind,
                        episode_id,
                    ),
                )
                created += int(inserted.rowcount or 0)
        created += _backfill_route_notifications(connection, limit=_MAX_PROJECT_LIMIT)
        if rows:
            connection.execute(
                """
                UPDATE session_notification_projection
                SET event_cursor = ?
                WHERE projection_id = 1
                """,
                (event_cursor,),
            )
        return {
            "cursor": event_cursor,
            "scanned": len(rows),
            "notifications_created": created,
        }


def list_session_notifications(
    store: Any,
    source_thread_id: str,
    limit: int = _MAX_LIST_LIMIT,
    after_cursor: int = 0,
) -> dict[str, Any]:
    """List pending metadata for exactly one routed session without writing."""

    source_thread_id = _identifier(source_thread_id, "source_thread_id")
    _bounded_limit(limit, _MAX_LIST_LIMIT, "limit")
    if isinstance(after_cursor, bool) or not isinstance(after_cursor, int) or after_cursor < 0:
        raise ValueError("after_cursor must be a non-negative integer")
    with store.connection() as connection:
        rows = connection.execute(
            """
            SELECT notification_id, source_thread_id, task_id, node_id,
                   event_cursor, event_type, created_at, reason_kind,
                   episode_id, state, acknowledged_at
            FROM session_notifications
            WHERE source_thread_id = ? AND state = 'pending' AND event_cursor > ?
            ORDER BY event_cursor, notification_id
            LIMIT ?
            """,
            (source_thread_id, after_cursor, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    selected = rows[:limit]
    return {
        "notifications": [_notification_document(row) for row in selected],
        "next_cursor": int(selected[-1]["event_cursor"]) if has_more and selected else None,
    }


def ack_session_notification(
    store: Any,
    source_thread_id: str,
    notification_id: str,
) -> dict[str, Any]:
    """Persist an idempotent acknowledgement owned by one exact session.

    The receipt records only Authority acknowledgement state.  It does not
    assert host display, chat delivery, model wakeup, or any external effect.
    """

    source_thread_id = _identifier(source_thread_id, "source_thread_id")
    notification_id = _identifier(notification_id, "notification_id")
    with store.transaction() as connection:
        row = connection.execute(
            """
            SELECT n.notification_id, n.source_thread_id, n.task_id, n.node_id,
                   n.event_cursor, n.event_type, n.created_at, n.reason_kind,
                   n.episode_id, n.state, n.acknowledged_at
            FROM session_notifications AS n
            JOIN session_notification_routes AS r
              ON r.source_thread_id = n.source_thread_id
             AND r.task_id = n.task_id
            WHERE n.notification_id = ?
            """,
            (notification_id,),
        ).fetchone()
        if row is None:
            exists = connection.execute(
                "SELECT source_thread_id FROM session_notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if exists is not None:
                raise PermissionError("notification belongs to a different session")
            raise KeyError(notification_id)
        if row["source_thread_id"] != source_thread_id:
            raise PermissionError("notification belongs to a different session")
        if row["state"] == "pending":
            acknowledged_at = now_iso()
            connection.execute(
                """
                UPDATE session_notifications
                SET state = 'acknowledged', acknowledged_at = ?
                WHERE notification_id = ? AND source_thread_id = ? AND state = 'pending'
                """,
                (acknowledged_at, notification_id, source_thread_id),
            )
            row = connection.execute(
                """
                SELECT notification_id, source_thread_id, task_id, node_id,
                       event_cursor, event_type, created_at, reason_kind,
                       episode_id, state, acknowledged_at
                FROM session_notifications
                WHERE notification_id = ?
                """,
                (notification_id,),
            ).fetchone()
            assert row is not None
        return _notification_document(row)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _valid_identifier(value):
        raise ValueError(f"{label} must be a non-empty string without whitespace")
    return value


def _valid_identifier(value: str) -> bool:
    return bool(value) and not any(character.isspace() for character in value)


def _bounded_limit(value: object, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _bounded_text(value: object, maximum: int, fallback: str) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    if len(value) <= maximum:
        return value
    return f"{value[: maximum - 1]}…"


def _bounded_optional_text(value: object) -> str | None:
    if value is None or not isinstance(value, str) or not value:
        return None
    return _bounded_text(value, _MAX_METADATA_CHARS, "unknown")


def _notification_id(task_id: str, event_cursor: int, source_thread_id: str | None) -> str:
    material = f"{source_thread_id or ''}\x00{task_id}\x00{event_cursor}"
    return "session-notification-" + sha256(material.encode("utf-8")).hexdigest()[:32]


def _notification_document(row: Mapping[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {
        "notification_id": str(row["notification_id"]),
        "source_thread_id": str(row["source_thread_id"]),
        "task_id": _bounded_text(row["task_id"], _MAX_METADATA_CHARS, "unknown"),
        "node_id": _bounded_optional_text(row["node_id"]),
        "event_cursor": int(row["event_cursor"]),
        "event_type": _bounded_text(row["event_type"], _MAX_METADATA_CHARS, "unknown"),
        "created_at": _bounded_text(row["created_at"], _MAX_TIMESTAMP_CHARS, "unknown"),
        "state": str(row["state"]),
        "acknowledged_at": row["acknowledged_at"],
    }
    if row["reason_kind"] is not None:
        document["reason_kind"] = _bounded_text(row["reason_kind"], _MAX_METADATA_CHARS, "unknown")
    if row["episode_id"] is not None:
        document["episode_id"] = _bounded_text(row["episode_id"], _MAX_METADATA_CHARS, "unknown")
    return document


__all__ = [
    "NOTIFICATION_EVENT_TYPES",
    "SCHEMA_SQL",
    "SessionNotificationError",
    "ack_session_notification",
    "list_session_notifications",
    "now_iso",
    "project_notifications",
    "register_task_session",
]
