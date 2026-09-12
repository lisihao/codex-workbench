"""Govern one isolated, one-time pnpm lockfile handoff for a blocked worker.

The ordinary task graph keeps each node's declared write scope immutable.  A
blocked importer change can nevertheless need a lockfile that belongs to a
different, otherwise idle node.  This module records a narrow temporary lease
in the existing event journal, reconstructs the blocked node's recorded input
in a separate worktree, and publishes only a content-addressed lockfile
overlay.  It never changes the task's node plan, attempt, historical result,
or acceptance state.

All filesystem and subprocess work happens outside a SQLite write
transaction.  The short transactions only reserve or settle a previously
observed immutable binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from .config import WorkbenchConfig
from .dependency_inputs import (
    DependencyInput,
    DependencyInputError,
    apply_recorded_dependency_input,
    base_dependency_input,
    load_recorded_dependency_input,
    validate_dependency_input_lineage,
)
from .dirty_worktree_recovery import (
    DirtyWorktreeRecovery,
    DirtyWorktreeRecoveryError,
    PnpmOfflineMaterializer,
    SourceDelta,
    inspect_recovery_source_delta,
)
from .model import canonical_hash, canonical_json, now_iso
from .recovery_processes import RecoveryProcessError, assert_recovery_source_idle
from .store import StateConflictError, WorkbenchStore, _repository_identity
from .worktrees import WorktreeError, WorktreeManager, scope_allows


TOOL_NAME = "workbench_handoff_lockfile"
_SCHEMA_VERSION = 1
_KIND = "lockfile-handoff-v1"
_LOCKFILE = "pnpm-lock.yaml"
_RESERVED_EVENT = "lockfile_handoff.reserved"
_READY_EVENT = "lockfile_handoff.ready"
_FAILED_EVENT = "lockfile_handoff.failed"
_CANCELLED_EVENT = "lockfile_handoff.cancelled"
_NEEDS_ACTION_EVENT = "lockfile_handoff.needs_action"
_EVENT_TYPES = (
    _RESERVED_EVENT,
    _READY_EVENT,
    _FAILED_EVENT,
    _CANCELLED_EVENT,
    _NEEDS_ACTION_EVENT,
)
_TERMINAL_STATES = frozenset({"ready", "failed", "cancelled", "needs_action"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_REQUEST_ID = 200
_MAX_TEXT = 512
_MAX_ERROR = 1_000
_MAX_INPUT_FILES = 4_096
_MAX_CHANGED_IMPORTERS = 4_096


TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Preview, apply, inspect, reconcile, or cancel one governed temporary "
        "pnpm-lock.yaml handoff for a blocked non-verifier worker. Apply records "
        "only a lockfile overlay; it does not queue a task, create an attempt, "
        "or accept work."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["op", "task_id", "request_id"],
        "properties": {
            "op": {
                "enum": ["preview", "apply", "status", "reconcile", "cancel"],
            },
            "task_id": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT},
            "node_id": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT},
            "expected_revision": {"type": "integer", "minimum": 1},
            "expected_attempt": {"type": "integer", "minimum": 1},
            "expected_contract_hash": {
                "type": "string", "pattern": "^[0-9a-f]{64}$",
            },
            "request_id": {"type": "string", "minLength": 1, "maxLength": _MAX_REQUEST_ID},
            "expected_fingerprint": {
                "type": "string", "pattern": "^[0-9a-f]{64}$",
            },
            "operation_id": {
                "type": "string", "minLength": 1, "maxLength": _MAX_REQUEST_ID,
                "description": "A distinct Authority journal id for reconcile or cancel.",
            },
        },
    },
}


class LockfileHandoffError(StateConflictError):
    """A lockfile handoff cannot safely be reserved, replayed, or published."""


def lockfile_handoff(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one explicit operation against the lockfile-handoff journal.

    Preview and status perform only reads.  Apply requires an already executing
    Authority request whose id equals the business request id.  Reconcile and
    cancel use a separate Authority request id so a completed or unknown apply
    receipt can never be replayed under a new operation.

    @param config: Authority-local Workbench paths and managed runtime context.
    @param store: Existing task-state authority and ArtifactStore owner.
    @param arguments: One schema-checked tool request.
    @returns: A read-only preview/status or durable terminal receipt.
    """

    if not isinstance(config, WorkbenchConfig):
        raise TypeError("config must be a WorkbenchConfig")
    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    request = _arguments(arguments)
    operation = request["op"]
    if operation == "status":
        return _status(store, request["task_id"], request["request_id"])
    if operation == "preview":
        existing = _receipt(store, request["task_id"], request["request_id"])
        if existing is not None:
            _assert_same_request(existing, request)
            return existing
        return _preview(_preflight(config, store, request))
    if operation == "cancel":
        return _cancel(config, store, request)
    if operation == "reconcile":
        return _reconcile(config, store, request)
    assert operation == "apply"
    existing = _receipt(store, request["task_id"], request["request_id"])
    if existing is not None:
        _assert_same_request(existing, request)
        return existing
    _require_authority_reservation(
        store, request["request_id"], request["task_id"], label="apply"
    )
    preflight = _preflight(config, store, request)
    if preflight["fingerprint"] != request["expected_fingerprint"]:
        raise LockfileHandoffError(
            "lockfile handoff preview fingerprint is stale; obtain a fresh preview"
        )
    reservation = _reserve(store, preflight)
    return _apply_reserved(config, store, request, reservation)


def active_lockfile_handoff(connection: Any, repository: str) -> bool:
    """Return whether journal events actively reserve a repository lockfile.

    This helper intentionally does no filesystem work: scheduler admission
    calls it while holding the store transaction.  ``repository`` may be an
    original contract string or the pre-resolved Git-common-dir identity used
    by the scheduler.  Reservations persist both spellings so linked
    worktrees remain mutually exclusive without resolving paths here.

    @param connection: Caller-owned SQLite connection.
    @param repository: Persisted repository identity or original contract path.
    @returns: Whether at least one latest handoff event remains reserved.
    """

    if not isinstance(repository, str) or not repository:
        raise ValueError("lockfile handoff repository identity must be a non-empty string")
    try:
        rows = _repository_handoff_rows(connection, repository, repository)
    except LockfileHandoffError:
        raise
    except Exception as error:
        raise LockfileHandoffError(
            "cannot inspect lockfile ownership journal: " + str(error)
        ) from error
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as error:
            raise LockfileHandoffError(
                "cannot inspect lockfile ownership journal: malformed event payload"
            ) from error
        if not isinstance(payload, dict):
            raise LockfileHandoffError(
                "cannot inspect lockfile ownership journal: event payload is not an object"
            )
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise LockfileHandoffError(
                "cannot inspect lockfile ownership journal: event lacks request_id"
            )
        latest[request_id] = payload
    for payload in latest.values():
        state = payload.get("state")
        if state == "reserved":
            if payload.get("lease_state") != "active":
                raise LockfileHandoffError(
                    "cannot inspect lockfile ownership journal: reserved event lacks an active lease"
                )
        elif state in _TERMINAL_STATES:
            if payload.get("lease_state") != "released":
                raise LockfileHandoffError(
                    "cannot inspect lockfile ownership journal: terminal event lacks a released lease"
                )
        else:
            raise LockfileHandoffError(
                "cannot inspect lockfile ownership journal: event has an unknown lease state"
            )
        if state != "reserved":
            continue
        if payload.get("repository") == repository or payload.get("repository_identity") == repository:
            return True
    return False


def _repository_handoff_rows(
    connection: Any,
    repository_identity: str,
    repository: str,
) -> list[Any]:
    """Select only one repository's handoff events without filesystem access.

    The first bounded health probe makes malformed handoff JSON an explicit
    authority failure instead of silently treating it as an expired lease.
    The second query uses persisted physical and original identities so a
    scheduler candidate need not decode other repositories' event payloads.
    """

    invalid = connection.execute(
        "SELECT 1 FROM events WHERE event_type IN (?, ?, ?, ?, ?) "
        "AND json_valid(payload_json) = 0 LIMIT 1",
        _EVENT_TYPES,
    ).fetchone()
    if invalid is not None:
        raise LockfileHandoffError("cannot inspect lockfile ownership journal: malformed event payload")
    return connection.execute(
        "SELECT cursor, payload_json FROM events WHERE event_type IN (?, ?, ?, ?, ?) "
        "AND ("
        "CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.repository_identity') END = ? "
        "OR CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.repository') END = ?"
        ") ORDER BY cursor",
        (*_EVENT_TYPES, repository_identity, repository),
    ).fetchall()


def get_ready_lockfile_handoffs(
    store: WorkbenchStore,
    task_id: str,
) -> list[dict[str, Any]]:
    """Read fully validated ready overlays for a task without replaying work.

    The integration layer uses this projection to attach an overlay only after
    it has reconstructed the exact ordered accepted-ancestor closure.

    @param store: Existing task-state authority.
    @param task_id: Task whose completed handoffs are requested.
    @returns: Ready receipts in journal order, each with its ready event cursor.
    """

    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    normalized_task_id = _text(task_id, "task_id")
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT cursor, payload_json FROM events WHERE task_id = ? "
            "AND event_type IN (?, ?, ?, ?, ?) ORDER BY cursor",
            (normalized_task_id, *_EVENT_TYPES),
        ).fetchall()
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        ready: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            payload = _event_payload(row)
            request_id = _text(payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID)
            cursor = int(row["cursor"])
            latest[request_id] = (cursor, payload)
            if payload.get("state") == "ready":
                ready.append((cursor, payload))
    projected: list[dict[str, Any]] = []
    for cursor, payload in ready:
        request_id = _text(payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID)
        current = latest.get(request_id)
        if current is None or current[0] != cursor or current[1].get("state") != "ready":
            continue
        receipt = _ready_receipt(payload, cursor)
        _verify_ready_artifacts(store, receipt)
        projected.append(_overlay_projection(receipt))
    return projected


def _arguments(raw: object) -> dict[str, Any]:
    """Normalize one operation before any task, filesystem, or journal read."""

    if not isinstance(raw, Mapping):
        raise ValueError("lockfile handoff arguments must be an object")
    allowed = set(TOOL["inputSchema"]["properties"])
    if set(raw) - allowed:
        raise ValueError("lockfile handoff accepts only explicit operation fields")
    op = raw.get("op")
    if op not in {"preview", "apply", "status", "reconcile", "cancel"}:
        raise ValueError("lockfile handoff op is unsupported")
    result: dict[str, Any] = {
        "op": op,
        "task_id": _text(raw.get("task_id"), "task_id"),
        "request_id": _text(raw.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID),
    }
    if op in {"preview", "apply"}:
        required = {
            "node_id",
            "expected_revision",
            "expected_attempt",
            "expected_contract_hash",
        }
        if not required.issubset(raw):
            raise ValueError("lockfile handoff preview/apply requires task, node, revision, attempt and contract hash")
        result.update(
            {
                "node_id": _text(raw.get("node_id"), "node_id"),
                "expected_revision": _positive_int(raw.get("expected_revision"), "expected_revision"),
                "expected_attempt": _positive_int(raw.get("expected_attempt"), "expected_attempt"),
                "expected_contract_hash": _digest(
                    raw.get("expected_contract_hash"), "expected_contract_hash"
                ),
            }
        )
        if op == "apply":
            result["expected_fingerprint"] = _digest(
                raw.get("expected_fingerprint"), "expected_fingerprint"
            )
        elif "expected_fingerprint" in raw:
            raise ValueError("expected_fingerprint is valid only for lockfile handoff apply")
        if "operation_id" in raw:
            raise ValueError("operation_id is valid only for lockfile handoff reconcile or cancel")
    elif op in {"reconcile", "cancel"}:
        result["operation_id"] = _text(
            raw.get("operation_id"), "operation_id", maximum=_MAX_REQUEST_ID
        )
        forbidden = {"node_id", "expected_revision", "expected_attempt", "expected_contract_hash", "expected_fingerprint"}
        if forbidden.intersection(raw):
            raise ValueError("lockfile handoff reconcile/cancel accepts only the target and operation journal ids")
    else:
        forbidden = {"node_id", "expected_revision", "expected_attempt", "expected_contract_hash", "expected_fingerprint", "operation_id"}
        if forbidden.intersection(raw):
            raise ValueError("lockfile handoff status accepts only task_id and request_id")
    return result


def _preflight(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    request: Mapping[str, Any],
    *,
    allow_reserved_request_id: str | None = None,
) -> dict[str, Any]:
    """Read all durable and filesystem bindings without acquiring a write lock."""

    with store.connection() as connection:
        durable = _durable_snapshot(connection, request)
    source = _source_snapshot(config, store, durable)
    repository_identity = _repository_identity(durable["repository"])
    if not isinstance(repository_identity, str) or not repository_identity:
        raise LockfileHandoffError("lockfile handoff repository identity is unavailable")
    active_rows = _active_lockfile_owner_rows(store)
    foreign = [
        row
        for row in active_rows
        if row["task_id"] != request["task_id"]
        and row["repository_identity"] == repository_identity
    ]
    if foreign:
        owner = foreign[0]
        raise LockfileHandoffError(
            "lockfile handoff is fenced by active lockfile owner "
            + owner["task_id"]
            + ":"
            + owner["node_id"]
        )
    with store.connection() as connection:
        after = _durable_snapshot(connection, request)
        _assert_no_other_active_handoff(
            connection,
            repository_identity,
            durable["repository"],
            allow_reserved_request_id=allow_reserved_request_id,
        )
    if canonical_json(after) != canonical_json(durable):
        raise LockfileHandoffError("lockfile handoff durable binding changed during preview")
    active_after = _active_lockfile_owner_rows(store)
    if canonical_json(active_after) != canonical_json(active_rows):
        raise LockfileHandoffError("lockfile owner activity changed during preview")
    fingerprint_payload = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "request": _business_request(request),
        "durable_fingerprint": canonical_hash(durable),
        "repository_identity": repository_identity,
        "source_fingerprint": source["source_fingerprint"],
        "source_patch_sha256": source["source_patch_sha256"],
        "accepted_input_lineage": source["accepted_input_lineage"],
        "input_fingerprints": source["input_fingerprints"],
        "manifest_fingerprint": source["manifest_fingerprint"],
        "lockfile": source["lockfile"],
        "changed_importers": source["changed_importers"],
    }
    return {
        "request": dict(request),
        "durable": durable,
        "durable_fingerprint": canonical_hash(durable),
        "source": source,
        "repository_identity": repository_identity,
        "active_owner_rows": active_rows,
        "repair_worktree": _repair_worktree_path(request, config.state_root),
        "fingerprint": canonical_hash(fingerprint_payload),
    }


def _durable_snapshot(connection: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable SQL-only binding that later terminal CAS repeats."""

    task_id = request["task_id"]
    node_id = request["node_id"]
    task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise KeyError(task_id)
    if task["state"] != "blocked":
        raise LockfileHandoffError("lockfile handoff requires a blocked task")
    if int(task["state_revision"]) != request["expected_revision"]:
        raise LockfileHandoffError("lockfile handoff task revision changed")
    if task["contract_hash"] != request["expected_contract_hash"]:
        raise LockfileHandoffError("lockfile handoff task contract hash changed")
    contract = _json_object(task["contract_json"], "lockfile handoff task contract")
    repository = _text(contract.get("repository"), "task repository")
    base_sha = _text(contract.get("base_sha"), "task base_sha")
    allowed_scope = _scope_list(contract.get("allowed_scope"), "task allowed_scope")
    forbidden_scope = _scope_list(contract.get("forbidden_scope", ()), "task forbidden_scope")
    if not scope_allows(_LOCKFILE, list(allowed_scope), list(forbidden_scope)):
        raise LockfileHandoffError("pnpm-lock.yaml is outside the task allowed scope")

    rows = connection.execute(
        "SELECT * FROM nodes WHERE task_id = ? ORDER BY node_id", (task_id,)
    ).fetchall()
    if not rows:
        raise LockfileHandoffError("lockfile handoff task has no nodes")
    nodes: dict[str, dict[str, Any]] = {}
    owners: list[str] = []
    active: list[str] = []
    for row in rows:
        spec = _json_object(row["spec_json"], "lockfile handoff node specification")
        candidate_id = _text(spec.get("node_id"), "node_id")
        if candidate_id != row["node_id"]:
            raise LockfileHandoffError("lockfile handoff node id disagrees with its specification")
        write_scopes = _scope_list(spec.get("write_scopes", ()), "node write_scopes")
        verifier = spec.get("verifier") is True
        if not verifier and scope_allows(_LOCKFILE, list(write_scopes), []):
            owners.append(candidate_id)
        if row["state"] in {"running", "indeterminate"}:
            active.append(candidate_id + ":" + str(row["state"]))
        nodes[candidate_id] = {
            "node_id": candidate_id,
            "spec_json": str(row["spec_json"]),
            "state": str(row["state"]),
            "attempt": int(row["attempt"]),
            "worktree": row["worktree"],
            "result_json": row["result_json"],
            "recovery_json": row["recovery_json"],
            "coordinator_epoch": int(row["coordinator_epoch"]),
            "lease_epoch": int(row["lease_epoch"]),
            "spec": spec,
        }
    if active:
        raise LockfileHandoffError(
            "lockfile handoff requires no running or indeterminate task nodes: " + ", ".join(active)
        )
    if len(owners) != 1:
        raise LockfileHandoffError(
            "lockfile handoff requires exactly one non-verifier pnpm-lock.yaml owner"
        )
    blocked = nodes.get(node_id)
    if blocked is None:
        raise KeyError((task_id, node_id))
    if blocked["state"] != "blocked":
        raise LockfileHandoffError("lockfile handoff requires a blocked node")
    if blocked["attempt"] != request["expected_attempt"]:
        raise LockfileHandoffError("lockfile handoff node attempt changed")
    if blocked["spec"].get("verifier") is True:
        raise LockfileHandoffError("lockfile handoff cannot use a verifier node")
    if node_id == owners[0]:
        raise LockfileHandoffError("blocked node already owns pnpm-lock.yaml; no handoff is needed")
    if blocked["recovery_json"] is not None:
        raise LockfileHandoffError("blocked node already has a recovery binding")
    historical_result = _json_object(blocked["result_json"], "blocked node result")
    if historical_result.get("status") != "blocked":
        raise LockfileHandoffError("blocked node result is not a blocked receipt")
    artifacts = historical_result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise LockfileHandoffError("blocked node result has invalid artifacts")
    dependency_input_ref = artifacts.get("dependency-input")
    if dependency_input_ref is not None:
        dependency_input_ref = _text(dependency_input_ref, "dependency-input artifact ref")

    pending = connection.execute(
        "SELECT approval_id FROM approvals WHERE task_id = ? AND decision IS NULL ORDER BY approval_id LIMIT 1",
        (task_id,),
    ).fetchone()
    if pending is not None:
        raise LockfileHandoffError("lockfile handoff is fenced by a pending approval")
    WorkbenchStore.assert_no_active_validation(connection, task_id)
    allocation = connection.execute(
        "SELECT * FROM worktree_allocations WHERE task_id = ? AND node_id = ? AND attempt = ?",
        (task_id, node_id, blocked["attempt"]),
    ).fetchone()
    if allocation is None:
        raise LockfileHandoffError("blocked node has no durable worktree allocation")
    branch = WorktreeManager.branch_name(task_id, node_id, blocked["attempt"])
    if (
        allocation["state"] != "active"
        or allocation["repository"] != repository
        or allocation["base_sha"] != base_sha
        or allocation["branch"] != branch
        or allocation["current_path"] != blocked["worktree"]
        or int(allocation["attempt"]) != blocked["attempt"]
    ):
        raise LockfileHandoffError("blocked node worktree allocation no longer matches its durable binding")
    if not isinstance(blocked["worktree"], str) or not blocked["worktree"]:
        raise LockfileHandoffError("blocked node worktree is unavailable")

    active_owner_rows = _running_lock_owner_rows(connection)
    task_snapshot_nodes = [
        {
            **entry["spec"],
            "state": entry["state"],
            "attempt": entry["attempt"],
            "result": (
                _json_object(entry["result_json"], "node result")
                if entry["result_json"] is not None
                else None
            ),
        }
        for entry in nodes.values()
    ]
    return {
        "task_id": task_id,
        "task_state": str(task["state"]),
        "task_revision": int(task["state_revision"]),
        "contract_hash": str(task["contract_hash"]),
        "contract_json": str(task["contract_json"]),
        "repository": repository,
        "base_sha": base_sha,
        "allowed_scope": list(allowed_scope),
        "forbidden_scope": list(forbidden_scope),
        "blocked_node_id": node_id,
        "blocked_attempt": blocked["attempt"],
        "blocked_spec_json": blocked["spec_json"],
        "blocked_result_json": str(blocked["result_json"]),
        "blocked_recovery_json": blocked["recovery_json"],
        "blocked_worktree": str(blocked["worktree"]),
        "blocked_write_scopes": list(_scope_list(blocked["spec"].get("write_scopes", ()), "node write_scopes")),
        "blocked_depends_on": list(_string_list(blocked["spec"].get("depends_on", ()), "node depends_on")),
        "historical_result_sha256": sha256(str(blocked["result_json"]).encode("utf-8")).hexdigest(),
        "dependency_input_ref": dependency_input_ref,
        "original_owner": owners[0],
        "temporary_owner": _temporary_owner(task_id, node_id, blocked["attempt"]),
        "source_allocation": {
            "allocation_id": str(allocation["allocation_id"]),
            "worktree": str(allocation["current_path"]),
            "branch": str(allocation["branch"]),
            "base_sha": str(allocation["base_sha"]),
            "repository": str(allocation["repository"]),
            "attempt": int(allocation["attempt"]),
        },
        "task_snapshot": {
            "task_id": task_id,
            "contract": contract,
            "nodes": task_snapshot_nodes,
        },
        "active_lock_owner_rows": active_owner_rows,
    }


def _source_snapshot(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    durable: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the replayable input and source delta without copying private files."""

    source = _validate_source_worktree(config, durable)
    try:
        assert_recovery_source_idle(source)
    except RecoveryProcessError as error:
        raise LockfileHandoffError(
            "lockfile handoff cannot prove the blocked source worktree is idle: " + str(error)
        ) from error
    dependency_input = _dependency_input(store, durable, source)
    lineage = _lineage(dependency_input, durable["dependency_input_ref"])
    source_delta = _source_delta(source, durable, dependency_input)
    patch = DirtyWorktreeRecovery.captured_patch(
        source,
        dependency_input.input_tree_sha,
        source_delta.untracked_paths,
    )
    source_delta_after_patch = _source_delta(source, durable, dependency_input)
    if source_delta_after_patch != source_delta:
        raise LockfileHandoffError("blocked source delta changed while its replay patch was captured")
    try:
        assert_recovery_source_idle(source)
    except RecoveryProcessError as error:
        raise LockfileHandoffError(
            "lockfile handoff source worktree became active during inspection: " + str(error)
        ) from error
    plan = _engine_plan(source)
    plan_summary = _plan_summary(plan)
    source_delta_after_plan = _source_delta(source, durable, dependency_input)
    if source_delta_after_plan != source_delta:
        raise LockfileHandoffError("blocked source delta changed while lockfile inputs were inspected")
    if plan_summary["before_sha256"] != _file_sha256(source / _LOCKFILE):
        raise LockfileHandoffError("lockfile importer plan does not bind the live source lockfile")
    source_fingerprint = canonical_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": "lockfile-handoff-source-v1",
            "source_allocation": durable["source_allocation"],
            "input_tree_sha": dependency_input.input_tree_sha,
            "after_ancestors": lineage["ancestors"],
            "source_delta": _delta_payload(source_delta),
            "source_patch_sha256": sha256(patch).hexdigest(),
            "source_head": _git_text(source, "rev-parse", "HEAD"),
        }
    )
    return {
        "source": source,
        "dependency_input": dependency_input,
        "accepted_input_lineage": lineage,
        "source_delta": source_delta,
        "source_patch": patch,
        "source_patch_sha256": sha256(patch).hexdigest(),
        "source_fingerprint": source_fingerprint,
        "plan": plan,
        "input_fingerprints": {
            "input_files": plan_summary["input_files"],
            "manifest_files": plan_summary["manifest_files"],
            "workspace_entries": plan_summary["workspace_entries"],
            "engine_fingerprint": plan_summary["fingerprint"],
        },
        "manifest_fingerprint": canonical_hash(plan_summary["manifest_files"]),
        "lockfile": {
            "path": _LOCKFILE,
            "before_sha256": plan_summary["before_sha256"],
            "after_sha256": plan_summary["after_sha256"],
        },
        "changed_importers": plan_summary["changed_importers"],
    }


def _dependency_input(
    store: WorkbenchStore,
    durable: Mapping[str, Any],
    source: Path,
) -> DependencyInput:
    """Load the recorded accepted closure, or an explicit base-only closure."""

    ref = durable["dependency_input_ref"]
    try:
        if ref is not None:
            dependency_input = load_recorded_dependency_input(
                store.artifacts,
                ref,
                task_id=durable["task_id"],
                node_id=durable["blocked_node_id"],
                base_sha=durable["base_sha"],
            )
            validate_dependency_input_lineage(
                durable["task_snapshot"],
                durable["blocked_node_id"],
                dependency_input,
                artifacts=store.artifacts,
            )
            return dependency_input
        if durable["blocked_depends_on"]:
            raise LockfileHandoffError(
                "blocked dependent node lacks its recorded dependency-input artifact"
            )
        return base_dependency_input(
            task_id=durable["task_id"],
            node_id=durable["blocked_node_id"],
            base_sha=durable["base_sha"],
            worktree=source,
        )
    except (DependencyInputError, WorktreeError, ValueError) as error:
        raise LockfileHandoffError("lockfile handoff dependency input is invalid: " + str(error)) from error


def _source_delta(
    source: Path,
    durable: Mapping[str, Any],
    dependency_input: DependencyInput,
) -> SourceDelta:
    """Read one exact original-scope delta; lockfile changes remain forbidden."""

    allowed = list(durable["allowed_scope"])
    forbidden = list(durable["forbidden_scope"])
    write_scopes = list(durable["blocked_write_scopes"])

    def validate(paths: tuple[str, ...]) -> None:
        for path in paths:
            if path == _LOCKFILE:
                raise LockfileHandoffError(
                    "blocked source already changes pnpm-lock.yaml outside its original owner scope"
                )
            if not scope_allows(path, allowed, forbidden):
                raise LockfileHandoffError("blocked source path is outside task scope: " + path)
            if not scope_allows(path, write_scopes, []):
                raise LockfileHandoffError("blocked source path is outside its original node scope: " + path)

    try:
        return inspect_recovery_source_delta(
            source, dependency_input.input_tree_sha, validate_paths=validate
        )
    except (DirtyWorktreeRecoveryError, ValueError) as error:
        raise LockfileHandoffError("lockfile handoff source delta is invalid: " + str(error)) from error


def _lineage(dependency_input: DependencyInput, ref: str | None) -> dict[str, Any]:
    """Return the exact dependency receipt without substituting later ancestors."""

    receipt = _json_safe_object(dependency_input.receipt, "dependency input receipt")
    ancestors = receipt.get("ancestors")
    if not isinstance(ancestors, list):
        raise LockfileHandoffError("dependency input receipt ancestors are invalid")
    normalized: list[dict[str, Any]] = []
    for ancestor in ancestors:
        if not isinstance(ancestor, Mapping) or set(ancestor) != {"node_id", "attempt", "patch_ref"}:
            raise LockfileHandoffError("dependency input receipt ancestor is invalid")
        normalized.append(
            {
                "node_id": _text(ancestor.get("node_id"), "ancestor node_id"),
                "attempt": _positive_int(ancestor.get("attempt"), "ancestor attempt"),
                "patch_ref": (
                    _text(ancestor.get("patch_ref"), "ancestor patch_ref")
                    if ancestor.get("patch_ref") is not None
                    else None
                ),
            }
        )
    if ref is not None:
        ref = _text(ref, "dependency input ref")
    return {
        "ref": ref,
        "receipt": receipt,
        "input_tree_sha": _text(dependency_input.input_tree_sha, "input_tree_sha"),
        "ancestors": normalized,
    }


def _plan_summary(raw: object) -> dict[str, Any]:
    """Validate engine output while deliberately excluding raw lockfile bytes/text."""

    if not isinstance(raw, Mapping):
        raise LockfileHandoffError("lockfile importer plan is not an object")
    required = {
        "schema_version",
        "lockfile",
        "input_files",
        "manifest_files",
        "before_sha256",
        "after_sha256",
        "new_lockfile",
        "changed_importers",
        "workspace_entries",
        "fingerprint",
        "before_lockfile",
    }
    missing = required - set(raw)
    if missing:
        raise LockfileHandoffError("lockfile importer plan is missing required fields")
    if raw.get("lockfile") != _LOCKFILE:
        raise LockfileHandoffError("lockfile importer plan targets an unexpected path")
    before = raw.get("before_lockfile")
    new = raw.get("new_lockfile")
    if not isinstance(before, bytes) or not isinstance(new, str):
        raise LockfileHandoffError("lockfile importer plan has invalid lockfile bytes")
    before_sha256 = _digest(raw.get("before_sha256"), "plan before_sha256")
    after_sha256 = _digest(raw.get("after_sha256"), "plan after_sha256")
    if sha256(before).hexdigest() != before_sha256:
        raise LockfileHandoffError("lockfile importer plan before_sha256 does not match its bytes")
    if sha256(new.encode("utf-8")).hexdigest() != after_sha256:
        raise LockfileHandoffError("lockfile importer plan after_sha256 does not match its text")
    input_files = _file_hash_map(raw.get("input_files"), "plan input_files")
    manifest_files = _file_hash_map(raw.get("manifest_files"), "plan manifest_files")
    if input_files.get(_LOCKFILE) != before_sha256:
        raise LockfileHandoffError("lockfile importer plan input_files does not bind pnpm-lock.yaml")
    if any(not path.endswith("package.json") for path in manifest_files):
        raise LockfileHandoffError("lockfile importer manifest_files contains a non-manifest path")
    if any(input_files.get(path) != digest for path, digest in manifest_files.items()):
        raise LockfileHandoffError("lockfile importer manifest hashes do not match input_files")
    changed_importers = _changed_importers(raw.get("changed_importers"))
    workspace_entries = _workspace_entries(raw.get("workspace_entries"))
    fingerprint = _digest(raw.get("fingerprint"), "plan fingerprint")
    if not changed_importers or before_sha256 == after_sha256:
        raise LockfileHandoffError("lockfile importer plan has no missing workspace importer to repair")
    return {
        "schema_version": _positive_int(raw.get("schema_version"), "plan schema_version"),
        "lockfile": _LOCKFILE,
        "input_files": input_files,
        "manifest_files": manifest_files,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "changed_importers": changed_importers,
        "workspace_entries": workspace_entries,
        "fingerprint": fingerprint,
    }


def _preview(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Render a bounded, raw-lockfile-free preview."""

    request = preflight["request"]
    durable = preflight["durable"]
    source = preflight["source"]
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "state": "preview",
        "ok": True,
        "task_id": request["task_id"],
        "request_id": request["request_id"],
        "request_fingerprint": _request_fingerprint(request),
        "fingerprint": preflight["fingerprint"],
        "task_revision": durable["task_revision"],
        "contract_hash": durable["contract_hash"],
        "blocked_node_id": durable["blocked_node_id"],
        "root_node_id": durable["blocked_node_id"],
        "blocked_attempt": durable["blocked_attempt"],
        "repository": durable["repository"],
        "repository_identity": preflight["repository_identity"],
        "original_owner": durable["original_owner"],
        "temporary_owner": durable["temporary_owner"],
        "source_allocation": dict(durable["source_allocation"]),
        "repair_worktree": str(preflight["repair_worktree"]),
        "source_fingerprint": source["source_fingerprint"],
        "accepted_input_lineage": _public_lineage(source["accepted_input_lineage"]),
        "after_ancestors": list(source["accepted_input_lineage"]["ancestors"]),
        "input_fingerprint_summary": _input_fingerprint_summary(source["input_fingerprints"]),
        "manifest_fingerprint": source["manifest_fingerprint"],
        "lockfile": source["lockfile"],
        "changed_importer_count": len(source["changed_importers"]),
        "changed_importers": [item["importer"] for item in source["changed_importers"][:32]],
        "changed_importers_truncated": len(source["changed_importers"]) > 32,
        "changed_importers_fingerprint": canonical_hash(source["changed_importers"]),
        "creates_attempt": False,
        "queues_task": False,
        "accepts_task": False,
        "historical_result_unchanged": True,
    }


def _reserve(store: WorkbenchStore, preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Persist the intent and active temporary-owner lease before any write."""

    request = preflight["request"]
    durable = preflight["durable"]
    source = preflight["source"]
    lease_token = canonical_hash(
        {
            "request_id": request["request_id"],
            "request_fingerprint": _request_fingerprint(request),
            "preview_fingerprint": preflight["fingerprint"],
        }
    )
    repair_worktree = _repair_worktree_path(request, store.path.parent)
    with store.transaction() as connection:
        existing = _receipt_from_connection(connection, request["task_id"], request["request_id"])
        if existing is not None:
            _assert_same_request(existing, request)
            return existing
        current = _durable_snapshot(connection, request)
        if canonical_json(current) != canonical_json(durable):
            raise LockfileHandoffError("lockfile handoff durable binding changed before reservation")
        _assert_no_other_active_handoff(
            connection,
            preflight["repository_identity"],
            durable["repository"],
            allow_reserved_request_id=None,
        )
        current_active_rows = _running_lock_owner_rows(connection)
        if canonical_json(current_active_rows) != canonical_json(durable["active_lock_owner_rows"]):
            raise LockfileHandoffError("lockfile owner activity changed before reservation")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "state": "reserved",
            "lease_state": "active",
            "request_id": request["request_id"],
            "request_fingerprint": _request_fingerprint(request),
            "preview_fingerprint": preflight["fingerprint"],
            "lease_token": lease_token,
            "task_id": request["task_id"],
            "task_revision": durable["task_revision"],
            "contract_hash": durable["contract_hash"],
            "blocked_node_id": durable["blocked_node_id"],
            "root_node_id": durable["blocked_node_id"],
            "blocked_attempt": durable["blocked_attempt"],
            "repository": durable["repository"],
            "repository_identity": preflight["repository_identity"],
            "original_owner": durable["original_owner"],
            "temporary_owner": durable["temporary_owner"],
            "source_allocation": dict(durable["source_allocation"]),
            "repair_worktree": str(repair_worktree),
            "durable_fingerprint": preflight["durable_fingerprint"],
            "source_fingerprint": source["source_fingerprint"],
            "source_patch_sha256": source["source_patch_sha256"],
            "accepted_input_lineage": source["accepted_input_lineage"],
            "after_ancestors": list(source["accepted_input_lineage"]["ancestors"]),
            "input_fingerprints": source["input_fingerprints"],
            "manifest_fingerprint": source["manifest_fingerprint"],
            "lockfile": source["lockfile"],
            "changed_importers": source["changed_importers"],
            "historical_result_sha256": durable["historical_result_sha256"],
        }
        cursor = store._event(
            connection,
            _RESERVED_EVENT,
            request["task_id"],
            durable["blocked_node_id"],
            payload,
            created_at=now_iso(),
        )
    return {**payload, "reservation_event_cursor": cursor}


def _apply_reserved(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    request: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    """Perform all isolated writes after reservation, then atomically publish or fence them."""

    try:
        _reserved_payload(reservation)
        refreshed = _preflight(
            config, store, request, allow_reserved_request_id=request["request_id"]
        )
        _assert_reserved_preflight(reservation, refreshed)
        artifacts = _perform_repair(config, store, request, reservation, refreshed)
        # The final external read proves that no source/input binding drifted
        # while the temporary worktree was generated and frozen-validated.
        final_preflight = _preflight(
            config, store, request, allow_reserved_request_id=request["request_id"]
        )
        _assert_reserved_preflight(reservation, final_preflight)
        return _publish_ready(store, request, reservation, final_preflight, artifacts)
    except LockfileHandoffError as error:
        return _settle_terminal(store, request, reservation, "failed", str(error))
    except (DependencyInputError, DirtyWorktreeRecoveryError, WorktreeError, OSError, ValueError, subprocess.SubprocessError) as error:
        return _settle_terminal(store, request, reservation, "failed", str(error))
    except Exception as error:
        # Preserve a concrete failure receipt for ordinary runtime errors. A
        # process crash before this point remains an Authority ``unknown`` and
        # must be reconciled explicitly; this branch never retries it.
        return _settle_terminal(
            store,
            request,
            reservation,
            "failed",
            f"lockfile handoff external operation failed: {type(error).__name__}: {error}",
        )


def _perform_repair(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    request: Mapping[str, Any],
    reservation: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct, repair, and frozen-validate the isolated repair worktree."""

    durable = preflight["durable"]
    source = preflight["source"]
    repair = Path(str(reservation["repair_worktree"])).expanduser()
    if repair.exists() or repair.is_symlink():
        raise LockfileHandoffError(
            "reserved lockfile handoff repair worktree already exists; reconcile it instead of replaying"
        )
    source_patch = source["source_patch"]
    if sha256(source_patch).hexdigest() != reservation["source_patch_sha256"]:
        raise LockfileHandoffError("blocked source patch changed after reservation")
    source_patch_ref = store.artifacts.put_bytes(source_patch, "lockfile-handoff-source.patch")
    before_lock = source["plan"].get("before_lockfile")
    if not isinstance(before_lock, bytes):
        raise LockfileHandoffError("lockfile importer plan lost its source lockfile bytes")
    before_ref = store.artifacts.put_bytes(before_lock, "pnpm-lock.yaml.before")
    _create_repair_worktree(repair, durable["repository"], durable["base_sha"])
    manager = WorktreeManager(config.state_root / "worktrees")
    _restore_input_and_delta(
        store,
        manager,
        repair,
        durable,
        source["dependency_input"],
        source["accepted_input_lineage"],
        source["source_delta"],
        source_patch_ref,
    )
    target_plan = _engine_plan(repair)
    if canonical_json(_plan_summary(target_plan)) != canonical_json(_plan_summary(source["plan"])):
        raise LockfileHandoffError("isolated repair worktree does not reproduce the previewed lockfile inputs")
    if (repair / _LOCKFILE).read_bytes() != before_lock:
        raise LockfileHandoffError("isolated repair lockfile differs from the previewed source lockfile")
    engine_receipt = _engine_apply(repair, target_plan)
    after_bytes = (repair / _LOCKFILE).read_bytes()
    expected_after = reservation["lockfile"]["after_sha256"]
    if sha256(after_bytes).hexdigest() != expected_after:
        raise LockfileHandoffError("lockfile importer apply did not produce the previewed lockfile bytes")
    _assert_repair_delta(
        repair,
        source["dependency_input"].input_tree_sha,
        source["source_delta"],
        expected_after,
    )
    frozen = _frozen_validation(config, repair, expected_after)
    _assert_repair_delta(
        repair,
        source["dependency_input"].input_tree_sha,
        source["source_delta"],
        expected_after,
    )
    repaired_ref = store.artifacts.put_bytes(after_bytes, "pnpm-lock.yaml")
    return {
        "source_patch_ref": source_patch_ref,
        "before_artifact_ref": before_ref,
        "artifact_ref": repaired_ref,
        "sha256": expected_after,
        "frozen_validation": frozen,
        "engine_apply": _engine_receipt(engine_receipt),
    }


def _restore_input_and_delta(
    store: WorkbenchStore,
    manager: WorktreeManager,
    repair: Path,
    durable: Mapping[str, Any],
    dependency_input: DependencyInput,
    lineage: Mapping[str, Any],
    source_delta: SourceDelta,
    source_patch_ref: str,
) -> None:
    """Rebuild only accepted input plus the scope-checked source delta."""

    ref = lineage.get("ref")
    if ref is not None:
        restored = apply_recorded_dependency_input(
            store.artifacts,
            manager,
            ref=ref,
            task_id=durable["task_id"],
            node_id=durable["blocked_node_id"],
            base_sha=durable["base_sha"],
            worktree=repair,
        )
        if restored.input_tree_sha != dependency_input.input_tree_sha:
            raise LockfileHandoffError("recorded dependency input tree changed during repair reconstruction")
    else:
        actual_base = _git_text(repair, "rev-parse", f"{durable['base_sha']}^{{tree}}")
        if actual_base != dependency_input.input_tree_sha:
            raise LockfileHandoffError("base-only dependency input does not match the repair worktree")
    patch = store.artifacts.verify(source_patch_ref)
    if patch.read_bytes():
        manager.apply_patch(repair, patch)
    reconstructed = inspect_recovery_source_delta(repair, dependency_input.input_tree_sha)
    if reconstructed != source_delta:
        raise LockfileHandoffError("isolated repair worktree does not reproduce the blocked source delta")


def _assert_repair_delta(
    repair: Path,
    input_tree_sha: str,
    source_delta: SourceDelta,
    expected_lock_sha256: str,
) -> None:
    """Require all reconstructed source files to remain untouched except the lockfile."""

    observed = inspect_recovery_source_delta(repair, input_tree_sha)
    expected_entries = {entry.path: entry.to_dict() for entry in source_delta.entries}
    observed_entries = {entry.path: entry.to_dict() for entry in observed.entries}
    lock_entry = observed_entries.pop(_LOCKFILE, None)
    if lock_entry is None or lock_entry.get("deleted") is True:
        raise LockfileHandoffError("repair worktree no longer contains pnpm-lock.yaml")
    if expected_entries != observed_entries:
        raise LockfileHandoffError("repair operation changed source content outside pnpm-lock.yaml")
    if _file_sha256(repair / _LOCKFILE) != expected_lock_sha256:
        raise LockfileHandoffError("repair worktree lockfile hash changed unexpectedly")


def _frozen_validation(config: WorkbenchConfig, repair: Path, expected_lock_sha256: str) -> dict[str, Any]:
    """Run the existing managed offline frozen pnpm materializer and sanitize evidence."""

    materializer = _pnpm_materializer(config)
    receipt = materializer.materialize(
        repair,
        timeout_seconds=PnpmOfflineMaterializer.MAX_MATERIALIZATION_SECONDS,
    )
    if not isinstance(receipt, Mapping) or receipt.get("kind") != "pnpm-offline-materialization":
        raise LockfileHandoffError("managed pnpm frozen validation did not return a materialization receipt")
    if receipt.get("lockfile_sha256") != expected_lock_sha256:
        raise LockfileHandoffError("managed pnpm frozen validation used another lockfile")
    commands = receipt.get("commands")
    if not isinstance(commands, list) or not commands:
        raise LockfileHandoffError("managed pnpm frozen validation has no command readiness evidence")
    command_exit_codes: list[int] = []
    has_version_probe = False
    has_frozen_install = False
    has_template_reuse = False
    template = receipt.get("template")
    template_state = template.get("state") if isinstance(template, Mapping) else None
    for command in commands:
        if not isinstance(command, Mapping) or type(command.get("exit_code")) is not int:
            raise LockfileHandoffError("managed pnpm frozen validation command receipt is invalid")
        exit_code = int(command["exit_code"])
        if exit_code != 0:
            raise LockfileHandoffError("managed pnpm frozen validation command failed")
        command_exit_codes.append(exit_code)
        argv = command.get("command")
        if not isinstance(argv, list) or not all(isinstance(value, str) and value for value in argv):
            raise LockfileHandoffError("managed pnpm frozen validation command argv is invalid")
        if argv[-1:] == ["--version"]:
            has_version_probe = True
        if "install" in argv:
            required_flags = {"--offline", "--frozen-lockfile", "--ignore-scripts"}
            if required_flags.issubset(set(argv)):
                has_frozen_install = True
        if len(argv) >= 2 and argv[0] == "pnpm-worktree" and argv[1] == "reuse":
            has_template_reuse = True
    if not has_version_probe or not (
        has_frozen_install
        or has_template_reuse
        or template_state in {"reuse", "hit"}
    ):
        raise LockfileHandoffError("managed pnpm frozen validation lacks a fixed frozen readiness command")
    version = receipt.get("pnpm_version")
    if not isinstance(version, str) or not version.strip():
        raise LockfileHandoffError("managed pnpm frozen validation has no resolved pnpm version")
    return {
        "kind": "pnpm-offline-materialization",
        "ready": True,
        "pnpm_version": version.strip(),
        "lockfile_sha256": expected_lock_sha256,
        "command_exit_codes": command_exit_codes,
        "template_state": template_state if isinstance(template_state, str) else None,
    }


def _pnpm_materializer(_config: WorkbenchConfig) -> PnpmOfflineMaterializer:
    """Return the existing Workbench-managed offline/frozen pnpm runtime."""

    return PnpmOfflineMaterializer()


def _publish_ready(
    store: WorkbenchStore,
    request: Mapping[str, Any],
    reservation: Mapping[str, Any],
    preflight: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    """Advance only task revision and atomically attach the completed overlay event."""

    _reserved_payload(reservation)
    durable = preflight["durable"]
    with store.transaction() as connection:
        current = _latest_payload(connection, request["task_id"], request["request_id"])
        if current is None:
            raise LockfileHandoffError("lockfile handoff reservation disappeared before publish")
        if current.get("state") != "reserved" or current.get("lease_token") != reservation.get("lease_token"):
            return _terminal_receipt(current, _latest_cursor(connection, request["task_id"], request["request_id"]))
        current_durable = _durable_snapshot(connection, request)
        if canonical_hash(current_durable) != reservation["durable_fingerprint"]:
            raise LockfileHandoffError("lockfile handoff durable binding changed before publish")
        changed = connection.execute(
            "UPDATE tasks SET state_revision = ?, updated_at = ? "
            "WHERE task_id = ? AND state = 'blocked' AND state_revision = ? AND contract_hash = ?",
            (
                int(durable["task_revision"]) + 1,
                now_iso(),
                request["task_id"],
                durable["task_revision"],
                durable["contract_hash"],
            ),
        ).rowcount
        if changed != 1:
            raise LockfileHandoffError("lockfile handoff task revision compare-and-set failed")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "state": "ready",
            "lease_state": "released",
            "request_id": request["request_id"],
            "request_fingerprint": reservation["request_fingerprint"],
            "preview_fingerprint": reservation["preview_fingerprint"],
            "lease_token": reservation["lease_token"],
            "task_id": request["task_id"],
            "task_revision": int(durable["task_revision"]) + 1,
            "contract_hash": durable["contract_hash"],
            "blocked_node_id": durable["blocked_node_id"],
            "root_node_id": durable["blocked_node_id"],
            "blocked_attempt": durable["blocked_attempt"],
            "repository": reservation["repository"],
            "repository_identity": reservation["repository_identity"],
            "original_owner": reservation["original_owner"],
            "temporary_owner": reservation["temporary_owner"],
            "source_allocation": reservation["source_allocation"],
            "repair_worktree": reservation["repair_worktree"],
            "source_fingerprint": reservation["source_fingerprint"],
            "source_patch_ref": artifacts["source_patch_ref"],
            "source_patch_sha256": reservation["source_patch_sha256"],
            "accepted_input_lineage": reservation["accepted_input_lineage"],
            "after_ancestors": reservation["after_ancestors"],
            "input_fingerprints": reservation["input_fingerprints"],
            "manifest_fingerprint": reservation["manifest_fingerprint"],
            "changed_importers": reservation["changed_importers"],
            "artifact_ref": artifacts["artifact_ref"],
            "sha256": artifacts["sha256"],
            "before_artifact_ref": artifacts["before_artifact_ref"],
            "before_sha256": reservation["lockfile"]["before_sha256"],
            "lockfile": {
                "path": _LOCKFILE,
                "before_artifact_ref": artifacts["before_artifact_ref"],
                "before_sha256": reservation["lockfile"]["before_sha256"],
                "artifact_ref": artifacts["artifact_ref"],
                "sha256": artifacts["sha256"],
            },
            "frozen_validation": artifacts["frozen_validation"],
            "engine_apply": artifacts["engine_apply"],
            "lease_event_cursor": int(reservation["reservation_event_cursor"]),
            "historical_result_unchanged": True,
            "creates_attempt": False,
            "queues_task": False,
            "accepts_task": False,
        }
        cursor = store._event(
            connection,
            _READY_EVENT,
            request["task_id"],
            durable["blocked_node_id"],
            payload,
            created_at=now_iso(),
        )
    return _ready_receipt(payload, cursor)


def _settle_terminal(
    store: WorkbenchStore,
    request: Mapping[str, Any],
    reservation: Mapping[str, Any],
    state: str,
    detail: str,
) -> dict[str, Any]:
    """Release a still-owned reservation without attaching any overlay."""

    if state not in {"failed", "cancelled", "needs_action"}:
        raise ValueError("lockfile handoff terminal state is invalid")
    _reserved_payload(reservation)
    with store.transaction() as connection:
        current = _latest_payload(connection, request["task_id"], request["request_id"])
        if current is None:
            raise LockfileHandoffError("lockfile handoff reservation disappeared before terminal settlement")
        cursor = _latest_cursor(connection, request["task_id"], request["request_id"])
        if current.get("state") != "reserved" or current.get("lease_token") != reservation.get("lease_token"):
            return _terminal_receipt(current, cursor)
        task = connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (request["task_id"],)
        ).fetchone()
        effective_state = "cancelled" if task is not None and task["state"] == "cancelled" else state
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "state": effective_state,
            "lease_state": "released",
            "request_id": request["request_id"],
            "request_fingerprint": reservation["request_fingerprint"],
            "preview_fingerprint": reservation["preview_fingerprint"],
            "lease_token": reservation["lease_token"],
            "task_id": request["task_id"],
            "repository": reservation["repository"],
            "repository_identity": reservation["repository_identity"],
            "blocked_node_id": reservation["blocked_node_id"],
            "blocked_attempt": reservation["blocked_attempt"],
            "repair_worktree": reservation["repair_worktree"],
            "detail": _bounded_detail(detail),
            "historical_result_unchanged": True,
            "overlay_attached": False,
            "external_operation_may_continue": effective_state in {"cancelled", "needs_action"},
        }
        event_type = {
            "failed": _FAILED_EVENT,
            "cancelled": _CANCELLED_EVENT,
            "needs_action": _NEEDS_ACTION_EVENT,
        }[effective_state]
        cursor = store._event(
            connection,
            event_type,
            request["task_id"],
            reservation["blocked_node_id"],
            payload,
            created_at=now_iso(),
        )
    return _terminal_receipt(payload, cursor)


def _cancel(config: WorkbenchConfig, store: WorkbenchStore, request: Mapping[str, Any]) -> dict[str, Any]:
    """Fence a reserved operation without claiming that a child process stopped."""

    del config
    reservation = _reserved_for_mutation(store, request)
    if reservation.get("state") != "reserved":
        return reservation
    _require_authority_reservation(
        store, request["operation_id"], request["task_id"], label="cancel"
    )
    return _settle_terminal(
        store,
        {"task_id": request["task_id"], "request_id": request["request_id"]},
        reservation,
        "cancelled",
        "operator cancelled the handoff; any independently running local operation is fenced off from overlay publication",
    )


def _reconcile(config: WorkbenchConfig, store: WorkbenchStore, request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a reserved restart safely without replaying an unknown external step."""

    del config
    existing = _receipt(store, request["task_id"], request["request_id"])
    if existing is None:
        raise KeyError(request["request_id"])
    if existing.get("state") in _TERMINAL_STATES:
        return existing
    reservation = _reserved_for_mutation(store, request)
    _require_authority_reservation(
        store, request["operation_id"], request["task_id"], label="reconcile"
    )
    # No filesystem action is treated as enough evidence to publish a lost
    # completion: an interrupted run may have changed the private repair tree
    # after frozen validation but before durable CAS.  Keep that tree for
    # inspection and release the scheduler gate explicitly.
    return _settle_terminal(
        store,
        {"task_id": request["task_id"], "request_id": request["request_id"]},
        reservation,
        "needs_action",
        "interrupted reservation has no durable ready receipt; no repair step was replayed and the retained private worktree requires explicit inspection",
    )


def _status(store: WorkbenchStore, task_id: str, request_id: str) -> dict[str, Any]:
    """Render a terminal receipt or a visible non-replayable reserved state."""

    receipt = _receipt(store, task_id, request_id)
    if receipt is None:
        raise KeyError(request_id)
    if receipt.get("state") == "reserved":
        with store.connection() as connection:
            journal = connection.execute(
                "SELECT state FROM authority_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        journal_state = str(journal["state"]) if journal is not None else "missing"
        observation = (
            "authority_request_executing"
            if journal_state == "executing"
            else "authority_request_not_executing"
        )
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "state": "reserved",
            "request_id": request_id,
            "task_id": task_id,
            "detail": "reservation is active or interrupted; status never replays its external repair",
            "repair_worktree": receipt.get("repair_worktree"),
            "authority_request_state": journal_state,
            "observation": observation,
            "reconcile_available": journal_state != "executing",
            "external_operation_may_continue": True,
        }
    return receipt


def _reserved_for_mutation(store: WorkbenchStore, request: Mapping[str, Any]) -> dict[str, Any]:
    """Load exactly one mutable reserved receipt before cancel/reconcile CAS."""

    receipt = _receipt(store, request["task_id"], request["request_id"])
    if receipt is None:
        raise KeyError(request["request_id"])
    if receipt.get("state") != "reserved":
        return receipt
    _reserved_payload(receipt)
    return receipt


def _require_authority_reservation(
    store: WorkbenchStore,
    request_id: str,
    task_id: str,
    *,
    label: str,
) -> None:
    """Require the existing Authority journal to have admitted this mutation."""

    with store.connection() as connection:
        row = connection.execute(
            "SELECT tool, task_id, state FROM authority_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if row is None or (row["tool"], row["task_id"], row["state"]) != (
        TOOL_NAME,
        task_id,
        "executing",
    ):
        raise LockfileHandoffError(
            f"lockfile handoff {label} must run through its reserved Authority service request"
        )


def _assert_reserved_preflight(
    reservation: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> None:
    """Require post-reservation filesystem observations to equal the sealed preview."""

    expected = {
        "durable_fingerprint": preflight["durable_fingerprint"],
        "source_fingerprint": preflight["source"]["source_fingerprint"],
        "source_patch_sha256": preflight["source"]["source_patch_sha256"],
        "accepted_input_lineage": preflight["source"]["accepted_input_lineage"],
        "input_fingerprints": preflight["source"]["input_fingerprints"],
        "manifest_fingerprint": preflight["source"]["manifest_fingerprint"],
        "lockfile": preflight["source"]["lockfile"],
        "changed_importers": preflight["source"]["changed_importers"],
    }
    for key, value in expected.items():
        if canonical_json(reservation.get(key)) != canonical_json(value):
            raise LockfileHandoffError("lockfile handoff binding drifted after reservation: " + key)


def _assert_no_other_active_handoff(
    connection: Any,
    repository_identity: str,
    repository: str,
    *,
    allow_reserved_request_id: str | None,
) -> None:
    """Fence the temporary writer from every active journal reservation."""

    rows = _repository_handoff_rows(connection, repository_identity, repository)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _event_payload(row)
        request_id = _text(payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID)
        latest[request_id] = payload
    for request_id, payload in latest.items():
        if payload.get("state") != "reserved":
            continue
        if request_id == allow_reserved_request_id:
            continue
        if payload.get("repository") in {repository, repository_identity} or payload.get("repository_identity") in {repository, repository_identity}:
            raise LockfileHandoffError("another lockfile handoff reservation is active")


def _active_lockfile_owner_rows(store: WorkbenchStore) -> list[dict[str, Any]]:
    """Resolve active owner repository identities outside any write transaction."""

    with store.connection() as connection:
        rows = _running_lock_owner_rows(connection)
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append({**row, "repository_identity": _repository_identity(row["repository"])})
    return result


def _running_lock_owner_rows(connection: Any) -> list[dict[str, Any]]:
    """Read possible running/indeterminate lockfile writers using only SQLite."""

    rows = connection.execute(
        "SELECT t.task_id, t.contract_json, n.node_id, n.attempt, n.state, n.spec_json "
        "FROM tasks t JOIN nodes n USING(task_id) "
        "WHERE n.state IN ('running', 'indeterminate') ORDER BY t.task_id, n.node_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        contract = _json_object(row["contract_json"], "active task contract")
        spec = _json_object(row["spec_json"], "active node specification")
        if spec.get("verifier") is True:
            continue
        write_scopes = _scope_list(spec.get("write_scopes", ()), "active node write_scopes")
        if not scope_allows(_LOCKFILE, list(write_scopes), []):
            continue
        result.append(
            {
                "task_id": _text(row["task_id"], "active task_id"),
                "node_id": _text(row["node_id"], "active node_id"),
                "attempt": _positive_int(row["attempt"], "active attempt"),
                "state": _text(row["state"], "active node state"),
                "repository": _text(contract.get("repository"), "active repository"),
                "spec_json": str(row["spec_json"]),
            }
        )
    return result


def _create_repair_worktree(repair: Path, repository: str, base_sha: str) -> None:
    """Create a detached, retained worktree at the immutable contract base."""

    repo = Path(repository).expanduser().resolve(strict=True)
    target = repair.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise LockfileHandoffError("lockfile handoff repair worktree path already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), base_sha],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise LockfileHandoffError(
            "cannot create isolated lockfile handoff worktree: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    os.chmod(target.parent, 0o700)


def _validate_source_worktree(config: WorkbenchConfig, durable: Mapping[str, Any]) -> Path:
    """Prove the stored source allocation is an idle worktree of this repository."""

    source = Path(durable["blocked_worktree"]).expanduser().resolve(strict=True)
    expected_root = (config.state_root / "worktrees").expanduser().resolve(strict=False)
    if not source.is_relative_to(expected_root):
        raise LockfileHandoffError("blocked source worktree is outside the Workbench worktree root")
    repository = Path(durable["repository"]).expanduser().resolve(strict=True)
    if _git_text(source, "rev-parse", "--show-toplevel") != str(source):
        raise LockfileHandoffError("blocked source allocation is not a standalone Git worktree")
    source_common = _git_text(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
    repository_common = _git_text(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if source_common != repository_common:
        raise LockfileHandoffError("blocked source allocation belongs to another Git repository")
    expected_branch = durable["source_allocation"]["branch"]
    if _git_text(source, "branch", "--show-current") != expected_branch:
        raise LockfileHandoffError("blocked source allocation branch changed")
    base = _git_text(repository, "rev-parse", f"{durable['base_sha']}^{{commit}}")
    head = _git_text(source, "rev-parse", "HEAD")
    if _git_text(source, "merge-base", base, head) != base:
        raise LockfileHandoffError("blocked source allocation no longer descends from the contract base")
    return source


def _engine_plan(root: Path) -> dict[str, Any]:
    """Import the isolated importer engine only when the operation needs it."""

    try:
        from .lockfile_importers import plan_workspace_importer_repair
    except ImportError as error:
        raise LockfileHandoffError("lockfile importer engine is unavailable") from error
    result = plan_workspace_importer_repair(root)
    if not isinstance(result, dict):
        raise LockfileHandoffError("lockfile importer engine returned a non-object plan")
    return result


def _engine_apply(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Run the fixed importer-only operation in the isolated repair worktree."""

    try:
        from .lockfile_importers import apply_workspace_importer_repair
    except ImportError as error:
        raise LockfileHandoffError("lockfile importer engine is unavailable") from error
    result = apply_workspace_importer_repair(root, dict(plan))
    if not isinstance(result, dict):
        raise LockfileHandoffError("lockfile importer engine returned a non-object apply receipt")
    return result


def _engine_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only JSON-safe, bounded engine evidence in the public receipt."""

    data = _json_safe_object(raw, "lockfile importer apply receipt")
    encoded = canonical_json(data).encode("utf-8")
    if len(encoded) > 32 * 1024:
        return {"sha256": sha256(encoded).hexdigest(), "truncated": True}
    return data


def _receipt(store: WorkbenchStore, task_id: str, request_id: str) -> dict[str, Any] | None:
    """Read one request's latest event without invoking any external operation."""

    with store.connection() as connection:
        return _receipt_from_connection(connection, task_id, request_id)


def _receipt_from_connection(connection: Any, task_id: str, request_id: str) -> dict[str, Any] | None:
    """Project one handoff request from a caller-owned connection."""

    rows = connection.execute(
        "SELECT cursor, payload_json FROM events WHERE task_id = ? "
        "AND event_type IN (?, ?, ?, ?, ?) ORDER BY cursor",
        (task_id, *_EVENT_TYPES),
    ).fetchall()
    matching: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        payload = _event_payload(row)
        if payload.get("request_id") == request_id:
            matching.append((int(row["cursor"]), payload))
    if not matching:
        return None
    cursor, payload = matching[-1]
    if payload.get("state") == "ready":
        return _ready_receipt(payload, cursor)
    if payload.get("state") == "reserved":
        _reserved_payload(payload)
        return {**payload, "reservation_event_cursor": cursor}
    return _terminal_receipt(payload, cursor)


def _latest_payload(connection: Any, task_id: str, request_id: str) -> dict[str, Any] | None:
    """Return latest matching payload without changing the event ledger."""

    rows = connection.execute(
        "SELECT cursor, payload_json FROM events WHERE task_id = ? "
        "AND event_type IN (?, ?, ?, ?, ?) ORDER BY cursor DESC",
        (task_id, *_EVENT_TYPES),
    ).fetchall()
    for row in rows:
        payload = _event_payload(row)
        if payload.get("request_id") == request_id:
            return payload
    return None


def _latest_cursor(connection: Any, task_id: str, request_id: str) -> int:
    """Return the latest matching cursor after a successful latest-payload read."""

    rows = connection.execute(
        "SELECT cursor, payload_json FROM events WHERE task_id = ? "
        "AND event_type IN (?, ?, ?, ?, ?) ORDER BY cursor DESC",
        (task_id, *_EVENT_TYPES),
    ).fetchall()
    for row in rows:
        payload = _event_payload(row)
        if payload.get("request_id") == request_id:
            return int(row["cursor"])
    raise LockfileHandoffError("lockfile handoff event cursor is unavailable")


def _event_payload(row: Any) -> dict[str, Any]:
    """Decode one handoff event or fail closed on malformed durable data."""

    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError) as error:
        raise LockfileHandoffError("lockfile handoff event payload is invalid JSON") from error
    if not isinstance(payload, dict):
        raise LockfileHandoffError("lockfile handoff event payload is not an object")
    return payload


def _ready_receipt(payload: Mapping[str, Any], cursor: int) -> dict[str, Any]:
    """Validate a ready event before allowing any overlay consumer to use it."""

    required = {
        "schema_version", "kind", "state", "lease_state", "request_id", "request_fingerprint",
        "preview_fingerprint", "lease_token", "task_id", "task_revision", "contract_hash",
        "blocked_node_id", "root_node_id", "blocked_attempt", "repository", "repository_identity",
        "original_owner", "temporary_owner", "source_allocation", "repair_worktree", "source_fingerprint",
        "source_patch_ref", "source_patch_sha256", "accepted_input_lineage", "after_ancestors",
        "input_fingerprints", "manifest_fingerprint", "changed_importers", "artifact_ref", "sha256",
        "before_artifact_ref", "before_sha256", "lockfile", "frozen_validation", "engine_apply",
        "lease_event_cursor", "historical_result_unchanged", "creates_attempt", "queues_task",
        "accepts_task",
    }
    if set(payload) != required:
        raise LockfileHandoffError("ready lockfile handoff receipt has an invalid shape")
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("kind") != _KIND:
        raise LockfileHandoffError("ready lockfile handoff receipt has an unsupported schema")
    if payload.get("state") != "ready" or payload.get("lease_state") != "released":
        raise LockfileHandoffError("ready lockfile handoff receipt has an invalid terminal state")
    for field, maximum in (
        ("request_id", _MAX_REQUEST_ID), ("task_id", _MAX_TEXT), ("blocked_node_id", _MAX_TEXT),
        ("root_node_id", _MAX_TEXT), ("repository", _MAX_TEXT * 8), ("repository_identity", _MAX_TEXT * 8),
        ("original_owner", _MAX_TEXT), ("temporary_owner", _MAX_TEXT), ("repair_worktree", _MAX_TEXT * 8),
        ("artifact_ref", _MAX_TEXT), ("before_artifact_ref", _MAX_TEXT), ("source_patch_ref", _MAX_TEXT),
    ):
        _text(payload.get(field), field, maximum=maximum)
    for field in (
        "request_fingerprint", "preview_fingerprint", "lease_token", "contract_hash", "source_fingerprint",
        "source_patch_sha256", "manifest_fingerprint", "sha256", "before_sha256",
    ):
        _digest(payload.get(field), field)
    _positive_int(payload.get("task_revision"), "task_revision")
    _positive_int(payload.get("blocked_attempt"), "blocked_attempt")
    _positive_int(payload.get("lease_event_cursor"), "lease_event_cursor")
    if not isinstance(cursor, int) or cursor < 1:
        raise LockfileHandoffError("ready lockfile handoff cursor is invalid")
    source_allocation = _source_allocation(payload.get("source_allocation"))
    lineage = _ready_lineage(payload.get("accepted_input_lineage"))
    after_ancestors = _ancestor_list(payload.get("after_ancestors"))
    if canonical_json(after_ancestors) != canonical_json(lineage["ancestors"]):
        raise LockfileHandoffError("ready lockfile handoff after_ancestors disagrees with its lineage")
    inputs = _ready_inputs(payload.get("input_fingerprints"))
    if _digest(inputs["engine_fingerprint"], "engine_fingerprint") != inputs["engine_fingerprint"]:
        raise LockfileHandoffError("ready lockfile handoff engine fingerprint is invalid")
    if canonical_hash(inputs["manifest_files"]) != payload["manifest_fingerprint"]:
        raise LockfileHandoffError("ready lockfile handoff manifest fingerprint is invalid")
    lockfile = payload.get("lockfile")
    if not isinstance(lockfile, Mapping) or set(lockfile) != {
        "path", "before_artifact_ref", "before_sha256", "artifact_ref", "sha256"
    }:
        raise LockfileHandoffError("ready lockfile handoff lockfile descriptor is invalid")
    if (
        lockfile.get("path") != _LOCKFILE
        or lockfile.get("artifact_ref") != payload["artifact_ref"]
        or lockfile.get("sha256") != payload["sha256"]
        or lockfile.get("before_artifact_ref") != payload["before_artifact_ref"]
        or lockfile.get("before_sha256") != payload["before_sha256"]
    ):
        raise LockfileHandoffError("ready lockfile handoff lockfile descriptor does not match its top-level refs")
    frozen = payload.get("frozen_validation")
    if not isinstance(frozen, Mapping) or set(frozen) != {
        "kind", "ready", "pnpm_version", "lockfile_sha256", "command_exit_codes", "template_state"
    }:
        raise LockfileHandoffError("ready lockfile handoff frozen validation is invalid")
    if (
        frozen.get("kind") != "pnpm-offline-materialization"
        or frozen.get("ready") is not True
        or frozen.get("lockfile_sha256") != payload["sha256"]
        or not isinstance(frozen.get("pnpm_version"), str)
        or not frozen["pnpm_version"].strip()
        or not isinstance(frozen.get("command_exit_codes"), list)
        or not frozen["command_exit_codes"]
        or any(type(value) is not int or value != 0 for value in frozen["command_exit_codes"])
        or frozen.get("template_state") is not None and not isinstance(frozen.get("template_state"), str)
    ):
        raise LockfileHandoffError("ready lockfile handoff frozen validation is not ready")
    if (
        payload.get("historical_result_unchanged") is not True
        or payload.get("creates_attempt") is not False
        or payload.get("queues_task") is not False
        or payload.get("accepts_task") is not False
    ):
        raise LockfileHandoffError("ready lockfile handoff lifecycle flags are invalid")
    changed_importers = _changed_importers(payload.get("changed_importers"))
    if not isinstance(payload.get("engine_apply"), Mapping):
        raise LockfileHandoffError("ready lockfile handoff importer evidence is invalid")
    return {**dict(payload), "ready_event_cursor": cursor, "source_allocation": source_allocation,
            "accepted_input_lineage": lineage, "after_ancestors": after_ancestors,
            "input_fingerprints": inputs, "changed_importers": changed_importers}


def _overlay_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact immutable descriptor replayed by dependency inputs.

    The event journal intentionally carries lease fencing and retained repair
    diagnostics that do not belong in a worker's reproducible input.  This
    projection is not a lossy shortcut for validation: it copies every field
    needed to bind task, source closure, manifests, lockfile bytes, frozen
    evidence, and durable event order.  The source patch and lease details
    remain auditable in the journal event itself.
    """

    source = receipt["source_allocation"]
    assert isinstance(source, Mapping)
    projection = {
        "schema_version": receipt["schema_version"],
        "kind": receipt["kind"],
        "state": receipt["state"],
        "request_id": receipt["request_id"],
        "request_fingerprint": receipt["request_fingerprint"],
        "preview_fingerprint": receipt["preview_fingerprint"],
        "task_id": receipt["task_id"],
        "task_revision": receipt["task_revision"],
        "contract_hash": receipt["contract_hash"],
        "blocked_node_id": receipt["blocked_node_id"],
        "root_node_id": receipt["root_node_id"],
        "blocked_attempt": receipt["blocked_attempt"],
        "repository": receipt["repository"],
        "original_owner": receipt["original_owner"],
        "temporary_owner": receipt["temporary_owner"],
        "source_allocation": {
            "allocation_id": source["allocation_id"],
            "worktree": source["worktree"],
            "branch": source["branch"],
            "base_sha": source["base_sha"],
        },
        "repair_worktree": receipt["repair_worktree"],
        "source_fingerprint": {
            "sha256": receipt["source_fingerprint"],
            "source_patch_sha256": receipt["source_patch_sha256"],
            "input_tree_sha": receipt["accepted_input_lineage"]["input_tree_sha"],
        },
        "accepted_input_lineage": receipt["accepted_input_lineage"],
        "input_fingerprints": receipt["input_fingerprints"],
        "manifest_fingerprint": receipt["manifest_fingerprint"],
        "lockfile": receipt["lockfile"],
        "changed_importers": receipt["changed_importers"],
        "frozen_validation": receipt["frozen_validation"],
        "lease_event_cursor": receipt["lease_event_cursor"],
        "ready_event_cursor": receipt["ready_event_cursor"],
        "after_ancestors": receipt["after_ancestors"],
    }
    copied = _json_safe_object(projection, "ready lockfile handoff overlay projection")
    return copied


def _ready_lineage(raw: object) -> dict[str, Any]:
    """Validate the durable recorded dependency input carried by a ready overlay."""

    if not isinstance(raw, Mapping) or set(raw) != {"ref", "receipt", "input_tree_sha", "ancestors"}:
        raise LockfileHandoffError("ready lockfile handoff lineage is invalid")
    ref = raw.get("ref")
    if ref is not None:
        _text(ref, "lineage ref")
    receipt = _json_safe_object(raw.get("receipt"), "lineage receipt")
    input_tree_sha = _text(raw.get("input_tree_sha"), "lineage input_tree_sha")
    ancestors = _ancestor_list(raw.get("ancestors"))
    if receipt.get("input_tree_sha") != input_tree_sha or receipt.get("ancestors") != ancestors:
        raise LockfileHandoffError("ready lockfile handoff lineage receipt disagrees with its projection")
    return {"ref": ref, "receipt": receipt, "input_tree_sha": input_tree_sha, "ancestors": ancestors}


def _ready_inputs(raw: object) -> dict[str, Any]:
    """Validate input maps used by the downstream overlay attach gate."""

    if not isinstance(raw, Mapping) or set(raw) != {
        "input_files", "manifest_files", "workspace_entries", "engine_fingerprint"
    }:
        raise LockfileHandoffError("ready lockfile handoff input fingerprints are invalid")
    inputs = _file_hash_map(raw.get("input_files"), "ready input_files")
    manifests = _file_hash_map(raw.get("manifest_files"), "ready manifest_files")
    if inputs.get(_LOCKFILE) is None or any(inputs.get(path) != digest for path, digest in manifests.items()):
        raise LockfileHandoffError("ready lockfile handoff input maps are inconsistent")
    return {
        "input_files": inputs,
        "manifest_files": manifests,
        "workspace_entries": _workspace_entries(raw.get("workspace_entries")),
        "engine_fingerprint": _digest(raw.get("engine_fingerprint"), "ready engine_fingerprint"),
    }


def _source_allocation(raw: object) -> dict[str, Any]:
    """Validate the allocation identity retained in all reserve/ready receipts."""

    if not isinstance(raw, Mapping) or set(raw) != {
        "allocation_id", "worktree", "branch", "base_sha", "repository", "attempt"
    }:
        raise LockfileHandoffError("lockfile handoff source allocation is invalid")
    return {
        "allocation_id": _text(raw.get("allocation_id"), "allocation_id"),
        "worktree": _text(raw.get("worktree"), "allocation worktree", maximum=_MAX_TEXT * 8),
        "branch": _text(raw.get("branch"), "allocation branch"),
        "base_sha": _text(raw.get("base_sha"), "allocation base_sha"),
        "repository": _text(raw.get("repository"), "allocation repository", maximum=_MAX_TEXT * 8),
        "attempt": _positive_int(raw.get("attempt"), "allocation attempt"),
    }


def _ancestor_list(raw: object) -> list[dict[str, Any]]:
    """Normalize exact accepted predecessor records in their durable order."""

    if not isinstance(raw, list):
        raise LockfileHandoffError("lockfile handoff after_ancestors is invalid")
    result: list[dict[str, Any]] = []
    for value in raw:
        if not isinstance(value, Mapping) or set(value) != {"node_id", "attempt", "patch_ref"}:
            raise LockfileHandoffError("lockfile handoff after_ancestor is invalid")
        patch = value.get("patch_ref")
        result.append(
            {
                "node_id": _text(value.get("node_id"), "after_ancestor node_id"),
                "attempt": _positive_int(value.get("attempt"), "after_ancestor attempt"),
                "patch_ref": _text(patch, "after_ancestor patch_ref") if patch is not None else None,
            }
        )
    return result


def _verify_ready_artifacts(store: WorkbenchStore, receipt: Mapping[str, Any]) -> None:
    """Require every content-addressed byte reference to match its stored digest."""

    for reference, digest, label in (
        (receipt["artifact_ref"], receipt["sha256"], "repaired lockfile"),
        (receipt["before_artifact_ref"], receipt["before_sha256"], "base lockfile"),
        (receipt["source_patch_ref"], receipt["source_patch_sha256"], "source patch"),
    ):
        try:
            data = store.artifacts.verify(reference).read_bytes()
        except (OSError, ValueError) as error:
            raise LockfileHandoffError(f"ready lockfile handoff {label} artifact is unavailable") from error
        if sha256(data).hexdigest() != digest:
            raise LockfileHandoffError(f"ready lockfile handoff {label} artifact digest is invalid")
    if store.artifacts.verify(receipt["artifact_ref"]).read_bytes() == store.artifacts.verify(receipt["before_artifact_ref"]).read_bytes():
        raise LockfileHandoffError("ready lockfile handoff did not change pnpm-lock.yaml")


def _reserved_payload(payload: Mapping[str, Any]) -> None:
    """Validate the minimally sufficient durable reservation binding."""

    required = {
        "schema_version", "kind", "state", "lease_state", "request_id", "request_fingerprint",
        "preview_fingerprint", "lease_token", "task_id", "task_revision", "contract_hash",
        "blocked_node_id", "root_node_id", "blocked_attempt", "repository", "repository_identity",
        "original_owner", "temporary_owner", "source_allocation", "repair_worktree", "durable_fingerprint",
        "source_fingerprint", "source_patch_sha256", "accepted_input_lineage", "after_ancestors",
        "input_fingerprints", "manifest_fingerprint", "lockfile", "changed_importers",
        "historical_result_sha256",
    }
    if set(payload) - {"reservation_event_cursor"} != required:
        raise LockfileHandoffError("lockfile handoff reservation has an invalid shape")
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("kind") != _KIND:
        raise LockfileHandoffError("lockfile handoff reservation schema is invalid")
    if payload.get("state") != "reserved" or payload.get("lease_state") != "active":
        raise LockfileHandoffError("lockfile handoff reservation is not active")
    for field in (
        "request_fingerprint", "preview_fingerprint", "lease_token", "contract_hash", "durable_fingerprint",
        "source_fingerprint", "source_patch_sha256", "manifest_fingerprint", "historical_result_sha256",
    ):
        _digest(payload.get(field), field)
    _text(payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID)
    _text(payload.get("task_id"), "task_id")
    _positive_int(payload.get("task_revision"), "task_revision")
    _positive_int(payload.get("blocked_attempt"), "blocked_attempt")
    _source_allocation(payload.get("source_allocation"))
    lineage = _ready_lineage(payload.get("accepted_input_lineage"))
    if canonical_json(_ancestor_list(payload.get("after_ancestors"))) != canonical_json(lineage["ancestors"]):
        raise LockfileHandoffError("lockfile handoff reservation lineage is inconsistent")
    _ready_inputs(payload.get("input_fingerprints"))
    lockfile = payload.get("lockfile")
    if not isinstance(lockfile, Mapping) or set(lockfile) != {"path", "before_sha256", "after_sha256"}:
        raise LockfileHandoffError("lockfile handoff reservation lockfile binding is invalid")
    if lockfile.get("path") != _LOCKFILE:
        raise LockfileHandoffError("lockfile handoff reservation lockfile path is invalid")
    _digest(lockfile.get("before_sha256"), "reservation before_sha256")
    _digest(lockfile.get("after_sha256"), "reservation after_sha256")
    _changed_importers(payload.get("changed_importers"))


def _terminal_receipt(payload: Mapping[str, Any], cursor: int) -> dict[str, Any]:
    """Render a non-ready terminal receipt without treating it as an overlay."""

    state = payload.get("state")
    if state == "ready":
        return _ready_receipt(payload, cursor)
    if state not in {"failed", "cancelled", "needs_action"}:
        raise LockfileHandoffError("lockfile handoff terminal receipt is invalid")
    required = {
        "schema_version", "kind", "state", "lease_state", "request_id", "request_fingerprint",
        "preview_fingerprint", "lease_token", "task_id", "repository", "repository_identity",
        "blocked_node_id", "blocked_attempt", "repair_worktree", "detail", "historical_result_unchanged",
        "overlay_attached", "external_operation_may_continue",
    }
    if set(payload) != required:
        raise LockfileHandoffError("lockfile handoff terminal receipt has an invalid shape")
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("kind") != _KIND or payload.get("lease_state") != "released":
        raise LockfileHandoffError("lockfile handoff terminal receipt has invalid metadata")
    for field in ("request_fingerprint", "preview_fingerprint", "lease_token"):
        _digest(payload.get(field), field)
    _text(payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID)
    _text(payload.get("task_id"), "task_id")
    _text(payload.get("detail"), "detail", maximum=_MAX_ERROR)
    if payload.get("historical_result_unchanged") is not True or payload.get("overlay_attached") is not False:
        raise LockfileHandoffError("lockfile handoff terminal receipt lifecycle flags are invalid")
    if type(payload.get("external_operation_may_continue")) is not bool:
        raise LockfileHandoffError("lockfile handoff terminal receipt external-operation flag is invalid")
    return {**dict(payload), "terminal_event_cursor": cursor}


def _assert_same_request(existing: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """Reject reuse of a business id for a different immutable apply input."""

    existing_fingerprint = existing.get("request_fingerprint")
    expected = _request_fingerprint(request)
    if existing_fingerprint != expected:
        raise LockfileHandoffError("lockfile handoff request_id was already used with different input")


def _business_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields whose identity is stable across preview and apply."""

    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "task_id": request["task_id"],
        "node_id": request["node_id"],
        "expected_revision": request["expected_revision"],
        "expected_attempt": request["expected_attempt"],
        "expected_contract_hash": request["expected_contract_hash"],
        "request_id": request["request_id"],
    }


def _request_fingerprint(request: Mapping[str, Any]) -> str:
    """Hash the business request only, never a generated preview digest."""

    return canonical_hash(_business_request(request))


def _repair_worktree_path(request: Mapping[str, Any], state_root: Path) -> Path:
    """Choose a deterministic retained private repair directory for one request."""

    digest = canonical_hash(
        {
            "task_id": request["task_id"],
            "node_id": request["node_id"],
            "attempt": request["expected_attempt"],
            "request_id": request["request_id"],
        }
    )[:32]
    return (state_root / "lockfile-handoffs" / digest).expanduser().resolve(strict=False)


def _temporary_owner(task_id: str, node_id: str, attempt: int) -> str:
    """Return an audit-only owner identity distinct from every task node spec."""

    return "lockfile-handoff:" + task_id + ":" + node_id + ":a" + str(attempt)


def _delta_payload(delta: SourceDelta) -> dict[str, Any]:
    """Return the complete bounded source-delta identity used in a fingerprint only."""

    return {
        "comparison_tree": delta.comparison_tree,
        "changed_paths": list(delta.changed_paths),
        "untracked_paths": list(delta.untracked_paths),
        "entries": [entry.to_dict() for entry in delta.entries],
        "sha256": delta.sha256,
    }


def _public_lineage(lineage: Mapping[str, Any]) -> dict[str, Any]:
    """Render lineage in the same exact form that a ready receipt will retain."""

    return {
        "ref": lineage["ref"],
        "receipt": lineage["receipt"],
        "input_tree_sha": lineage["input_tree_sha"],
        "ancestors": list(lineage["ancestors"]),
    }


def _input_fingerprint_summary(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Keep preview responses bounded while the journal retains full input maps."""

    input_files = inputs.get("input_files")
    manifest_files = inputs.get("manifest_files")
    workspace_entries = inputs.get("workspace_entries")
    if not isinstance(input_files, Mapping) or not isinstance(manifest_files, Mapping) or not isinstance(workspace_entries, list):
        raise LockfileHandoffError("lockfile handoff input fingerprints are invalid")
    return {
        "input_files_count": len(input_files),
        "manifest_files_count": len(manifest_files),
        "workspace_entries_count": len(workspace_entries),
        "engine_fingerprint": inputs.get("engine_fingerprint"),
        "input_files_fingerprint": canonical_hash(dict(input_files)),
    }


def _file_hash_map(raw: object, label: str) -> dict[str, str]:
    """Normalize a bounded relative POSIX path-to-digest mapping."""

    if not isinstance(raw, Mapping) or len(raw) > _MAX_INPUT_FILES:
        raise LockfileHandoffError(label + " is invalid")
    result: dict[str, str] = {}
    for raw_path, raw_digest in raw.items():
        if not isinstance(raw_path, str):
            raise LockfileHandoffError(label + " contains a non-string path")
        path = _relative_path(raw_path, label + " path")
        result[path] = _digest(raw_digest, label + " digest")
    if not result:
        raise LockfileHandoffError(label + " must not be empty")
    return dict(sorted(result.items()))


def _workspace_entries(raw: object) -> list[dict[str, Any]]:
    """Validate the importer engine's complete deterministic workspace table."""

    if not isinstance(raw, list) or len(raw) > _MAX_INPUT_FILES:
        raise LockfileHandoffError("workspace_entries is invalid")
    required = {
        "importer", "manifest", "package_name", "section", "name", "specifier",
        "version", "target_importer", "status",
    }
    normalized: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != required:
            raise LockfileHandoffError("workspace_entries entry is invalid")
        importer = _workspace_importer(entry.get("importer"), "workspace importer")
        target_importer = _workspace_importer(
            entry.get("target_importer"), "workspace target_importer"
        )
        manifest = _relative_path(entry.get("manifest"), "workspace manifest")
        if not manifest.endswith("package.json"):
            raise LockfileHandoffError("workspace manifest is not package.json")
        package_name = entry.get("package_name")
        if package_name is not None:
            package_name = _text(package_name, "workspace package_name")
        section = entry.get("section")
        if section not in {"dependencies", "devDependencies", "optionalDependencies"}:
            raise LockfileHandoffError("workspace entry section is invalid")
        name = _text(entry.get("name"), "workspace entry name")
        specifier = _text(entry.get("specifier"), "workspace entry specifier")
        if not specifier.startswith("workspace:"):
            raise LockfileHandoffError("workspace entry specifier is invalid")
        version = _text(entry.get("version"), "workspace entry version")
        if not version.startswith("link:"):
            raise LockfileHandoffError("workspace entry version is invalid")
        status = entry.get("status")
        if status not in {"present", "missing"}:
            raise LockfileHandoffError("workspace entry status is invalid")
        normalized.append(
            {
                "importer": importer,
                "manifest": manifest,
                "package_name": package_name,
                "section": section,
                "name": name,
                "specifier": specifier,
                "version": version,
                "target_importer": target_importer,
                "status": status,
            }
        )
    order = lambda entry: (
        0 if entry["importer"] == "." else 1,
        str(entry["importer"]),
        str(entry["section"]),
        str(entry["name"]),
    )
    if normalized != sorted(normalized, key=order):
        raise LockfileHandoffError("workspace_entries are not in deterministic order")
    return normalized


def _changed_importers(raw: object) -> list[dict[str, Any]]:
    """Validate the engine's concise importer-only modification evidence."""

    if not isinstance(raw, list) or len(raw) > _MAX_CHANGED_IMPORTERS:
        raise LockfileHandoffError("changed_importers is invalid")
    required = {"importer", "manifest", "created", "added"}
    # The enclosing item supplies importer/manifest. The engine's ``added``
    # entries are its fixed importer-record fields only.
    added_required = {"section", "name", "specifier", "version", "target_importer"}
    normalized: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != required:
            raise LockfileHandoffError("changed_importers entry is invalid")
        importer = _workspace_importer(entry.get("importer"), "changed importer")
        manifest = _relative_path(entry.get("manifest"), "changed importer manifest")
        if not manifest.endswith("package.json"):
            raise LockfileHandoffError("changed importer manifest is not package.json")
        if type(entry.get("created")) is not bool or not isinstance(entry.get("added"), list):
            raise LockfileHandoffError("changed_importers entry fields are invalid")
        added: list[dict[str, Any]] = []
        for dependency in entry["added"]:
            if not isinstance(dependency, Mapping) or set(dependency) != added_required:
                raise LockfileHandoffError("changed_importers added entry is invalid")
            section = dependency.get("section")
            if section not in {"dependencies", "devDependencies", "optionalDependencies"}:
                raise LockfileHandoffError("changed_importers added section is invalid")
            added.append(
                {
                    "section": section,
                    "name": _text(dependency.get("name"), "added name"),
                    "specifier": _text(dependency.get("specifier"), "added specifier"),
                    "version": _text(dependency.get("version"), "added version"),
                    "target_importer": _workspace_importer(
                        dependency.get("target_importer"), "added target_importer"
                    ),
                }
            )
        normalized.append(
            {"importer": importer, "manifest": manifest, "created": entry["created"], "added": added}
        )
    if normalized != sorted(normalized, key=lambda entry: (0 if entry["importer"] == "." else 1, entry["importer"])):
        raise LockfileHandoffError("changed_importers are not in deterministic order")
    return normalized


def _workspace_importer(value: object, label: str) -> str:
    """Accept the engine's root ``.`` importer or a normalized child path."""

    if value == ".":
        return "."
    if not isinstance(value, str):
        raise LockfileHandoffError(label + " is invalid")
    return _relative_path(value, label)


def _relative_path(value: str, label: str) -> str:
    """Require one non-empty repository-relative POSIX path."""

    if not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise LockfileHandoffError(label + " is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        raise LockfileHandoffError(label + " is invalid")
    normalized = str(path)
    if normalized != value:
        raise LockfileHandoffError(label + " is not normalized")
    return normalized


def _scope_list(raw: object, label: str) -> tuple[str, ...]:
    """Validate a persisted scope sequence before passing it to scope_allows."""

    if not isinstance(raw, (list, tuple)) or not all(isinstance(value, str) and value for value in raw):
        raise LockfileHandoffError(label + " is invalid")
    try:
        # scope_allows performs the actual normalization. The direct calls
        # below force every persisted item through that same parser.
        for value in raw:
            scope_allows(_LOCKFILE, [value], [])
    except ValueError as error:
        raise LockfileHandoffError(label + " is invalid") from error
    return tuple(raw)


def _string_list(raw: object, label: str) -> tuple[str, ...]:
    """Return one exact string sequence from persisted node metadata."""

    if not isinstance(raw, (list, tuple)) or not all(isinstance(value, str) and value for value in raw):
        raise LockfileHandoffError(label + " is invalid")
    return tuple(raw)


def _json_object(raw: object, label: str) -> dict[str, Any]:
    """Decode one persisted JSON object without silently accepting another type."""

    if not isinstance(raw, str):
        raise LockfileHandoffError(label + " is unavailable")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise LockfileHandoffError(label + " is invalid JSON") from error
    if not isinstance(value, dict):
        raise LockfileHandoffError(label + " must be an object")
    return value


def _json_safe(value: object, label: str) -> Any:
    """Copy JSON-safe evidence while refusing bytes and non-finite custom values."""

    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LockfileHandoffError(label + " is not JSON-safe") from error


def _json_safe_object(value: object, label: str) -> dict[str, Any]:
    """Return a copied JSON object from an evidence value."""

    copied = _json_safe(value, label)
    if not isinstance(copied, dict):
        raise LockfileHandoffError(label + " must be an object")
    return copied


def _file_sha256(path: Path) -> str:
    """Hash one required regular source file without following a missing path."""

    try:
        if path.is_symlink() or not path.is_file():
            raise LockfileHandoffError("lockfile path is not a regular file")
        return sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise LockfileHandoffError("cannot read lockfile path: " + str(error)) from error


def _git_text(path: Path, *arguments: str) -> str:
    """Run one fixed Git read operation and return its stripped output."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LockfileHandoffError("cannot inspect Git worktree: " + str(error)) from error
    if result.returncode:
        raise LockfileHandoffError(result.stderr.strip() or result.stdout.strip() or "Git inspection failed")
    return result.stdout.strip()


def _bounded_detail(value: object) -> str:
    """Retain an actionable real failure without storing unbounded child output."""

    text = str(value).strip().replace("\x00", "?")
    if not text:
        text = "lockfile handoff failed without a diagnostic"
    if len(text) > _MAX_ERROR:
        text = text[: _MAX_ERROR - 3] + "..."
    return text


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT) -> str:
    """Validate one bounded non-control single-line string."""

    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(label + " must be a bounded non-empty string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(label + " contains forbidden control characters")
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    """Return one strictly positive integer without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(label + " must be a positive integer")
    return value


def _digest(value: object, label: str) -> str:
    """Return one canonical lowercase SHA-256 digest."""

    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(label + " must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "TOOL",
    "TOOL_NAME",
    "LockfileHandoffError",
    "active_lockfile_handoff",
    "get_ready_lockfile_handoffs",
    "lockfile_handoff",
]
