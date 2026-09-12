"""Queue a verified blocked source patch for the existing worker retry path.

This action is deliberately narrower than ordinary blocked-worktree recovery.
It retains the original blocked result unchanged, captures only the current
scope-checked source delta, and lets the normal next worker attempt decide
whether that patch can repair the implementation.  The existing verifier
remains the sole task-acceptance transition.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any

from .dirty_worktree_recovery import DirtyWorktreeRecoveryError
from .model import canonical_hash, canonical_json, now_iso
from .node_recovery_policy import RecoveryPolicy
from .store import StateConflictError, WorkbenchStore


_SCHEMA_VERSION = 1
_KIND = "blocked-source-repair-v1"
_EVENT_TYPE = "node.blocked_source_repair_queued"
_MODE = "blocked_source_repair"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUEST_ID_MAX = 200
_REASON_MAX = 500


def blocked_source_repair(
    store: WorkbenchStore,
    task_id: str,
    node_id: str,
    *,
    expected_revision: int,
    expected_attempt: int,
    expected_contract_hash: str,
    request_id: str,
    reason: str,
    dry_run: bool,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Preview or queue one policy-authorized source-only blocked repair.

    The preview performs only read-only source and durable-state inspection.
    Apply requires that preview's fingerprint, then atomically queues the
    existing failed-attempt recovery binding. The existing pre-dispatch
    recovery helper captures artifacts later, outside SQLite's write lock.

    @param store: Existing task-state authority.
    @param task_id: Task containing the blocked worker.
    @param node_id: Blocked non-verifier worker to repair.
    @param expected_revision: Current task revision observed by the caller.
    @param expected_attempt: Current blocked worker attempt.
    @param expected_contract_hash: Immutable task contract digest.
    @param request_id: Stable idempotency key for this logical repair request.
    @param reason: Bounded operator-visible repair reason.
    @param dry_run: Whether to return a read-only preview.
    @param expected_fingerprint: Required preview fingerprint for apply.
    @returns: A preview or durable queued receipt.
    """

    request = _request(
        task_id=task_id,
        node_id=node_id,
        expected_revision=expected_revision,
        expected_attempt=expected_attempt,
        expected_contract_hash=expected_contract_hash,
        request_id=request_id,
        reason=reason,
    )
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    if expected_fingerprint is not None:
        _digest(expected_fingerprint, "expected_fingerprint")

    # A completed request is authoritative even after its task revision has
    # advanced. Check it before any current-state fence so retrying a lost
    # response cannot accidentally queue a second attempt.
    existing = blocked_source_repair_receipt(store, request["request_id"])
    if existing is not None:
        if existing["request_fingerprint"] != request["request_fingerprint"]:
            raise StateConflictError(
                "blocked source repair request_id was already used with different input"
            )
        return existing

    if dry_run:
        if expected_fingerprint is not None:
            raise ValueError("expected_fingerprint is only valid when dry_run is false")
        preflight = _preflight(store, request)
        return _preview(preflight)
    if expected_fingerprint is None:
        raise ValueError("blocked source repair apply requires expected_fingerprint from its preview")

    preflight = _preflight(store, request)
    if preflight["fingerprint"] != expected_fingerprint:
        raise StateConflictError(
            "blocked source repair preview fingerprint is stale; rerun dry-run"
        )
    return _queue(store, preflight)


def blocked_source_repair_receipt(
    store: WorkbenchStore,
    request_id: str,
) -> dict[str, Any] | None:
    """Return an existing queued receipt without authorizing or replaying work.

    @param store: Existing task-state authority.
    @param request_id: Stable request id to look up.
    @returns: The durable queue receipt, or ``None`` when no request was queued.
    """

    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    normalized_request_id = _text(request_id, "request_id", maximum=_REQUEST_ID_MAX)
    with store.connection() as connection:
        return _receipt_from_connection(connection, normalized_request_id)


def _request(
    *,
    task_id: object,
    node_id: object,
    expected_revision: object,
    expected_attempt: object,
    expected_contract_hash: object,
    request_id: object,
    reason: object,
) -> dict[str, Any]:
    """Normalize the immutable input used by preview, apply, and dedupe."""

    normalized = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "task_id": _text(task_id, "task_id"),
        "node_id": _text(node_id, "node_id"),
        "expected_revision": _positive_int(expected_revision, "expected_revision"),
        "expected_attempt": _positive_int(expected_attempt, "expected_attempt"),
        "expected_contract_hash": _digest(expected_contract_hash, "expected_contract_hash"),
        "request_id": _text(request_id, "request_id", maximum=_REQUEST_ID_MAX),
        "reason": _text(reason, "reason", maximum=_REASON_MAX),
    }
    normalized["request_fingerprint"] = canonical_hash(normalized)
    return normalized


def _preflight(store: WorkbenchStore, request: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect source and durable inputs without acquiring a write lock."""

    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    try:
        snapshot, delta = store._prepare_source_only_blocked_recovery(
            str(request["task_id"]),
            str(request["node_id"]),
            expected_revision=int(request["expected_revision"]),
            expected_attempt=int(request["expected_attempt"]),
            # The source-only inspector validates all actual untracked paths
            # against the task and worker scopes. The capture below preserves
            # only that exact verified list.
            preserve_untracked=None,
            expected_checkpoint_sha=None,
            expected_source_delta_sha256=None,
        )
    except DirtyWorktreeRecoveryError as error:
        raise StateConflictError("blocked source repair source inspection failed: " + str(error)) from error
    candidate = snapshot.get("candidate")
    if not isinstance(candidate, Mapping):
        raise StateConflictError("blocked source repair durable candidate is invalid")
    task = candidate.get("task")
    node = candidate.get("node")
    if not isinstance(task, Mapping) or not isinstance(node, Mapping):
        raise StateConflictError("blocked source repair task or node candidate is invalid")
    if task.get("state") != "blocked" or node.get("state") != "blocked":
        raise StateConflictError("blocked source repair requires a blocked task and worker")
    contract_hash = snapshot.get("contract_hash")
    if not isinstance(contract_hash, str) or contract_hash != request["expected_contract_hash"]:
        raise StateConflictError("blocked source repair contract hash changed")
    if not delta.changed_paths:
        raise StateConflictError("blocked source repair requires a non-empty observed source delta")

    with store.connection() as connection:
        current_snapshot = store._blocked_source_only_recovery_durable_snapshot(
            connection,
            str(request["task_id"]),
            str(request["node_id"]),
            expected_revision=int(request["expected_revision"]),
            expected_attempt=int(request["expected_attempt"]),
        )
        if canonical_json(current_snapshot) != canonical_json(snapshot):
            raise StateConflictError("blocked source repair durable state changed during inspection")
        _assert_no_parallel_recovery_gate(connection, str(request["task_id"]))
        policy = _policy(connection, str(request["task_id"]))
        row = connection.execute(
            "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?",
            (request["task_id"], request["node_id"]),
        ).fetchone()
        if row is None:
            raise KeyError((request["task_id"], request["node_id"]))
        authorization = store._blocked_source_repair_authorization(
            connection,
            str(request["task_id"]),
            row,
            authorization_revision=int(request["expected_revision"]) + 1,
            source_result_json=str(snapshot["historical_result_json"]),
            observed_changed_paths=delta.changed_paths,
            source_delta_sha256=delta.sha256,
        )
    fingerprint = canonical_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "request": dict(request),
            "durable_snapshot": snapshot,
            "policy": policy,
            "source_delta_sha256": delta.sha256,
            "changed_paths": list(delta.changed_paths),
            "untracked_paths": list(delta.untracked_paths),
            "authorization": authorization,
        }
    )
    return {
        "request": dict(request),
        "snapshot": snapshot,
        "delta": delta,
        "policy": policy,
        "authorization": authorization,
        "fingerprint": fingerprint,
    }


def _queue(store: WorkbenchStore, preflight: Mapping[str, Any]) -> dict[str, Any]:
    """CAS the sealed retry binding after all source I/O has completed."""

    request = preflight["request"]
    snapshot = preflight["snapshot"]
    policy = preflight["policy"]
    authorization = preflight["authorization"]
    delta = preflight["delta"]
    if not all(isinstance(value, Mapping) for value in (request, snapshot, policy, authorization)):
        raise StateConflictError("blocked source repair queue preflight is invalid")
    timestamp = now_iso()
    with store.transaction() as connection:
        existing = _receipt_from_connection(connection, str(request["request_id"]))
        if existing is not None:
            if existing["request_fingerprint"] != request["request_fingerprint"]:
                raise StateConflictError(
                    "blocked source repair request_id was already used with different input"
                )
            return existing
        store.assert_no_active_validation(connection, str(request["task_id"]))
        _assert_no_parallel_recovery_gate(connection, str(request["task_id"]))
        current_snapshot = store._blocked_source_only_recovery_durable_snapshot(
            connection,
            str(request["task_id"]),
            str(request["node_id"]),
            expected_revision=int(request["expected_revision"]),
            expected_attempt=int(request["expected_attempt"]),
        )
        if canonical_json(current_snapshot) != canonical_json(snapshot):
            raise StateConflictError("blocked source repair durable state changed before queue")
        current_policy = _policy(connection, str(request["task_id"]))
        if canonical_json(current_policy) != canonical_json(policy):
            raise StateConflictError("blocked source repair policy changed before queue")
        row = connection.execute(
            "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?",
            (request["task_id"], request["node_id"]),
        ).fetchone()
        if row is None:
            raise KeyError((request["task_id"], request["node_id"]))
        current_authorization = store._blocked_source_repair_authorization(
            connection,
            str(request["task_id"]),
            row,
            authorization_revision=int(request["expected_revision"]) + 1,
            source_result_json=str(current_snapshot["historical_result_json"]),
            observed_changed_paths=tuple(delta.changed_paths),
            source_delta_sha256=delta.sha256,
        )
        if canonical_json(current_authorization) != canonical_json(authorization):
            raise StateConflictError("blocked source repair authorization changed before queue")
        prior = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE event_type = ? AND task_id = ? AND node_id = ?
                  AND json_extract(payload_json, '$.policy_revision') = ?
                """,
                (
                    _EVENT_TYPE,
                    request["task_id"],
                    request["node_id"],
                    policy["policy_revision"],
                ),
            ).fetchone()[0]
        )
        if prior >= int(policy["max_action_attempts"]):
            raise StateConflictError("blocked source repair policy action budget is exhausted")
        revision = int(request["expected_revision"]) + 1
        node_changed = connection.execute(
            """
            UPDATE nodes
            SET state = 'pending', worker_id = NULL, worktree = NULL,
                effective_executor = NULL, effective_model = NULL,
                started_at = NULL, settled_at = NULL, result_json = NULL,
                coordinator_epoch = 0, lease_epoch = 0, recovery_json = ?, updated_at = ?
            WHERE task_id = ? AND node_id = ? AND state = 'blocked' AND attempt = ?
              AND recovery_json IS NULL AND result_json = ?
            """,
            (
                canonical_json(current_authorization),
                timestamp,
                request["task_id"],
                request["node_id"],
                request["expected_attempt"],
                current_snapshot["historical_result_json"],
            ),
        ).rowcount
        if node_changed != 1:
            raise StateConflictError("blocked source repair node compare-and-set failed")
        task_changed = connection.execute(
            """
            UPDATE tasks
            SET state = 'queued', state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
            WHERE task_id = ? AND state = 'blocked' AND state_revision = ? AND contract_hash = ?
            """,
            (
                revision,
                timestamp,
                request["task_id"],
                request["expected_revision"],
                request["expected_contract_hash"],
            ),
        ).rowcount
        if task_changed != 1:
            raise StateConflictError("blocked source repair task compare-and-set failed")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "request_id": request["request_id"],
            "request_fingerprint": request["request_fingerprint"],
            "fingerprint": preflight["fingerprint"],
            "queued": True,
            "task_id": request["task_id"],
            "node_id": request["node_id"],
            "reason": request["reason"],
            "revision": revision,
            "authorization_revision": revision,
            "next_attempt": int(request["expected_attempt"]) + 1,
            "policy_revision": policy["policy_revision"],
            "max_action_attempts": policy["max_action_attempts"],
            "expected_contract_hash": request["expected_contract_hash"],
            "source_allocation_id": authorization["source_allocation_id"],
            "source_attempt": authorization["source"]["attempt"],
            "source_node_state": authorization["source_node_state"],
            "source_task_state": authorization["source_task_state"],
            "source_delta_sha256": delta.sha256,
            "changed_paths": list(delta.changed_paths),
            "untracked_paths": list(delta.untracked_paths),
            "source_result_sha256": sha256(
                str(current_snapshot["historical_result_json"]).encode("utf-8")
            ).hexdigest(),
            "source_only_ignored": True,
            "historical_result_unchanged": True,
            "mode": _MODE,
        }
        cursor = store._event(
            connection,
            _EVENT_TYPE,
            str(request["task_id"]),
            str(request["node_id"]),
            payload,
            created_at=timestamp,
        )
        store._event(
            connection,
            "task.state_changed",
            str(request["task_id"]),
            None,
            {
                "from": "blocked",
                "to": "queued",
                "revision": revision,
                "blocker": None,
                "blocked_source_repair": True,
            },
            created_at=timestamp,
        )
    return _receipt_from_payload(payload, cursor)


def _preview(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Render a compact read-only preview without creating artifacts."""

    request = preflight["request"]
    snapshot = preflight["snapshot"]
    policy = preflight["policy"]
    authorization = preflight["authorization"]
    delta = preflight["delta"]
    candidate = snapshot["candidate"]
    source = candidate["source"]
    return {
        "ok": True,
        "dry_run": True,
        "queued": False,
        "task_id": request["task_id"],
        "node_id": request["node_id"],
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "fingerprint": preflight["fingerprint"],
        "policy_revision": policy["policy_revision"],
        "max_action_attempts": policy["max_action_attempts"],
        "expected_contract_hash": request["expected_contract_hash"],
        "revision": request["expected_revision"],
        "next_attempt": int(request["expected_attempt"]) + 1,
        "source": {
            "attempt": authorization["source"]["attempt"],
            "worktree": source["worktree"],
            "branch": source["branch"],
            "base_sha": source["base_sha"],
            "allocation_id": source["allocation_id"],
        },
        "changed_paths": list(delta.changed_paths),
        "untracked_paths": list(delta.untracked_paths),
        "source_delta_sha256": delta.sha256,
        "source_result_sha256": sha256(
            str(snapshot["historical_result_json"]).encode("utf-8")
        ).hexdigest(),
        "source_node_state": authorization["source_node_state"],
        "source_task_state": authorization["source_task_state"],
        "historical_result_unchanged": True,
        "source_only_ignored": True,
    }


def _policy(connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Read and validate the current policy using the caller's DB snapshot."""

    row = connection.execute(
        "SELECT policy_json, policy_revision FROM node_recovery_policies WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise StateConflictError("blocked source repair policy is disabled")
    try:
        policy = RecoveryPolicy.from_dict(json.loads(str(row["policy_json"])))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StateConflictError("blocked source repair policy is invalid") from error
    revision = row["policy_revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or policy.enabled is not True
        or "repair_source" not in policy.allowed_actions
    ):
        raise StateConflictError("blocked source repair is not authorized by the current policy")
    return {
        "policy": policy.to_dict(),
        "policy_revision": revision,
        "max_action_attempts": policy.max_action_attempts,
    }


def _assert_no_parallel_recovery_gate(connection: sqlite3.Connection, task_id: str) -> None:
    """Keep source repair fenced from approvals and active task execution."""

    approval = connection.execute(
        "SELECT 1 FROM approvals WHERE task_id = ? AND decision IS NULL LIMIT 1",
        (task_id,),
    ).fetchone()
    if approval is not None:
        raise StateConflictError("blocked source repair is fenced by a pending approval")
    active = connection.execute(
        """
        SELECT node_id, state FROM nodes
        WHERE task_id = ? AND state IN ('running', 'indeterminate')
        ORDER BY node_id LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if active is not None:
        raise StateConflictError(
            "blocked source repair is fenced by active node "
            + str(active["node_id"])
            + ":"
            + str(active["state"])
        )


def _receipt_from_connection(
    connection: sqlite3.Connection,
    request_id: str,
) -> dict[str, Any] | None:
    """Read one receipt inside a caller-owned transaction."""

    rows = connection.execute(
        """
        SELECT cursor, payload_json FROM events
        WHERE event_type = ? AND json_extract(payload_json, '$.request_id') = ?
        ORDER BY cursor LIMIT 2
        """,
        (_EVENT_TYPE, request_id),
    ).fetchall()
    if not rows:
        return None
    receipts = [_event_receipt(row) for row in rows]
    first = receipts[0]
    if any(canonical_json(receipt) != canonical_json(first) for receipt in receipts[1:]):
        raise StateConflictError("blocked source repair request_id has conflicting queued receipts")
    return first


def _event_receipt(row: sqlite3.Row) -> dict[str, Any]:
    """Validate and render one immutable queued event as an API receipt."""

    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise StateConflictError("blocked source repair queued receipt is invalid JSON") from error
    if not isinstance(payload, dict):
        raise StateConflictError("blocked source repair queued receipt is invalid")
    return _receipt_from_payload(payload, int(row["cursor"]))


def _receipt_from_payload(payload: Mapping[str, Any], cursor: int) -> dict[str, Any]:
    """Validate one event payload and attach its event cursor."""

    required = {
        "schema_version",
        "kind",
        "request_id",
        "request_fingerprint",
        "fingerprint",
        "queued",
        "task_id",
        "node_id",
        "reason",
        "revision",
        "authorization_revision",
        "next_attempt",
        "policy_revision",
        "max_action_attempts",
        "expected_contract_hash",
        "source_allocation_id",
        "source_attempt",
        "source_node_state",
        "source_task_state",
        "source_delta_sha256",
        "changed_paths",
        "untracked_paths",
        "source_result_sha256",
        "source_only_ignored",
        "historical_result_unchanged",
        "mode",
    }
    if set(payload) != required:
        raise StateConflictError("blocked source repair queued receipt has an invalid shape")
    for field, maximum in (
        ("request_id", _REQUEST_ID_MAX),
        ("task_id", 512),
        ("node_id", 512),
        ("reason", _REASON_MAX),
        ("source_allocation_id", 512),
    ):
        _text(payload.get(field), field, maximum=maximum)
    for field in (
        "request_fingerprint",
        "fingerprint",
        "expected_contract_hash",
        "source_delta_sha256",
        "source_result_sha256",
    ):
        _digest(payload.get(field), field)
    for field in (
        "revision",
        "authorization_revision",
        "next_attempt",
        "policy_revision",
        "max_action_attempts",
        "source_attempt",
    ):
        _positive_int(payload.get(field), field)
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("kind") != _KIND
        or payload.get("queued") is not True
        or payload.get("source_node_state") != "blocked"
        or payload.get("source_task_state") != "blocked"
        or payload.get("source_only_ignored") is not True
        or payload.get("historical_result_unchanged") is not True
        or payload.get("mode") != _MODE
        or payload.get("authorization_revision") != payload.get("revision")
        or payload.get("next_attempt") != payload.get("source_attempt") + 1
    ):
        raise StateConflictError("blocked source repair queued receipt fields are invalid")
    changed_paths = payload.get("changed_paths")
    untracked_paths = payload.get("untracked_paths")
    if (
        not isinstance(changed_paths, list)
        or not changed_paths
        or not all(isinstance(path, str) and path for path in changed_paths)
        or changed_paths != sorted(set(changed_paths))
        or not isinstance(untracked_paths, list)
        or not all(isinstance(path, str) and path for path in untracked_paths)
        or untracked_paths != sorted(set(untracked_paths))
        or not set(untracked_paths).issubset(changed_paths)
        or isinstance(cursor, bool)
        or not isinstance(cursor, int)
        or cursor < 1
    ):
        raise StateConflictError("blocked source repair queued receipt paths are invalid")
    return {**dict(payload), "authorization_event_cursor": cursor}


def _positive_int(value: object, label: str) -> int:
    """Return one strict positive integer."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _digest(value: object, label: str) -> str:
    """Return one lowercase SHA-256 digest."""

    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    """Return one bounded single-line text field."""

    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} contains forbidden control characters")
    return value.strip()


__all__ = ["blocked_source_repair", "blocked_source_repair_receipt"]
