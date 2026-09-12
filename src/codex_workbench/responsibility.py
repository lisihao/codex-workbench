"""Durable, non-executing responsibility records for one task goal.

The ledger deliberately stores accountability only.  It appends complete
snapshots to the existing Authority event journal and never claims a node,
changes task state, grants an access route, or starts execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Mapping

from .model import canonical_hash, now_iso
from .store import CommandConflictError, StateConflictError, WorkbenchStore


_EVENT_GLOB = "responsibility.*"
_SCHEMA_VERSION = 1
_OPERATION_STATES = {
    "open": frozenset({"open"}),
    "propose_handoff": frozenset({"handoff_proposed"}),
    "claim_handoff": frozenset({"claimed"}),
    "defer": frozenset({"deferred"}),
    "reconcile_terminal": frozenset({"fulfilled", "cancelled"}),
}
_OPERATIONS = frozenset(_OPERATION_STATES)
_STATES = frozenset().union(*_OPERATION_STATES.values())
_TERMINAL_STATES = frozenset({"fulfilled", "cancelled"})
_NONTERMINAL_STATES = _STATES - _TERMINAL_STATES
_MAX_RECONCILE_LIMIT = 100
_MAX_LIST_LIMIT = 100
RESPONSIBILITY_WAIT_KINDS = frozenset(
    {"resource", "dependency", "environment", "indeterminate", "approval", "user_pause"}
)
_REQUIRED_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "command_id",
        "request_hash",
        "operation",
        "state",
        "goal_id",
        "task_id",
        "node_id",
        "attempt",
        "task_revision",
        "responsibility_revision",
        "original_owner",
        "current_owner",
        "proposed_owner",
        "next_action",
        "deadline",
        "wait",
    }
)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if any(character.isspace() for character in normalized):
        raise ValueError(f"{label} must contain no whitespace")
    return normalized


def _revision(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _json_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty object")
    document = dict(value)
    try:
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-safe") from error
    normalized = json.loads(encoded)
    assert isinstance(normalized, dict)
    return normalized


def _next_action(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("next_action must be non-empty")
        return {"action": value.strip()}
    return _json_mapping(value, "next_action")


def _utc_deadline(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("deadline must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError("deadline must be an ISO-8601 UTC timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("deadline must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _typed_wait(
    value: object,
    *,
    responsible_owner: str,
    next_recheck_at: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("wait_reason must be an object with an explicit wait_kind")
    raw = dict(value)
    allowed = {
        "kind",
        "wait_kind",
        "detail",
        "reason",
        "release_condition",
        "responsible_owner",
        "next_recheck_at",
        "requires_user_action",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(f"wait_reason has unsupported fields: {sorted(unexpected)}")
    if "wait_kind" in raw and "kind" in raw and raw["wait_kind"] != raw["kind"]:
        raise ValueError("wait_reason.kind and wait_reason.wait_kind must match")
    supplied_kind = raw.get("wait_kind", raw.get("kind"))
    if supplied_kind not in RESPONSIBILITY_WAIT_KINDS:
        raise ValueError(
            "wait_reason.wait_kind must be resource, dependency, environment, "
            "indeterminate, approval, or user_pause"
        )
    detail = raw.get("detail", raw.get("reason"))
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError("wait_reason.detail must be a non-empty string")
    release_condition = raw.get("release_condition")
    if not isinstance(release_condition, str) or not release_condition.strip():
        raise ValueError("wait_reason.release_condition must be a non-empty string")
    supplied_owner = raw.get("responsible_owner", responsible_owner)
    if supplied_owner != responsible_owner:
        raise ValueError("wait_reason.responsible_owner must match current_owner")
    embedded_recheck = raw.get("next_recheck_at")
    if next_recheck_at is not None and embedded_recheck is not None and next_recheck_at != embedded_recheck:
        raise ValueError("next_recheck_at does not match wait_reason.next_recheck_at")
    recheck = next_recheck_at if next_recheck_at is not None else embedded_recheck
    if supplied_kind == "user_pause":
        if raw.get("requires_user_action") is not True:
            raise ValueError("user_pause waits require requires_user_action=true")
        if recheck is not None:
            raise ValueError("user_pause waits must not schedule an automatic recheck")
        return {
            "wait_kind": supplied_kind,
            "detail": detail.strip(),
            "release_condition": release_condition.strip(),
            "responsible_owner": responsible_owner,
            "next_recheck_at": None,
            "requires_user_action": True,
        }
    if raw.get("requires_user_action") not in {None, False}:
        raise ValueError("only user_pause waits may require user action")
    if recheck is None:
        raise ValueError("non-user_pause waits require next_recheck_at")
    return {
        "wait_kind": supplied_kind,
        "detail": detail.strip(),
        "release_condition": release_condition.strip(),
        "responsible_owner": responsible_owner,
        "next_recheck_at": _utc_deadline(recheck),
        "requires_user_action": False,
    }


class ResponsibilityLedger:
    """Append and read task-scoped accountability snapshots.

    ``WorkbenchStore.transaction()`` serializes every mutation.  The ledger
    therefore uses the event row itself as both durable state and command
    receipt, without adding a table, a scheduler, or a second owner lease.
    """

    def __init__(self, store: WorkbenchStore):
        self.store = store

    def open(
        self,
        *,
        command_id: str,
        task_id: str,
        goal_id: str,
        node_id: str,
        attempt: int,
        task_revision: int,
        owner: str,
        next_action: str | Mapping[str, Any],
        deadline: str,
    ) -> dict[str, Any]:
        """Record the source session accountable for one exact task identity.

        The source owner must already be named by the task contract or by a
        permanent session route.  Opening only records that fact; it does not
        create a route or grant the source session any task-control authority.
        """

        identity = self._identity(task_id, goal_id, node_id, attempt, task_revision)
        source_owner = _identifier(owner, "owner")
        normalized_action = _next_action(next_action)
        normalized_deadline = _utc_deadline(deadline)
        namespaced_command_id = self._command_id(identity["task_id"], command_id)
        request = {
            "operation": "open",
            **identity,
            "owner": source_owner,
            "next_action": normalized_action,
            "deadline": normalized_deadline,
        }
        request_hash = canonical_hash(request)

        with self.store.transaction() as connection:
            replay = self._replay_or_conflict(
                connection,
                task_id=identity["task_id"],
                command_id=namespaced_command_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            task = self._validate_task_identity(connection, identity)
            if self._latest_snapshot(connection, identity["task_id"], identity["goal_id"]) is not None:
                raise StateConflictError(
                    f"responsibility goal {identity['goal_id']!r} is already open for task "
                    f"{identity['task_id']!r}"
                )
            self._validate_source_owner(connection, task, identity["task_id"], source_owner)
            snapshot = self._snapshot(
                command_id=namespaced_command_id,
                request_hash=request_hash,
                operation="open",
                state="open",
                identity=identity,
                responsibility_revision=1,
                original_owner=source_owner,
                current_owner=source_owner,
                proposed_owner=None,
                next_action=normalized_action,
                deadline=normalized_deadline,
                wait=None,
            )
            return self._append(connection, "responsibility.opened", snapshot)

    def propose_handoff(
        self,
        *,
        command_id: str,
        task_id: str,
        goal_id: str,
        node_id: str,
        attempt: int,
        task_revision: int,
        expected_responsibility_revision: int,
        current_owner: str,
        proposed_owner: str,
        next_action: str | Mapping[str, Any],
        deadline: str,
    ) -> dict[str, Any]:
        """Record a proposed recipient while retaining the current owner.

        A proposal is an accountability note only.  The proposed owner gains
        neither an execution lease nor access to task resources here.
        """

        identity = self._identity(task_id, goal_id, node_id, attempt, task_revision)
        expected_revision = _revision(
            expected_responsibility_revision,
            "expected_responsibility_revision",
            minimum=1,
        )
        owner = _identifier(current_owner, "current_owner")
        recipient = _identifier(proposed_owner, "proposed_owner")
        if recipient == owner:
            raise ValueError("proposed_owner must differ from current_owner")
        normalized_action = _next_action(next_action)
        normalized_deadline = _utc_deadline(deadline)
        namespaced_command_id = self._command_id(identity["task_id"], command_id)
        request = {
            "operation": "propose_handoff",
            **identity,
            "expected_responsibility_revision": expected_revision,
            "current_owner": owner,
            "proposed_owner": recipient,
            "next_action": normalized_action,
            "deadline": normalized_deadline,
        }
        request_hash = canonical_hash(request)

        with self.store.transaction() as connection:
            replay = self._replay_or_conflict(
                connection,
                task_id=identity["task_id"],
                command_id=namespaced_command_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            self._validate_task_identity(connection, identity)
            current = self._require_current_snapshot(connection, identity)
            self._validate_expected_revision(current, expected_revision)
            if current["current_owner"] != owner:
                raise StateConflictError("current_owner does not match the responsibility ledger")
            snapshot = self._snapshot(
                command_id=namespaced_command_id,
                request_hash=request_hash,
                operation="propose_handoff",
                state="handoff_proposed",
                identity=identity,
                responsibility_revision=expected_revision + 1,
                original_owner=str(current["original_owner"]),
                current_owner=owner,
                proposed_owner=recipient,
                next_action=normalized_action,
                deadline=normalized_deadline,
                wait=None,
            )
            return self._append(connection, "responsibility.handoff_proposed", snapshot)

    def claim_handoff(
        self,
        *,
        command_id: str,
        task_id: str,
        goal_id: str,
        node_id: str,
        attempt: int,
        task_revision: int,
        expected_responsibility_revision: int,
        claimant: str,
        next_action: str | Mapping[str, Any],
        deadline: str,
    ) -> dict[str, Any]:
        """Transfer accountability after the exact proposed recipient claims it.

        The claim is fenced by the proposal's task/node/attempt/revision
        identity.  It records no execution intent and cannot restart or claim
        a paused or cancelled task.
        """

        identity = self._identity(task_id, goal_id, node_id, attempt, task_revision)
        expected_revision = _revision(
            expected_responsibility_revision,
            "expected_responsibility_revision",
            minimum=1,
        )
        recipient = _identifier(claimant, "claimant")
        normalized_action = _next_action(next_action)
        normalized_deadline = _utc_deadline(deadline)
        namespaced_command_id = self._command_id(identity["task_id"], command_id)
        request = {
            "operation": "claim_handoff",
            **identity,
            "expected_responsibility_revision": expected_revision,
            "claimant": recipient,
            "next_action": normalized_action,
            "deadline": normalized_deadline,
        }
        request_hash = canonical_hash(request)

        with self.store.transaction() as connection:
            replay = self._replay_or_conflict(
                connection,
                task_id=identity["task_id"],
                command_id=namespaced_command_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            task = self._validate_task_identity(connection, identity)
            current = self._require_current_snapshot(connection, identity)
            self._validate_expected_revision(current, expected_revision)
            self._validate_claim_identity(current, identity)
            if task["state"] in {"paused", "cancelled"}:
                raise StateConflictError(
                    f"cannot claim responsibility while task is {task['state']!r}"
                )
            if current["proposed_owner"] != recipient:
                raise StateConflictError("claimant is not the proposed responsibility recipient")
            if self._deadline_expired(str(current["deadline"])):
                raise StateConflictError("cannot claim an expired responsibility handoff")
            self._validate_task_session_owner(
                connection,
                task,
                identity["task_id"],
                recipient,
                label="claimant",
            )
            snapshot = self._snapshot(
                command_id=namespaced_command_id,
                request_hash=request_hash,
                operation="claim_handoff",
                state="claimed",
                identity=identity,
                responsibility_revision=expected_revision + 1,
                original_owner=str(current["original_owner"]),
                current_owner=recipient,
                proposed_owner=None,
                next_action=normalized_action,
                deadline=normalized_deadline,
                wait=None,
            )
            return self._append(connection, "responsibility.handoff_claimed", snapshot)

    def defer(
        self,
        *,
        command_id: str,
        task_id: str,
        goal_id: str,
        node_id: str,
        attempt: int,
        task_revision: int,
        expected_responsibility_revision: int,
        current_owner: str,
        wait_reason: object,
        next_action: str | Mapping[str, Any],
        deadline: str,
        next_recheck_at: str | None = None,
    ) -> dict[str, Any]:
        """Record a typed wait without dropping current accountability.

        A pending handoff remains pending across a deferral; expiry and a wait
        never select a recipient or transfer the current owner automatically.
        """

        identity = self._identity(task_id, goal_id, node_id, attempt, task_revision)
        expected_revision = _revision(
            expected_responsibility_revision,
            "expected_responsibility_revision",
            minimum=1,
        )
        owner = _identifier(current_owner, "current_owner")
        normalized_wait = _typed_wait(
            wait_reason,
            responsible_owner=owner,
            next_recheck_at=next_recheck_at,
        )
        normalized_action = _next_action(next_action)
        normalized_deadline = _utc_deadline(deadline)
        namespaced_command_id = self._command_id(identity["task_id"], command_id)
        request = {
            "operation": "defer",
            **identity,
            "expected_responsibility_revision": expected_revision,
            "current_owner": owner,
            "wait": normalized_wait,
            "next_action": normalized_action,
            "deadline": normalized_deadline,
        }
        request_hash = canonical_hash(request)

        with self.store.transaction() as connection:
            replay = self._replay_or_conflict(
                connection,
                task_id=identity["task_id"],
                command_id=namespaced_command_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            task = self._validate_task_identity(connection, identity)
            self._validate_new_wait(normalized_wait, normalized_deadline, str(task["state"]))
            current = self._require_current_snapshot(connection, identity)
            self._validate_expected_revision(current, expected_revision)
            if current["current_owner"] != owner:
                raise StateConflictError("current_owner does not match the responsibility ledger")
            snapshot = self._snapshot(
                command_id=namespaced_command_id,
                request_hash=request_hash,
                operation="defer",
                state="deferred",
                identity=identity,
                responsibility_revision=expected_revision + 1,
                original_owner=str(current["original_owner"]),
                current_owner=owner,
                proposed_owner=current["proposed_owner"],
                next_action=normalized_action,
                deadline=normalized_deadline,
                wait=normalized_wait,
            )
            return self._append(connection, "responsibility.deferred", snapshot)

    def inspect(self, *, task_id: str, goal_id: str) -> dict[str, Any]:
        """Return the newest snapshot for exactly one task goal without mutation."""

        normalized_task_id = _identifier(task_id, "task_id")
        normalized_goal_id = _identifier(goal_id, "goal_id")
        with self.store.connection() as connection:
            task = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (normalized_task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(normalized_task_id)
            current = self._latest_snapshot(connection, normalized_task_id, normalized_goal_id)
            if current is None:
                raise KeyError((normalized_task_id, normalized_goal_id))
            receipt = self._receipt_from_row(current["row"], current["snapshot"])
        return {**receipt, "deadline_expired": self._deadline_expired(str(receipt["deadline"]))}

    def reconcile_terminal(
        self,
        coordinator_epoch: int,
        limit: int = _MAX_RECONCILE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Append bounded terminal snapshots for already-terminal task state.

        This is an Authority-only projection. It observes the durable task and
        optional delivery objective, then records responsibility completion or
        cancellation without changing task, node, lease, route, or owner data.
        """

        epoch = _revision(coordinator_epoch, "coordinator_epoch", minimum=1)
        bounded_limit = self._bounded_limit(limit, "limit", _MAX_RECONCILE_LIMIT)
        with self.store.transaction() as connection:
            self.store._assert_active_coordinator(connection, epoch)
            receipts: list[dict[str, Any]] = []
            for candidate in self._terminal_candidates(connection, bounded_limit):
                task_id = str(candidate["task_id"])
                goal_id = str(candidate["goal_id"])
                current = self._latest_snapshot(connection, task_id, goal_id)
                if current is None or int(current["row"]["cursor"]) != int(candidate["cursor"]):
                    raise StateConflictError("terminal responsibility snapshot compare-and-set failed")
                source = current["snapshot"]
                if source["state"] not in _NONTERMINAL_STATES:
                    raise StateConflictError("terminal reconciliation selected an already terminal snapshot")
                task = connection.execute(
                    "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise StateConflictError("terminal reconciliation task disappeared")
                if (
                    str(task["state"]) != str(candidate["task_state"])
                    or int(task["state_revision"]) != int(candidate["task_revision"])
                ):
                    raise StateConflictError("terminal task state changed during reconciliation")
                node = connection.execute(
                    "SELECT 1 FROM nodes WHERE task_id = ? AND node_id = ?",
                    (task_id, source["node_id"]),
                ).fetchone()
                if node is None:
                    raise StateConflictError("terminal responsibility node no longer exists")
                delivery = connection.execute(
                    "SELECT objective_id, state FROM delivery_objectives WHERE task_id = ?", (task_id,)
                ).fetchone()
                self._validate_terminal_delivery(candidate, delivery)
                terminal_state = "cancelled" if task["state"] == "cancelled" else "fulfilled"
                terminal_source = {
                    "task_state": str(task["state"]),
                    "task_revision": int(task["state_revision"]),
                    "delivery_objective_id": (
                        str(delivery["objective_id"]) if delivery is not None else None
                    ),
                    "delivery_state": str(delivery["state"]) if delivery is not None else None,
                }
                identity = {
                    "task_id": task_id,
                    "goal_id": goal_id,
                    "node_id": str(source["node_id"]),
                    "attempt": int(source["attempt"]),
                    "task_revision": int(task["state_revision"]),
                }
                command_id = self._command_id(
                    task_id,
                    (
                        f"terminal:{goal_id}:{candidate['cursor']}:"
                        f"{task['state_revision']}:{terminal_state}"
                    ),
                )
                request_hash = canonical_hash(
                    {
                        "operation": "reconcile_terminal",
                        "source_cursor": int(candidate["cursor"]),
                        "source_snapshot_hash": canonical_hash(source),
                        "terminal_source": terminal_source,
                    }
                )
                replay = self._replay_or_conflict(
                    connection,
                    task_id=task_id,
                    command_id=command_id,
                    request_hash=request_hash,
                )
                if replay is not None:
                    receipts.append(replay)
                    continue
                snapshot = self._snapshot(
                    command_id=command_id,
                    request_hash=request_hash,
                    operation="reconcile_terminal",
                    state=terminal_state,
                    identity=identity,
                    responsibility_revision=int(source["responsibility_revision"]) + 1,
                    original_owner=str(source["original_owner"]),
                    current_owner=str(source["current_owner"]),
                    proposed_owner=source["proposed_owner"],
                    next_action=source["next_action"],
                    deadline=str(source["deadline"]),
                    wait=source["wait"],
                    terminal_source=terminal_source,
                )
                receipts.append(
                    self._append(connection, f"responsibility.{terminal_state}", snapshot)
                )
            return receipts

    def list_for_task(
        self,
        task_id: str,
        limit: int = _MAX_LIST_LIMIT,
        cursor: int = 0,
    ) -> dict[str, Any]:
        """List latest goal snapshots for one task through a bounded cursor page."""

        normalized_task_id = _identifier(task_id, "task_id")
        bounded_limit = self._bounded_limit(limit, "limit", _MAX_LIST_LIMIT)
        after = _revision(cursor, "cursor", minimum=0)
        with self.store.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (normalized_task_id,)
            ).fetchone() is None:
                raise KeyError(normalized_task_id)
            placeholders = ", ".join("?" for _ in _STATES)
            rows = connection.execute(
                f"""
                WITH latest AS (
                    SELECT e.cursor, e.event_type, e.task_id, e.node_id, e.payload_json,
                           e.created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY json_extract(e.payload_json, '$.goal_id')
                               ORDER BY e.cursor DESC
                           ) AS goal_rank
                    FROM events AS e
                    WHERE e.task_id = ?
                      AND e.event_type GLOB ?
                      AND json_valid(e.payload_json)
                      AND json_extract(e.payload_json, '$.schema_version') = ?
                      AND json_type(e.payload_json, '$.goal_id') = 'text'
                      AND json_extract(e.payload_json, '$.state') IN ({placeholders})
                )
                SELECT cursor, event_type, task_id, node_id, payload_json, created_at
                FROM latest
                WHERE goal_rank = 1 AND cursor > ?
                ORDER BY cursor
                LIMIT ?
                """,
                (
                    normalized_task_id,
                    _EVENT_GLOB,
                    _SCHEMA_VERSION,
                    *sorted(_STATES),
                    after,
                    bounded_limit + 1,
                ),
            ).fetchall()
        selected = rows[:bounded_limit]
        items = [
            self._receipt_from_row(row, self._snapshot_from_row(row)) for row in selected
        ]
        return {
            "task_id": normalized_task_id,
            "items": items,
            "next_cursor": int(selected[-1]["cursor"]) if len(rows) > bounded_limit else None,
        }

    @staticmethod
    def _identity(
        task_id: object,
        goal_id: object,
        node_id: object,
        attempt: object,
        task_revision: object,
    ) -> dict[str, Any]:
        return {
            "task_id": _identifier(task_id, "task_id"),
            "goal_id": _identifier(goal_id, "goal_id"),
            "node_id": _identifier(node_id, "node_id"),
            "attempt": _revision(attempt, "attempt", minimum=0),
            "task_revision": _revision(task_revision, "task_revision", minimum=1),
        }

    @staticmethod
    def _bounded_limit(value: object, label: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{label} must be an integer from 1 through {maximum}")
        return value

    @staticmethod
    def _command_id(task_id: str, command_id: object) -> str:
        raw_command_id = _identifier(command_id, "command_id")
        namespace = f"responsibility:{task_id}:"
        if raw_command_id.startswith("responsibility:") and not raw_command_id.startswith(namespace):
            raise ValueError("responsibility command_id belongs to a different task")
        return raw_command_id if raw_command_id.startswith(namespace) else namespace + raw_command_id

    def _terminal_candidates(self, connection, limit: int):
        nonterminal_placeholders = ", ".join("?" for _ in _NONTERMINAL_STATES)
        return connection.execute(
            f"""
            WITH terminal_task_events AS (
                SELECT e.cursor, e.event_type, e.task_id, e.node_id, e.payload_json,
                       e.created_at, json_extract(e.payload_json, '$.goal_id') AS goal_id,
                       json_extract(e.payload_json, '$.state') AS snapshot_state,
                       t.state AS task_state, t.state_revision AS task_revision,
                       d.objective_id AS objective_id, d.state AS delivery_state,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.task_id, json_extract(e.payload_json, '$.goal_id')
                           ORDER BY e.cursor DESC
                       ) AS goal_rank
                FROM events AS e
                JOIN tasks AS t ON t.task_id = e.task_id
                LEFT JOIN delivery_objectives AS d ON d.task_id = t.task_id
                WHERE e.event_type GLOB ?
                  AND json_valid(e.payload_json)
                  AND json_extract(e.payload_json, '$.schema_version') = ?
                  AND json_type(e.payload_json, '$.goal_id') = 'text'
                  AND (
                      t.state = 'cancelled'
                      OR (
                          t.state = 'accepted'
                          AND (d.objective_id IS NULL OR d.state = 'complete')
                      )
                  )
            )
            SELECT cursor, event_type, task_id, node_id, payload_json, created_at,
                   goal_id, snapshot_state, task_state, task_revision,
                   objective_id, delivery_state
            FROM terminal_task_events
            WHERE goal_rank = 1 AND snapshot_state IN ({nonterminal_placeholders})
            ORDER BY CASE task_state WHEN 'cancelled' THEN 0 ELSE 1 END,
                     task_id, goal_id, cursor
            LIMIT ?
            """,
            (
                _EVENT_GLOB,
                _SCHEMA_VERSION,
                *sorted(_NONTERMINAL_STATES),
                limit,
            ),
        ).fetchall()

    @staticmethod
    def _validate_terminal_delivery(candidate, delivery) -> None:
        candidate_objective = candidate["objective_id"]
        candidate_state = candidate["delivery_state"]
        current_objective = delivery["objective_id"] if delivery is not None else None
        current_state = delivery["state"] if delivery is not None else None
        if (candidate_objective, candidate_state) != (current_objective, current_state):
            raise StateConflictError("delivery state changed during terminal reconciliation")
        task_state = str(candidate["task_state"])
        if task_state == "cancelled":
            return
        if task_state != "accepted":
            raise StateConflictError("terminal reconciliation task is not accepted or cancelled")
        if delivery is not None and delivery["state"] != "complete":
            raise StateConflictError("accepted task has incomplete delivery objective")

    @staticmethod
    def _validate_task_identity(connection, identity: Mapping[str, Any]):
        task = connection.execute(
            "SELECT state, state_revision, contract_json FROM tasks WHERE task_id = ?",
            (identity["task_id"],),
        ).fetchone()
        if task is None:
            raise KeyError(str(identity["task_id"]))
        node = connection.execute(
            "SELECT attempt FROM nodes WHERE task_id = ? AND node_id = ?",
            (identity["task_id"], identity["node_id"]),
        ).fetchone()
        if node is None:
            raise KeyError((str(identity["task_id"]), str(identity["node_id"])))
        if int(task["state_revision"]) != identity["task_revision"]:
            raise StateConflictError(
                f"expected task revision {identity['task_revision']}, found {task['state_revision']}"
            )
        if int(node["attempt"]) != identity["attempt"]:
            raise StateConflictError(
                f"expected node attempt {identity['attempt']}, found {node['attempt']}"
            )
        return task

    @staticmethod
    def _validate_source_owner(connection, task, task_id: str, source_owner: str) -> None:
        ResponsibilityLedger._validate_task_session_owner(
            connection,
            task,
            task_id,
            source_owner,
            label="owner",
        )

    @staticmethod
    def _validate_task_session_owner(
        connection,
        task,
        task_id: str,
        session_owner: str,
        *,
        label: str,
    ) -> None:
        try:
            contract = json.loads(str(task["contract_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("task contract is invalid JSON") from error
        contract_owner = contract.get("source_thread_id") if isinstance(contract, dict) else None
        route = connection.execute(
            """
            SELECT 1 FROM session_notification_routes
            WHERE task_id = ? AND source_thread_id = ?
            LIMIT 1
            """,
            (task_id, session_owner),
        ).fetchone()
        if session_owner != contract_owner and route is None:
            raise PermissionError(
                f"{label} is not the task contract source session or a permanent route"
            )

    def _replay_or_conflict(
        self,
        connection,
        *,
        task_id: str,
        command_id: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            """
            SELECT cursor, event_type, task_id, node_id, payload_json, created_at
            FROM events
            WHERE task_id = ?
              AND event_type GLOB ?
              AND json_valid(payload_json)
              AND json_extract(payload_json, '$.schema_version') = ?
              AND json_extract(payload_json, '$.command_id') = ?
            ORDER BY cursor
            LIMIT 2
            """,
            (task_id, _EVENT_GLOB, _SCHEMA_VERSION, command_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise StateConflictError(f"responsibility command {command_id!r} has multiple receipts")
        snapshot = self._snapshot_from_row(rows[0])
        if snapshot["request_hash"] != request_hash:
            raise CommandConflictError(
                f"command {command_id!r} was already used with a different responsibility request"
            )
        return self._receipt_from_row(rows[0], snapshot)

    def _require_current_snapshot(self, connection, identity: Mapping[str, Any]) -> dict[str, Any]:
        current = self._latest_snapshot(connection, str(identity["task_id"]), str(identity["goal_id"]))
        if current is None:
            raise KeyError((str(identity["task_id"]), str(identity["goal_id"])))
        return current["snapshot"]

    def _latest_snapshot(
        self,
        connection,
        task_id: str,
        goal_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT cursor, event_type, task_id, node_id, payload_json, created_at
            FROM events
            WHERE task_id = ?
              AND event_type GLOB ?
              AND json_valid(payload_json)
              AND json_extract(payload_json, '$.schema_version') = ?
              AND json_extract(payload_json, '$.goal_id') = ?
            ORDER BY cursor DESC
            LIMIT 1
            """,
            (task_id, _EVENT_GLOB, _SCHEMA_VERSION, goal_id),
        ).fetchone()
        if row is None:
            return None
        return {"row": row, "snapshot": self._snapshot_from_row(row)}

    @staticmethod
    def _validate_expected_revision(snapshot: Mapping[str, Any], expected_revision: int) -> None:
        if snapshot["responsibility_revision"] != expected_revision:
            raise StateConflictError(
                "expected responsibility revision "
                f"{expected_revision}, found {snapshot['responsibility_revision']}"
            )

    @staticmethod
    def _validate_claim_identity(snapshot: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
        for field in ("node_id", "attempt", "task_revision"):
            if snapshot[field] != identity[field]:
                raise StateConflictError(
                    f"claim {field} does not match the proposed handoff identity"
                )

    @staticmethod
    def _validate_new_wait(
        wait: Mapping[str, Any],
        deadline: str,
        task_state: str,
    ) -> None:
        if wait["wait_kind"] == "user_pause":
            if task_state != "paused":
                raise StateConflictError("user_pause wait requires the task to be paused")
            return
        recheck = datetime.fromisoformat(str(wait["next_recheck_at"]))
        now = datetime.fromisoformat(now_iso())
        if recheck <= now:
            raise ValueError("next_recheck_at must be in the future")
        if recheck > datetime.fromisoformat(deadline):
            raise ValueError("next_recheck_at must not be after the responsibility deadline")

    @staticmethod
    def _deadline_expired(deadline: str) -> bool:
        return datetime.fromisoformat(deadline) <= datetime.fromisoformat(now_iso())

    @staticmethod
    def _snapshot(
        *,
        command_id: str,
        request_hash: str,
        operation: str,
        state: str,
        identity: Mapping[str, Any],
        responsibility_revision: int,
        original_owner: str,
        current_owner: str,
        proposed_owner: str | None,
        next_action: Mapping[str, Any],
        deadline: str,
        wait: Mapping[str, Any] | None,
        terminal_source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "schema_version": _SCHEMA_VERSION,
            "command_id": command_id,
            "request_hash": request_hash,
            "operation": operation,
            "state": state,
            **dict(identity),
            "responsibility_revision": responsibility_revision,
            "original_owner": original_owner,
            "current_owner": current_owner,
            "proposed_owner": proposed_owner,
            "next_action": dict(next_action),
            "deadline": deadline,
            "wait": dict(wait) if wait is not None else None,
        }
        if terminal_source is not None:
            snapshot["terminal_source"] = dict(terminal_source)
        return snapshot

    def _append(self, connection, event_type: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        cursor = self.store._event(
            connection,
            event_type,
            str(snapshot["task_id"]),
            str(snapshot["node_id"]),
            dict(snapshot),
            created_at=timestamp,
        )
        return {
            "cursor": cursor,
            "event_type": event_type,
            "created_at": timestamp,
            **dict(snapshot),
        }

    @staticmethod
    def _snapshot_from_row(row) -> dict[str, Any]:
        try:
            snapshot = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("responsibility event payload is invalid JSON") from error
        if not isinstance(snapshot, dict) or not _REQUIRED_SNAPSHOT_FIELDS.issubset(snapshot):
            raise StateConflictError("responsibility event does not contain a complete snapshot")
        if snapshot["schema_version"] != _SCHEMA_VERSION:
            raise StateConflictError("unsupported responsibility event schema_version")
        if snapshot["operation"] not in _OPERATIONS or snapshot["state"] not in _STATES:
            raise StateConflictError("responsibility event has an unsupported transition")
        if snapshot["operation"] == "reconcile_terminal":
            if snapshot["state"] not in _TERMINAL_STATES:
                raise StateConflictError("terminal responsibility event has a nonterminal state")
        elif snapshot["state"] not in _NONTERMINAL_STATES:
            raise StateConflictError("only terminal reconciliation may append a terminal state")
        for field in (
            "command_id",
            "request_hash",
            "goal_id",
            "task_id",
            "node_id",
            "original_owner",
            "current_owner",
            "deadline",
        ):
            _identifier(snapshot[field], f"responsibility event {field}")
        if snapshot["task_id"] != row["task_id"] or snapshot["node_id"] != row["node_id"]:
            raise StateConflictError("responsibility event identity does not match its event row")
        _revision(snapshot["attempt"], "responsibility event attempt", minimum=0)
        _revision(snapshot["task_revision"], "responsibility event task_revision", minimum=1)
        _revision(
            snapshot["responsibility_revision"],
            "responsibility event responsibility_revision",
            minimum=1,
        )
        if snapshot["proposed_owner"] is not None:
            _identifier(snapshot["proposed_owner"], "responsibility event proposed_owner")
        _json_mapping(snapshot["next_action"], "responsibility event next_action")
        _utc_deadline(snapshot["deadline"])
        if snapshot["wait"] is not None:
            _json_mapping(snapshot["wait"], "responsibility event wait")
        terminal_source = snapshot.get("terminal_source")
        if snapshot["operation"] == "reconcile_terminal":
            if not isinstance(terminal_source, Mapping):
                raise StateConflictError("terminal responsibility event lacks terminal source")
            expected_source_fields = {
                "task_state",
                "task_revision",
                "delivery_objective_id",
                "delivery_state",
            }
            if set(terminal_source) != expected_source_fields:
                raise StateConflictError("terminal responsibility event has invalid terminal source")
            terminal_task_state = terminal_source["task_state"]
            if terminal_task_state not in {"accepted", "cancelled"}:
                raise StateConflictError("terminal responsibility event has invalid task state")
            if (
                (terminal_task_state == "accepted" and snapshot["state"] != "fulfilled")
                or (terminal_task_state == "cancelled" and snapshot["state"] != "cancelled")
                or terminal_source["task_revision"] != snapshot["task_revision"]
            ):
                raise StateConflictError("terminal responsibility event does not match terminal task state")
            delivery_objective_id = terminal_source["delivery_objective_id"]
            delivery_state = terminal_source["delivery_state"]
            if (delivery_objective_id is None) != (delivery_state is None):
                raise StateConflictError("terminal responsibility delivery source is incomplete")
            if delivery_objective_id is not None:
                _identifier(delivery_objective_id, "terminal responsibility delivery_objective_id")
                _identifier(delivery_state, "terminal responsibility delivery_state")
            if snapshot["state"] == "fulfilled" and delivery_state not in {None, "complete"}:
                raise StateConflictError("fulfilled responsibility has incomplete delivery source")
        elif terminal_source is not None:
            raise StateConflictError("nonterminal responsibility event has terminal source")
        return snapshot

    @staticmethod
    def _receipt_from_row(row, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "cursor": int(row["cursor"]),
            "event_type": str(row["event_type"]),
            "created_at": str(row["created_at"]),
            **dict(snapshot),
        }
