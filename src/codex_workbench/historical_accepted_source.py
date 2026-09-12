"""Restore one exact historical accepted patch for a blocked worker.

The current blocked attempt is deliberately not a source of worker output.
It proves only the current accepted-ancestor input is clean.  A caller names
one immutable earlier ``node.accepted`` event, and this module seals the
event's verified patch into ``nodes.recovery_json`` for the ordinary next
attempt.  The source event and its allocation are never rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
from typing import Any, TypedDict

from .dependency_inputs import (
    DependencyInput,
    DependencyInputError,
    apply_accepted_ancestor_patches,
    changed_paths_since_input_tree,
    load_recorded_dependency_input,
    reconstruct_prepared_dependency_input,
    validate_dependency_input_lineage,
)
from .dirty_worktree_recovery import is_python_bytecode_residue_path
from .lockfile_handoff import get_ready_lockfile_handoffs
from .model import canonical_hash, canonical_json, now_iso
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeError, WorktreeManager, scope_allows


TOOL_NAME = "workbench_restore_accepted_source"
HISTORICAL_ACCEPTED_SOURCE_KIND = "historical-accepted-source-v1"
_SCHEMA_VERSION = 1
_EVENT_TYPE = "node.historical_accepted_source_queued"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEXT = 200


class HistoricalAcceptedSourceError(StateConflictError):
    """An old accepted result cannot safely seed the current blocked node."""


class HistoricalAcceptedSourceSource(TypedDict):
    """The immutable old accepted event selected by the caller."""

    attempt: int
    result_json: str
    result_sha256: str
    allocation_id: str
    worktree: str
    branch: str
    base_sha: str
    patch_ref: str
    patch_sha256: str
    dependency_input_ref: str | None
    dependency_input: dict[str, Any] | None
    ancestors: list[dict[str, Any]]


class HistoricalAcceptedSourceCurrent(TypedDict):
    """The current blocked source whose local delta must be empty."""

    attempt: int
    result_json: str
    result_sha256: str
    allocation_id: str
    worktree: str
    branch: str
    base_sha: str
    dependency_input_ref: str | None
    dependency_input: dict[str, Any]
    ancestors: list[dict[str, Any]]
    input_tree_sha: str
    source_delta_sha256: str
    legacy_reconstructed: bool
    spec_sha256: str


class HistoricalAcceptedSourceBinding(TypedDict):
    """The authorization persisted on the current node before it is claimed."""

    schema_version: int
    kind: str
    state: str
    request_id: str
    request_fingerprint: str
    fingerprint: str
    task_id: str
    node_id: str
    authorization_revision: int
    current_revision: int
    current_contract_hash: str
    current_base_sha: str
    current_attempt: int
    source_event_cursor: int
    source: HistoricalAcceptedSourceSource
    current: HistoricalAcceptedSourceCurrent
    ready_overlays: list[dict[str, Any]]
    assignment: dict[str, Any] | None


class HistoricalAcceptedSourcePreparedReceipt(TypedDict):
    """Proof that a fresh claimed target contains old patch on current input."""

    schema_version: int
    kind: str
    state: str
    binding_sha256: str
    source_event_cursor: int
    source_attempt: int
    current_attempt: int
    current_allocation_id: str
    target_attempt: int
    target_worktree: str
    target_branch: str
    dependency_input_tree_sha: str
    patch_ref: str
    patch_sha256: str


@dataclass(frozen=True)
class PreparedHistoricalAcceptedSource:
    """A fresh target and current dependency input ready for normal execution."""

    worktree: Path
    dependency_input: DependencyInput
    receipt: HistoricalAcceptedSourcePreparedReceipt


def historical_accepted_source(
    store: WorkbenchStore,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Preview, authorize, or inspect restoration of a historical event.

    ``apply`` requires an already-executing Authority journal entry.  The
    Authority service writes that entry before it invokes this function, so a
    duplicate request can return the same durable event receipt without
    replaying a task mutation.

    @param store: The durable task-state owner.
    @param arguments: Exact operation arguments supplied by the MCP adapter.
    @returns: A preview, immutable queued receipt, or receipt status.
    """

    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    request = _arguments(arguments)
    if request["op"] == "status":
        receipt = _receipt(store, request["task_id"], request["request_id"])
        if receipt is None:
            raise KeyError(request["request_id"])
        return receipt

    existing = _receipt(store, request["task_id"], request["request_id"])
    if existing is not None:
        _assert_same_request(existing, request)
        if request["op"] == "apply" and existing["fingerprint"] != request["expected_fingerprint"]:
            raise HistoricalAcceptedSourceError(
                "historical accepted source preview fingerprint is stale"
            )
        return existing

    preflight = _preflight(store, request)
    if request["op"] == "preview":
        return _preview(preflight)
    if preflight["fingerprint"] != request["expected_fingerprint"]:
        raise HistoricalAcceptedSourceError(
            "historical accepted source preview fingerprint is stale; obtain a fresh preview"
        )
    _require_authority_reservation(store, request)
    return _apply(store, preflight)


def parse_historical_accepted_source_binding(
    raw: object,
    *,
    next_attempt: int | None = None,
    allow_assigned: bool = False,
) -> HistoricalAcceptedSourceBinding | None:
    """Decode this recovery kind without reading SQLite or Git.

    Other recovery kinds return ``None`` so their owner retains control.  An
    assigned binding is deliberately unavailable to a new claim, but the
    dispatch fence can decode it with ``allow_assigned=True``.

    @param raw: ``nodes.recovery_json`` or a parsed mapping.
    @param next_attempt: Optional claimed attempt, which must follow current.
    @param allow_assigned: Allow an assignment receipt after worktree CAS.
    @returns: A normalized binding, or ``None`` for another recovery kind.
    """

    if raw is None:
        return None
    value = _json_mapping(raw, "historical accepted source binding")
    if value.get("kind") != HISTORICAL_ACCEPTED_SOURCE_KIND:
        return None
    state = value.get("state")
    base_fields = {
        "schema_version",
        "kind",
        "state",
        "request_id",
        "request_fingerprint",
        "fingerprint",
        "task_id",
        "node_id",
        "authorization_revision",
        "current_revision",
        "current_contract_hash",
        "current_base_sha",
        "current_attempt",
        "source_event_cursor",
        "source",
        "current",
        "ready_overlays",
    }
    assigned_fields = base_fields | {"assignment"}
    if state == "authorized":
        if set(value) != base_fields:
            raise HistoricalAcceptedSourceError(
                "historical accepted source binding has an invalid shape"
            )
    elif state == "assigned" and allow_assigned:
        if set(value) != assigned_fields:
            raise HistoricalAcceptedSourceError(
                "historical accepted source assignment has an invalid shape"
            )
    else:
        raise HistoricalAcceptedSourceError(
            "historical accepted source binding is not claimable"
        )
    if value["schema_version"] != _SCHEMA_VERSION:
        raise HistoricalAcceptedSourceError("historical accepted source schema is unsupported")
    task_id = _text(value["task_id"], "historical accepted source task_id")
    node_id = _text(value["node_id"], "historical accepted source node_id")
    request_id = _text(value["request_id"], "historical accepted source request_id")
    authorization_revision = _positive_int(
        value["authorization_revision"], "historical accepted source authorization_revision"
    )
    current_revision = _positive_int(
        value["current_revision"], "historical accepted source current_revision"
    )
    if authorization_revision != current_revision + 1:
        raise HistoricalAcceptedSourceError(
            "historical accepted source authorization revision is invalid"
        )
    current_contract_hash = _digest(
        value["current_contract_hash"], "historical accepted source current_contract_hash"
    )
    current_base_sha = _text(
        value["current_base_sha"], "historical accepted source current_base_sha", maximum=512
    )
    current_attempt = _positive_int(
        value["current_attempt"], "historical accepted source current_attempt"
    )
    source_event_cursor = _positive_int(
        value["source_event_cursor"], "historical accepted source event cursor"
    )
    if next_attempt is not None and current_attempt + 1 != _positive_int(
        next_attempt, "historical accepted source next_attempt"
    ):
        raise HistoricalAcceptedSourceError(
            "historical accepted source does not match the claimed next attempt"
        )
    request_fingerprint = _digest(
        value["request_fingerprint"], "historical accepted source request_fingerprint"
    )
    fingerprint = _digest(value["fingerprint"], "historical accepted source fingerprint")
    source = _source_binding(value["source"], task_id, node_id, current_base_sha)
    current = _current_binding(value["current"], task_id, node_id, current_base_sha)
    if source["attempt"] >= current_attempt or current["attempt"] != current_attempt:
        raise HistoricalAcceptedSourceError(
            "historical accepted source attempts do not identify an older source and current block"
        )
    if source["base_sha"] != current["base_sha"]:
        raise HistoricalAcceptedSourceError(
            "historical accepted source base does not match the current block"
        )
    overlays = _overlays(value["ready_overlays"])
    assignment: dict[str, Any] | None = None
    if state == "assigned":
        assignment = _assignment(
            value["assignment"], task_id=task_id, node_id=node_id,
            next_attempt=current_attempt + 1,
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "state": str(state),
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "fingerprint": fingerprint,
        "task_id": task_id,
        "node_id": node_id,
        "authorization_revision": authorization_revision,
        "current_revision": current_revision,
        "current_contract_hash": current_contract_hash,
        "current_base_sha": current_base_sha,
        "current_attempt": current_attempt,
        "source_event_cursor": source_event_cursor,
        "source": source,
        "current": current,
        "ready_overlays": overlays,
        **({"assignment": assignment} if assignment is not None else {}),
    }


def prepare_historical_accepted_source(
    store: WorkbenchStore,
    binding: Mapping[str, Any] | str,
    manager: WorktreeManager,
) -> PreparedHistoricalAcceptedSource:
    """Prepare current accepted input and apply the exact historical patch.

    The current blocked source is inspected during authorization and
    contributes no patch. Preparation uses current accepted ancestors and
    ready overlays, then applies the selected historical artifact byte-for-
    byte. It does not update SQLite; ``assign_worktree`` performs the fenced
    durable assignment.

    @param store: The task-state and artifact owner.
    @param binding: The claimed ``historical-accepted-source-v1`` binding.
    @param manager: The target worktree manager.
    @returns: A target worktree, current dependency input, and receipt.
    """

    parsed = parse_historical_accepted_source_binding(binding)
    if parsed is None:
        raise HistoricalAcceptedSourceError("historical accepted source binding is missing")
    target_attempt = parsed["current_attempt"] + 1
    initial = _preparation_snapshot(store, parsed, target_attempt)
    try:
        target = manager.prepare_clean(
            initial["repository"],
            parsed["current_base_sha"],
            parsed["task_id"],
            parsed["node_id"],
            target_attempt,
        )
        task = store.get_task(parsed["task_id"])
        dependency_input = apply_accepted_ancestor_patches(
            task,
            parsed["node_id"],
            target,
            store.artifacts,
            manager,
            ready_lockfile_handoffs=parsed["ready_overlays"],
        )
        if dependency_input is None:
            from .dependency_inputs import base_dependency_input

            dependency_input = base_dependency_input(
                task_id=parsed["task_id"],
                node_id=parsed["node_id"],
                base_sha=parsed["current_base_sha"],
                worktree=target,
            )
        if canonical_json(dependency_input.receipt["ancestors"]) != canonical_json(
            parsed["current"]["ancestors"]
        ):
            raise HistoricalAcceptedSourceError(
                "current accepted ancestor closure changed after authorization"
            )
        patch = _artifact_bytes(
            store,
            parsed["source"]["patch_ref"],
            parsed["source"]["patch_sha256"],
            "historical accepted source patch",
        )
        manager.apply_patch(target, store.artifacts.verify(parsed["source"]["patch_ref"]))
        if manager.diff_patch(target, dependency_input.input_tree_sha) != patch:
            raise HistoricalAcceptedSourceError(
                "historical accepted source patch is incompatible with the current input"
            )
        _validate_restored_patch_scope(task, parsed["node_id"], target, dependency_input)
    except (DependencyInputError, WorktreeError, OSError, ValueError) as error:
        if isinstance(error, HistoricalAcceptedSourceError):
            raise
        raise HistoricalAcceptedSourceError(
            f"historical accepted source preparation failed: {error}"
        ) from error

    final = _preparation_snapshot(store, parsed, target_attempt)
    if canonical_json(initial) != canonical_json(final):
        raise HistoricalAcceptedSourceError(
            "historical accepted source binding changed during target preparation"
        )
    receipt: HistoricalAcceptedSourcePreparedReceipt = {
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "state": "prepared",
        "binding_sha256": canonical_hash(parsed),
        "source_event_cursor": parsed["source_event_cursor"],
        "source_attempt": parsed["source"]["attempt"],
        "current_attempt": parsed["current_attempt"],
        "current_allocation_id": parsed["current"]["allocation_id"],
        "target_attempt": target_attempt,
        "target_worktree": str(target),
        "target_branch": WorktreeManager.branch_name(
            parsed["task_id"], parsed["node_id"], target_attempt
        ),
        "dependency_input_tree_sha": dependency_input.input_tree_sha,
        "patch_ref": parsed["source"]["patch_ref"],
        "patch_sha256": parsed["source"]["patch_sha256"],
    }
    return PreparedHistoricalAcceptedSource(target, dependency_input, receipt)


def assert_historical_accepted_source_dispatch(
    store: WorkbenchStore,
    claimed: Mapping[str, Any],
) -> None:
    """Fence dispatch after preparation or materialization may have taken time.

    @param store: The durable task-state owner.
    @param claimed: The ordinary scheduler claim carrying this binding.
    @returns: ``None`` when the exact assignment remains dispatchable.
    """

    if not isinstance(store, WorkbenchStore):
        raise TypeError("store must be a WorkbenchStore")
    if not isinstance(claimed, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source claim is invalid")
    binding = parse_historical_accepted_source_binding(
        claimed.get("historical_accepted_source"),
        next_attempt=_positive_int(claimed.get("attempt"), "claimed attempt"),
        allow_assigned=True,
    )
    if binding is None:
        raise HistoricalAcceptedSourceError("historical accepted source claim is missing")
    task_id = _text(claimed.get("task_id"), "claimed task_id")
    node_id = _text(claimed.get("node_id"), "claimed node_id")
    if (task_id, node_id) != (binding["task_id"], binding["node_id"]):
        raise HistoricalAcceptedSourceError("historical accepted source claim identity is stale")
    coordinator_epoch = _positive_int(claimed.get("coordinator_epoch"), "claimed coordinator_epoch")
    lease_epoch = _positive_int(claimed.get("lease_epoch"), "claimed lease_epoch")
    with store.connection() as connection:
        store._assert_active_coordinator(connection, coordinator_epoch)
        task = connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        node = connection.execute(
            """
            SELECT state, attempt, coordinator_epoch, lease_epoch, worktree, recovery_json
            FROM nodes WHERE task_id = ? AND node_id = ?
            """,
            (task_id, node_id),
        ).fetchone()
    if task is None or node is None:
        raise KeyError((task_id, node_id))
    if task["state"] in {"paused", "cancelled"}:
        raise HistoricalAcceptedSourceError(
            "historical accepted source dispatch was paused or cancelled"
        )
    if (
        node["state"] != "running"
        or int(node["attempt"]) != int(claimed["attempt"])
        or int(node["coordinator_epoch"]) != coordinator_epoch
        or int(node["lease_epoch"]) != lease_epoch
    ):
        raise HistoricalAcceptedSourceError("historical accepted source dispatch lease is stale")
    current = parse_historical_accepted_source_binding(
        node["recovery_json"], next_attempt=int(claimed["attempt"]), allow_assigned=True
    )
    if current is None or canonical_json(_authorization(current)) != canonical_json(_authorization(binding)):
        raise HistoricalAcceptedSourceError("historical accepted source dispatch binding changed")
    if current["state"] == "assigned":
        assignment = current.get("assignment")
        assert assignment is not None
        if node["worktree"] != assignment["target_worktree"]:
            raise HistoricalAcceptedSourceError(
                "historical accepted source assigned worktree changed before dispatch"
            )


def _arguments(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("historical accepted source arguments must be an object")
    op = raw.get("op")
    fields = {
        "status": {"op", "task_id", "request_id"},
        "preview": {
            "op", "task_id", "node_id", "accepted_event_cursor", "expected_attempt",
            "expected_revision", "expected_contract_hash", "request_id",
        },
        "apply": {
            "op", "task_id", "node_id", "accepted_event_cursor", "expected_attempt",
            "expected_revision", "expected_contract_hash", "request_id", "expected_fingerprint",
        },
    }
    expected = fields.get(op)
    if expected is None or set(raw) != expected:
        raise ValueError("historical accepted source operation requires exact fields")
    result: dict[str, Any] = {
        "op": op,
        "task_id": _text(raw.get("task_id"), "task_id"),
        "request_id": _text(raw.get("request_id"), "request_id"),
    }
    if op == "status":
        return result
    result.update(
        {
            "node_id": _text(raw.get("node_id"), "node_id"),
            "accepted_event_cursor": _positive_int(
                raw.get("accepted_event_cursor"), "accepted_event_cursor"
            ),
            "expected_attempt": _positive_int(raw.get("expected_attempt"), "expected_attempt"),
            "expected_revision": _positive_int(raw.get("expected_revision"), "expected_revision"),
            "expected_contract_hash": _digest(
                raw.get("expected_contract_hash"), "expected_contract_hash"
            ),
        }
    )
    result["request_fingerprint"] = canonical_hash(_business_request(result))
    if op == "apply":
        result["expected_fingerprint"] = _digest(
            raw.get("expected_fingerprint"), "expected_fingerprint"
        )
    return result


def _preflight(store: WorkbenchStore, request: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _snapshot(store, request, inspect_current_source=True)
    binding = _binding(request, snapshot)
    fingerprint = canonical_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
            "request": _business_request(request),
            "snapshot": snapshot["fingerprint_input"],
            "binding": binding,
        }
    )
    binding["fingerprint"] = fingerprint
    parsed = parse_historical_accepted_source_binding(binding)
    assert parsed is not None
    return {"request": dict(request), "snapshot": snapshot, "binding": parsed, "fingerprint": fingerprint}


def _apply(store: WorkbenchStore, preflight: Mapping[str, Any]) -> dict[str, Any]:
    request = preflight["request"]
    binding = preflight["binding"]
    if not isinstance(request, Mapping) or not isinstance(binding, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source preflight is invalid")
    timestamp = now_iso()
    with store.transaction() as connection:
        existing = _receipt_from_connection(connection, str(request["task_id"]), str(request["request_id"]))
        if existing is not None:
            _assert_same_request(existing, request)
            if existing["fingerprint"] != request["expected_fingerprint"]:
                raise HistoricalAcceptedSourceError("historical accepted source preview fingerprint is stale")
            return existing
        _require_authority_reservation_connection(connection, request)
        _assert_apply_snapshot(connection, request, binding)
        _assert_no_active_work_or_recovery(connection, str(request["task_id"]), str(request["request_id"]))
        node_changed = connection.execute(
            """
            UPDATE nodes
            SET state = 'pending', worker_id = NULL, worktree = NULL,
                effective_executor = NULL, effective_model = NULL,
                started_at = NULL, settled_at = NULL, result_json = NULL,
                coordinator_epoch = 0, lease_epoch = 0, recovery_json = ?, updated_at = ?
            WHERE task_id = ? AND node_id = ? AND state = 'blocked' AND attempt = ?
              AND worktree = ? AND result_json = ? AND recovery_json IS NULL
            """,
            (
                canonical_json(binding), timestamp, request["task_id"], request["node_id"],
                request["expected_attempt"], binding["current"]["worktree"],
                binding["current"]["result_json"],
            ),
        ).rowcount
        if node_changed != 1:
            raise HistoricalAcceptedSourceError("historical accepted source node compare-and-set failed")
        revision = int(request["expected_revision"]) + 1
        task_changed = connection.execute(
            """
            UPDATE tasks
            SET state = 'queued', state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
            WHERE task_id = ? AND state = 'blocked' AND state_revision = ? AND contract_hash = ?
            """,
            (
                revision, timestamp, request["task_id"], request["expected_revision"],
                request["expected_contract_hash"],
            ),
        ).rowcount
        if task_changed != 1:
            raise HistoricalAcceptedSourceError("historical accepted source task compare-and-set failed")
        payload = _queued_payload(binding, request, revision)
        cursor = store._event(
            connection, _EVENT_TYPE, str(request["task_id"]), str(request["node_id"]),
            payload, created_at=timestamp,
        )
        store._event(
            connection,
            "task.state_changed",
            str(request["task_id"]),
            None,
            {
                "from": "blocked", "to": "queued", "revision": revision,
                "blocker": None, "historical_accepted_source": True,
            },
            created_at=timestamp,
        )
    return _receipt_from_payload(payload, cursor)


def _snapshot(
    store: WorkbenchStore,
    request: Mapping[str, Any],
    *,
    inspect_current_source: bool,
) -> dict[str, Any]:
    with store.connection() as connection:
        return _snapshot_from_connection(
            store, connection, request, inspect_current_source=inspect_current_source
        )


def _snapshot_from_connection(
    store: WorkbenchStore,
    connection: sqlite3.Connection,
    request: Mapping[str, Any],
    *,
    inspect_current_source: bool,
) -> dict[str, Any]:
    task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (request["task_id"],)).fetchone()
    node = connection.execute(
        "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?",
        (request["task_id"], request["node_id"]),
    ).fetchone()
    if task is None or node is None:
        raise KeyError((request["task_id"], request["node_id"]))
    if int(task["state_revision"]) != int(request["expected_revision"]):
        raise HistoricalAcceptedSourceError(
            f"expected task revision {request['expected_revision']}, found {task['state_revision']}"
        )
    if task["state"] != "blocked":
        raise HistoricalAcceptedSourceError(
            f"task {request['task_id']} is {task['state']}, expected blocked"
        )
    if task["contract_hash"] != request["expected_contract_hash"]:
        raise HistoricalAcceptedSourceError("historical accepted source contract hash changed")
    if int(node["attempt"]) != int(request["expected_attempt"]):
        raise HistoricalAcceptedSourceError(
            f"expected node attempt {request['expected_attempt']}, found {node['attempt']}"
        )
    if node["state"] != "blocked":
        raise HistoricalAcceptedSourceError(
            f"node {request['node_id']} is {node['state']}, expected blocked"
        )
    if node["recovery_json"] is not None:
        raise HistoricalAcceptedSourceError("blocked node already has a pending recovery binding")
    _assert_no_active_work_or_recovery(
        connection, str(request["task_id"]), str(request["request_id"])
    )
    contract = _json_mapping(task["contract_json"], "historical accepted source task contract")
    repository = _text(contract.get("repository"), "historical accepted source repository", maximum=4096)
    base_sha = _text(contract.get("base_sha"), "historical accepted source contract base", maximum=512)
    if (
        contract.get("external_write_permission") is not False
        or contract.get("destructive_action_permission") is not False
    ):
        raise HistoricalAcceptedSourceError(
            "historical accepted source does not permit external or destructive work"
        )
    spec = _json_mapping(node["spec_json"], "historical accepted source node spec")
    if spec.get("verifier") is True:
        raise HistoricalAcceptedSourceError("historical accepted source is only supported for worker nodes")
    current_result_json = node["result_json"]
    if not isinstance(current_result_json, str):
        raise HistoricalAcceptedSourceError("current blocked node has no result receipt")
    current_result = _result(current_result_json, "current blocked result")
    if current_result.get("status") != "blocked":
        raise HistoricalAcceptedSourceError("current blocked node result is not blocked")

    current_allocation = connection.execute(
        """
        SELECT * FROM worktree_allocations
        WHERE task_id = ? AND node_id = ? AND attempt = ?
        """,
        (request["task_id"], request["node_id"], request["expected_attempt"]),
    ).fetchone()
    _validate_allocation(
        current_allocation, task_id=str(request["task_id"]), node_id=str(request["node_id"]),
        attempt=int(request["expected_attempt"]), repository=repository, base_sha=base_sha,
        result_json=current_result_json, required_state="active", require_worktree=str(node["worktree"] or ""),
        label="current blocked source",
    )
    assert current_allocation is not None

    event = connection.execute(
        "SELECT * FROM events WHERE cursor = ?", (request["accepted_event_cursor"],)
    ).fetchone()
    if event is None:
        raise HistoricalAcceptedSourceError("historical accepted event is unavailable")
    if (
        event["event_type"] != "node.accepted"
        or event["task_id"] != request["task_id"]
        or event["node_id"] != request["node_id"]
    ):
        raise HistoricalAcceptedSourceError(
            "accepted_event_cursor is not an accepted event for this task and node"
        )
    event_payload = _json_mapping(event["payload_json"], "historical accepted event")
    source_attempt = _positive_int(event_payload.get("attempt"), "historical accepted event attempt")
    if source_attempt >= int(request["expected_attempt"]):
        raise HistoricalAcceptedSourceError(
            "historical accepted event is not older than the current blocked attempt"
        )
    source_result = event_payload.get("result")
    if not isinstance(source_result, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted event has no result")
    source_result_json = canonical_json(dict(source_result))
    source_result_value = _result(source_result_json, "historical accepted source result")
    if source_result_value.get("status") != "succeeded":
        raise HistoricalAcceptedSourceError("historical accepted source result is not succeeded")
    source_artifacts = _artifacts(source_result_value, "historical accepted source")
    patch_ref = _text(source_artifacts.get("patch"), "historical accepted source patch_ref", maximum=512)
    patch = _artifact_bytes(store, patch_ref, None, "historical accepted source patch")
    if not patch:
        raise HistoricalAcceptedSourceError("historical accepted source patch is empty")

    source_allocation = connection.execute(
        """
        SELECT * FROM worktree_allocations
        WHERE task_id = ? AND node_id = ? AND attempt = ?
        """,
        (request["task_id"], request["node_id"], source_attempt),
    ).fetchone()
    _validate_allocation(
        source_allocation, task_id=str(request["task_id"]), node_id=str(request["node_id"]),
        attempt=source_attempt, repository=repository, base_sha=base_sha,
        result_json=source_result_json, required_state=None, require_worktree=None,
        label="historical accepted source",
    )
    assert source_allocation is not None

    node_rows = connection.execute(
        "SELECT node_id, spec_json FROM nodes WHERE task_id = ? ORDER BY node_id",
        (request["task_id"],),
    ).fetchall()
    specs = _specs(node_rows, str(request["task_id"]))
    ancestors = _historical_ancestors(
        connection, task_id=str(request["task_id"]), node_id=str(request["node_id"]),
        source_attempt=source_attempt, source_event_cursor=int(request["accepted_event_cursor"]),
        specs=specs,
    )
    source_input_ref = source_artifacts.get("dependency-input")
    source_input, source_input_ref = _historical_dependency_input(
        store, source_input_ref, task_id=str(request["task_id"]), node_id=str(request["node_id"]),
        base_sha=base_sha, ancestors=ancestors,
    )

    current_task = store.get_task(str(request["task_id"]))
    current_input_ref = _artifacts(current_result, "current blocked source").get("dependency-input")
    current_input, current_input_ref, legacy = _current_dependency_input(
        store, current_result=current_result, task=current_task, node_id=str(request["node_id"]),
        base_sha=base_sha, worktree=Path(str(current_allocation["current_path"])),
        dependency_input_ref=current_input_ref,
    )
    if inspect_current_source:
        _validate_current_source_worktree(
            repository=repository, worktree=Path(str(current_allocation["current_path"])),
            branch=str(current_allocation["branch"]), base_sha=base_sha,
            dependency_input=current_input,
        )
    ready_overlays = get_ready_lockfile_handoffs(store, str(request["task_id"]))
    overlays = _overlays(ready_overlays)
    current_ancestors = _ancestor_receipt_list(current_input.receipt.get("ancestors"), "current accepted ancestors")
    source_ancestors = _ancestor_receipt_list(
        source_input.receipt.get("ancestors") if source_input is not None else [],
        "historical accepted ancestors",
    )
    source = {
        "attempt": source_attempt,
        "result_json": source_result_json,
        "result_sha256": sha256(source_result_json.encode()).hexdigest(),
        "allocation_id": str(source_allocation["allocation_id"]),
        "worktree": str(source_allocation["current_path"]),
        "branch": str(source_allocation["branch"]),
        "base_sha": base_sha,
        "patch_ref": patch_ref,
        "patch_sha256": sha256(patch).hexdigest(),
        "dependency_input_ref": source_input_ref,
        "dependency_input": dict(source_input.receipt) if source_input is not None else None,
        "ancestors": source_ancestors,
    }
    current = {
        "attempt": int(request["expected_attempt"]),
        "result_json": current_result_json,
        "result_sha256": sha256(current_result_json.encode()).hexdigest(),
        "allocation_id": str(current_allocation["allocation_id"]),
        "worktree": str(current_allocation["current_path"]),
        "branch": str(current_allocation["branch"]),
        "base_sha": base_sha,
        "dependency_input_ref": current_input_ref,
        "dependency_input": dict(current_input.receipt),
        "ancestors": current_ancestors,
        "input_tree_sha": current_input.input_tree_sha,
        "source_delta_sha256": canonical_hash({"changed_paths": [], "ignored_paths": []}),
        "legacy_reconstructed": legacy,
        "spec_sha256": canonical_hash(spec),
    }
    fingerprint_input = {
        "task_id": str(request["task_id"]),
        "node_id": str(request["node_id"]),
        "task_state": str(task["state"]),
        "task_revision": int(task["state_revision"]),
        "contract_hash": str(task["contract_hash"]),
        "repository": repository,
        "base_sha": base_sha,
        "source_event_cursor": int(request["accepted_event_cursor"]),
        "source": source,
        "current": current,
        "ready_overlays": overlays,
    }
    return {
        "repository": repository,
        "contract": contract,
        "source": source,
        "current": current,
        "ready_overlays": overlays,
        "fingerprint_input": fingerprint_input,
    }


def _binding(request: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    source = snapshot.get("source")
    current = snapshot.get("current")
    if not isinstance(source, Mapping) or not isinstance(current, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source snapshot is invalid")
    binding = {
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "state": "authorized",
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "fingerprint": "0" * 64,
        "task_id": request["task_id"],
        "node_id": request["node_id"],
        "authorization_revision": int(request["expected_revision"]) + 1,
        "current_revision": int(request["expected_revision"]),
        "current_contract_hash": request["expected_contract_hash"],
        "current_base_sha": snapshot["contract"]["base_sha"],
        "current_attempt": int(request["expected_attempt"]),
        "source_event_cursor": int(request["accepted_event_cursor"]),
        "source": dict(source),
        "current": dict(current),
        "ready_overlays": list(snapshot["ready_overlays"]),
    }
    return binding


def _preview(preflight: Mapping[str, Any]) -> dict[str, Any]:
    binding = preflight["binding"]
    if not isinstance(binding, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source preview is invalid")
    return {
        "ok": True,
        "dry_run": True,
        "queued": False,
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "task_id": binding["task_id"],
        "node_id": binding["node_id"],
        "request_id": binding["request_id"],
        "request_fingerprint": binding["request_fingerprint"],
        "fingerprint": binding["fingerprint"],
        "current_revision": binding["current_revision"],
        "next_attempt": int(binding["current_attempt"]) + 1,
        "source_event_cursor": binding["source_event_cursor"],
        "source": {
            "attempt": binding["source"]["attempt"],
            "result_sha256": binding["source"]["result_sha256"],
            "allocation_id": binding["source"]["allocation_id"],
            "patch_ref": binding["source"]["patch_ref"],
            "patch_sha256": binding["source"]["patch_sha256"],
            "ancestors": binding["source"]["ancestors"],
        },
        "current": {
            "attempt": binding["current"]["attempt"],
            "result_sha256": binding["current"]["result_sha256"],
            "allocation_id": binding["current"]["allocation_id"],
            "input_tree_sha": binding["current"]["input_tree_sha"],
            "ancestors": binding["current"]["ancestors"],
            "legacy_reconstructed": binding["current"]["legacy_reconstructed"],
        },
        "ready_overlay_event_cursors": [
            overlay.get("ready_event_cursor") for overlay in binding["ready_overlays"]
        ],
    }


def _queued_payload(
    binding: Mapping[str, Any], request: Mapping[str, Any], revision: int
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "state": "queued",
        "request_id": binding["request_id"],
        "request_fingerprint": binding["request_fingerprint"],
        "fingerprint": binding["fingerprint"],
        "task_id": binding["task_id"],
        "node_id": binding["node_id"],
        "revision": revision,
        "authorization_revision": binding["authorization_revision"],
        "expected_contract_hash": request["expected_contract_hash"],
        "next_attempt": int(binding["current_attempt"]) + 1,
        "source_event_cursor": binding["source_event_cursor"],
        "source_attempt": binding["source"]["attempt"],
        "source_result_sha256": binding["source"]["result_sha256"],
        "source_patch_ref": binding["source"]["patch_ref"],
        "source_patch_sha256": binding["source"]["patch_sha256"],
        "current_attempt": binding["current_attempt"],
        "current_allocation_id": binding["current"]["allocation_id"],
        "current_result_sha256": binding["current"]["result_sha256"],
        "binding_sha256": canonical_hash(binding),
        "ready_overlay_event_cursors": [
            overlay.get("ready_event_cursor") for overlay in binding["ready_overlays"]
        ],
        "historical_result_unchanged": True,
        "queued": True,
    }


def _receipt(store: WorkbenchStore, task_id: str, request_id: str) -> dict[str, Any] | None:
    with store.connection() as connection:
        return _receipt_from_connection(connection, task_id, request_id)


def _receipt_from_connection(
    connection: sqlite3.Connection, task_id: str, request_id: str
) -> dict[str, Any] | None:
    rows = connection.execute(
        """
        SELECT cursor, payload_json FROM events
        WHERE event_type = ? AND task_id = ?
          AND json_extract(payload_json, '$.request_id') = ?
        ORDER BY cursor
        """,
        (_EVENT_TYPE, task_id, request_id),
    ).fetchall()
    if not rows:
        return None
    receipts = [_receipt_from_payload(_json_mapping(row["payload_json"], "historical source receipt"), int(row["cursor"])) for row in rows]
    first = receipts[0]
    if any(canonical_json(receipt) != canonical_json(first) for receipt in receipts[1:]):
        raise HistoricalAcceptedSourceError(
            "historical accepted source request has conflicting durable receipts"
        )
    return first


def _receipt_from_payload(payload: Mapping[str, Any], cursor: int) -> dict[str, Any]:
    required = {
        "schema_version", "kind", "state", "request_id", "request_fingerprint", "fingerprint",
        "task_id", "node_id", "revision", "authorization_revision", "expected_contract_hash",
        "next_attempt", "source_event_cursor", "source_attempt", "source_result_sha256",
        "source_patch_ref", "source_patch_sha256", "current_attempt", "current_allocation_id",
        "current_result_sha256", "binding_sha256", "ready_overlay_event_cursors",
        "historical_result_unchanged", "queued",
    }
    if set(payload) != required:
        raise HistoricalAcceptedSourceError("historical accepted source receipt has an invalid shape")
    for field in ("request_id", "task_id", "node_id", "source_patch_ref", "current_allocation_id"):
        _text(payload.get(field), field, maximum=512)
    for field in (
        "request_fingerprint", "fingerprint", "expected_contract_hash", "source_result_sha256",
        "source_patch_sha256", "current_result_sha256", "binding_sha256",
    ):
        _digest(payload.get(field), field)
    for field in (
        "revision", "authorization_revision", "next_attempt", "source_event_cursor",
        "source_attempt", "current_attempt",
    ):
        _positive_int(payload.get(field), field)
    cursors = payload.get("ready_overlay_event_cursors")
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("kind") != HISTORICAL_ACCEPTED_SOURCE_KIND
        or payload.get("state") != "queued"
        or payload.get("queued") is not True
        or payload.get("historical_result_unchanged") is not True
        or payload.get("authorization_revision") != payload.get("revision")
        or payload.get("next_attempt") != payload.get("current_attempt") + 1
        or not isinstance(cursors, list)
        or any(type(value) is not int or value < 1 for value in cursors)
        or cursors != sorted(set(cursors))
        or type(cursor) is not int
        or cursor < 1
    ):
        raise HistoricalAcceptedSourceError("historical accepted source receipt is invalid")
    return {**dict(payload), "authorization_event_cursor": cursor}


def _require_authority_reservation(store: WorkbenchStore, request: Mapping[str, Any]) -> None:
    with store.connection() as connection:
        _require_authority_reservation_connection(connection, request)


def _require_authority_reservation_connection(
    connection: sqlite3.Connection, request: Mapping[str, Any]
) -> None:
    row = connection.execute(
        "SELECT tool, task_id, state FROM authority_requests WHERE request_id = ?",
        (request["request_id"],),
    ).fetchone()
    if row is None or (row["tool"], row["task_id"], row["state"]) != (
        TOOL_NAME, request["task_id"], "executing"
    ):
        raise HistoricalAcceptedSourceError(
            "historical accepted source apply must run through its reserved Authority service request"
        )


def _assert_same_request(existing: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    if existing.get("request_fingerprint") != request.get("request_fingerprint"):
        raise HistoricalAcceptedSourceError(
            "historical accepted source request_id was already used with different input"
        )


def _assert_apply_snapshot(
    connection: sqlite3.Connection,
    request: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    """Compare the preview binding using SQLite rows and journal bytes only."""

    parsed = parse_historical_accepted_source_binding(binding)
    if parsed is None:
        raise HistoricalAcceptedSourceError("historical accepted source binding is missing")
    task = connection.execute(
        "SELECT state, state_revision, contract_json, contract_hash FROM tasks WHERE task_id = ?",
        (request["task_id"],),
    ).fetchone()
    node = connection.execute(
        """
        SELECT state, attempt, result_json, worktree, recovery_json, spec_json
        FROM nodes WHERE task_id = ? AND node_id = ?
        """,
        (request["task_id"], request["node_id"]),
    ).fetchone()
    if task is None or node is None:
        raise KeyError((request["task_id"], request["node_id"]))
    contract = _json_mapping(task["contract_json"], "historical accepted source task contract")
    if (
        task["state"] != "blocked"
        or int(task["state_revision"]) != parsed["current_revision"]
        or task["contract_hash"] != parsed["current_contract_hash"]
        or contract.get("base_sha") != parsed["current_base_sha"]
        or contract.get("external_write_permission") is not False
        or contract.get("destructive_action_permission") is not False
    ):
        raise HistoricalAcceptedSourceError("historical accepted source task changed before queue")
    if (
        node["state"] != "blocked"
        or int(node["attempt"]) != parsed["current_attempt"]
        or node["result_json"] != parsed["current"]["result_json"]
        or node["worktree"] != parsed["current"]["worktree"]
        or node["recovery_json"] is not None
        or canonical_hash(_json_mapping(node["spec_json"], "historical accepted source node spec"))
        != parsed["current"]["spec_sha256"]
    ):
        raise HistoricalAcceptedSourceError("historical accepted source node changed before queue")
    for expected, label, attempt, result_json, required_state, worktree in (
        (
            parsed["source"], "historical accepted source", parsed["source"]["attempt"],
            parsed["source"]["result_json"], None, None,
        ),
        (
            parsed["current"], "current blocked source", parsed["current_attempt"],
            parsed["current"]["result_json"], "active", parsed["current"]["worktree"],
        ),
    ):
        allocation = connection.execute(
            "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
            (expected["allocation_id"],),
        ).fetchone()
        _validate_allocation(
            allocation,
            task_id=parsed["task_id"],
            node_id=parsed["node_id"],
            attempt=attempt,
            repository=_text(contract.get("repository"), "repository", maximum=4096),
            base_sha=parsed["current_base_sha"],
            result_json=result_json,
            required_state=required_state,
            require_worktree=worktree,
            label=label,
        )
    source_event = connection.execute(
        "SELECT event_type, task_id, node_id, payload_json FROM events WHERE cursor = ?",
        (parsed["source_event_cursor"],),
    ).fetchone()
    if (
        source_event is None
        or source_event["event_type"] != "node.accepted"
        or source_event["task_id"] != parsed["task_id"]
        or source_event["node_id"] != parsed["node_id"]
    ):
        raise HistoricalAcceptedSourceError("historical accepted event changed before queue")
    event_payload = _json_mapping(source_event["payload_json"], "historical accepted event")
    if (
        event_payload.get("attempt") != parsed["source"]["attempt"]
        or canonical_json(event_payload.get("result")) != parsed["source"]["result_json"]
    ):
        raise HistoricalAcceptedSourceError("historical accepted event result changed before queue")
    spec_rows = connection.execute(
        "SELECT node_id, spec_json FROM nodes WHERE task_id = ? ORDER BY node_id",
        (parsed["task_id"],),
    ).fetchall()
    specs = _specs(spec_rows, parsed["task_id"])
    historical_ancestors = _historical_ancestors(
        connection,
        task_id=parsed["task_id"],
        node_id=parsed["node_id"],
        source_attempt=parsed["source"]["attempt"],
        source_event_cursor=parsed["source_event_cursor"],
        specs=specs,
    )
    if canonical_json(historical_ancestors) != canonical_json(parsed["source"]["ancestors"]):
        raise HistoricalAcceptedSourceError("historical accepted ancestors changed before queue")
    current_ancestors = _current_ancestor_records(connection, parsed["task_id"], parsed["node_id"], specs)
    if canonical_json(current_ancestors) != canonical_json(parsed["current"]["ancestors"]):
        raise HistoricalAcceptedSourceError("current accepted ancestors changed before queue")
    _assert_ready_overlays_sql(connection, parsed)


def _current_ancestor_records(
    connection: sqlite3.Connection,
    task_id: str,
    node_id: str,
    specs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT node_id, state, attempt, result_json FROM nodes WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    by_id = {str(row["node_id"]): row for row in rows}
    records: list[dict[str, Any]] = []
    for ancestor_id in _ancestor_ids(specs, node_id):
        row = by_id.get(ancestor_id)
        if row is None or row["state"] != "accepted" or not isinstance(row["result_json"], str):
            raise HistoricalAcceptedSourceError("current accepted ancestor gap for " + ancestor_id)
        result = _result(row["result_json"], "current accepted ancestor result")
        records.append(
            {
                "node_id": ancestor_id,
                "attempt": _positive_int(row["attempt"], "current accepted ancestor attempt"),
                "patch_ref": _artifacts(result, "current accepted ancestor").get("patch"),
            }
        )
    return _ancestor_receipt_list(records, "current accepted ancestors")


def _assert_ready_overlays_sql(
    connection: sqlite3.Connection,
    binding: HistoricalAcceptedSourceBinding,
) -> None:
    """Require every bound ready overlay to remain the request's latest event."""

    for overlay in binding["ready_overlays"]:
        cursor = _positive_int(overlay.get("ready_event_cursor"), "ready overlay event cursor")
        row = connection.execute(
            "SELECT event_type, task_id, payload_json FROM events WHERE cursor = ?", (cursor,)
        ).fetchone()
        if row is None or row["event_type"] != "lockfile_handoff.ready" or row["task_id"] != binding["task_id"]:
            raise HistoricalAcceptedSourceError("historical accepted source ready overlay changed before queue")
        payload = _json_mapping(row["payload_json"], "historical accepted source ready overlay")
        request_id = overlay.get("request_id")
        if payload.get("state") != "ready" or payload.get("request_id") != request_id:
            raise HistoricalAcceptedSourceError("historical accepted source ready overlay changed before queue")
        latest = connection.execute(
            """
            SELECT cursor, payload_json FROM events
            WHERE task_id = ? AND event_type IN (
                'lockfile_handoff.reserved', 'lockfile_handoff.ready', 'lockfile_handoff.failed',
                'lockfile_handoff.cancelled', 'lockfile_handoff.needs_action'
            ) AND json_extract(payload_json, '$.request_id') = ?
            ORDER BY cursor DESC LIMIT 1
            """,
            (binding["task_id"], request_id),
        ).fetchone()
        if latest is None or int(latest["cursor"]) != cursor:
            raise HistoricalAcceptedSourceError("historical accepted source ready overlay changed before queue")


def _business_request(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": HISTORICAL_ACCEPTED_SOURCE_KIND,
        "task_id": request["task_id"],
        "node_id": request["node_id"],
        "accepted_event_cursor": request["accepted_event_cursor"],
        "expected_attempt": request["expected_attempt"],
        "expected_revision": request["expected_revision"],
        "expected_contract_hash": request["expected_contract_hash"],
        "request_id": request["request_id"],
    }


def _assert_no_active_work_or_recovery(
    connection: sqlite3.Connection, task_id: str, request_id: str
) -> None:
    approval = connection.execute(
        "SELECT 1 FROM approvals WHERE task_id = ? AND decision IS NULL LIMIT 1", (task_id,)
    ).fetchone()
    if approval is not None:
        raise HistoricalAcceptedSourceError("historical accepted source is fenced by a pending approval")
    active = connection.execute(
        """
        SELECT node_id, state FROM nodes
        WHERE task_id = ? AND state IN ('running', 'indeterminate')
        ORDER BY node_id LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if active is not None:
        raise HistoricalAcceptedSourceError(
            "historical accepted source is fenced by active node "
            + str(active["node_id"]) + ":" + str(active["state"])
        )
    recovery = connection.execute(
        "SELECT node_id FROM nodes WHERE task_id = ? AND recovery_json IS NOT NULL LIMIT 1", (task_id,)
    ).fetchone()
    if recovery is not None:
        raise HistoricalAcceptedSourceError(
            "historical accepted source is fenced by pending recovery on node " + str(recovery["node_id"])
        )
    journal = connection.execute(
        """
        SELECT request_id FROM authority_requests
        WHERE task_id = ? AND state = 'executing' AND request_id != ? LIMIT 1
        """,
        (task_id, request_id),
    ).fetchone()
    if journal is not None:
        raise HistoricalAcceptedSourceError(
            "historical accepted source is fenced by another Authority mutation"
        )


def _historical_ancestors(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    node_id: str,
    source_attempt: int,
    source_event_cursor: int,
    specs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    start = _source_started_event(
        connection, task_id=task_id, node_id=node_id, attempt=source_attempt,
        before_cursor=source_event_cursor,
    )
    ancestor_ids = _ancestor_ids(specs, node_id)
    sources: dict[str, dict[str, Any]] = {}
    for ancestor_id in ancestor_ids:
        event = _latest_terminal_event_before(
            connection, task_id=task_id, node_id=ancestor_id, before_cursor=start
        )
        if event is None or event["event_type"] != "node.accepted":
            raise HistoricalAcceptedSourceError(
                "historical accepted ancestor gap for " + ancestor_id
            )
        payload = _json_mapping(event["payload_json"], "historical accepted ancestor event")
        attempt = _positive_int(payload.get("attempt"), "historical accepted ancestor attempt")
        result_raw = payload.get("result")
        if not isinstance(result_raw, Mapping):
            raise HistoricalAcceptedSourceError(
                "historical accepted ancestor has no result"
            )
        result = _result(canonical_json(dict(result_raw)), "historical accepted ancestor result")
        if result.get("status") != "succeeded":
            raise HistoricalAcceptedSourceError("historical accepted ancestor result is not succeeded")
        sources[ancestor_id] = {
            "attempt": attempt,
            "patch_ref": _artifacts(result, "historical accepted ancestor").get("patch"),
        }
    return _ancestor_receipt_list(
        [
            {
                "node_id": ancestor_id,
                "attempt": sources[ancestor_id]["attempt"],
                "patch_ref": sources[ancestor_id]["patch_ref"],
            }
            for ancestor_id in ancestor_ids
        ],
        "historical accepted ancestors",
    )


def _historical_dependency_input(
    store: WorkbenchStore,
    raw_ref: object,
    *,
    task_id: str,
    node_id: str,
    base_sha: str,
    ancestors: list[dict[str, Any]],
) -> tuple[DependencyInput | None, str | None]:
    ref = None if raw_ref is None else _text(raw_ref, "historical dependency input ref", maximum=512)
    if ref is None:
        if ancestors:
            raise HistoricalAcceptedSourceError(
                "historical accepted source dependency input is missing"
            )
        return None, None
    try:
        dependency_input = load_recorded_dependency_input(
            store.artifacts, ref, task_id=task_id, node_id=node_id, base_sha=base_sha
        )
    except (DependencyInputError, ValueError) as error:
        raise HistoricalAcceptedSourceError(
            f"historical accepted source dependency input is invalid: {error}"
        ) from error
    if canonical_json(dependency_input.receipt["ancestors"]) != canonical_json(ancestors):
        raise HistoricalAcceptedSourceError(
            "historical accepted source dependency input does not match ancestors as of its event"
        )
    return dependency_input, ref


def _current_dependency_input(
    store: WorkbenchStore,
    *,
    current_result: Mapping[str, Any],
    task: Mapping[str, Any],
    node_id: str,
    base_sha: str,
    worktree: Path,
    dependency_input_ref: object,
) -> tuple[DependencyInput, str | None, bool]:
    ref = None if dependency_input_ref is None else _text(
        dependency_input_ref, "current dependency input ref", maximum=512
    )
    try:
        if ref is None:
            dependency_input = reconstruct_prepared_dependency_input(
                task, node_id, worktree, store.artifacts
            )
            legacy = True
        else:
            dependency_input = load_recorded_dependency_input(
                store.artifacts, ref, task_id=str(task["task_id"]), node_id=node_id,
                base_sha=base_sha,
            )
            legacy = False
        validate_dependency_input_lineage(
            task, node_id, dependency_input, artifacts=store.artifacts
        )
    except (DependencyInputError, ValueError) as error:
        raise HistoricalAcceptedSourceError(
            f"current blocked source dependency input is invalid: {error}"
        ) from error
    return dependency_input, ref, legacy


def _source_binding(
    raw: object, task_id: str, node_id: str, base_sha: str
) -> HistoricalAcceptedSourceSource:
    value = _mapping_with_fields(
        raw,
        {
            "attempt", "result_json", "result_sha256", "allocation_id", "worktree", "branch",
            "base_sha", "patch_ref", "patch_sha256", "dependency_input_ref", "dependency_input", "ancestors",
        },
        "historical accepted source",
    )
    attempt = _positive_int(value["attempt"], "historical accepted source attempt")
    result_json = _result_json(value["result_json"], "historical accepted source result")
    result = _result(result_json, "historical accepted source result")
    if result.get("status") != "succeeded":
        raise HistoricalAcceptedSourceError("historical accepted source result is not succeeded")
    result_sha256 = _digest(value["result_sha256"], "historical accepted source result_sha256")
    if sha256(result_json.encode()).hexdigest() != result_sha256:
        raise HistoricalAcceptedSourceError("historical accepted source result hash is invalid")
    artifacts = _artifacts(result, "historical accepted source")
    patch_ref = _text(value["patch_ref"], "historical accepted source patch_ref", maximum=512)
    if artifacts.get("patch") != patch_ref:
        raise HistoricalAcceptedSourceError("historical accepted source patch does not match result")
    dependency_input_ref = value["dependency_input_ref"]
    if dependency_input_ref is not None:
        dependency_input_ref = _text(dependency_input_ref, "historical dependency input ref", maximum=512)
    if artifacts.get("dependency-input") != dependency_input_ref:
        raise HistoricalAcceptedSourceError("historical accepted source dependency input does not match result")
    dependency_input = value["dependency_input"]
    if dependency_input is not None and not isinstance(dependency_input, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source dependency receipt is invalid")
    ancestors = _ancestor_receipt_list(value["ancestors"], "historical accepted ancestors")
    if dependency_input is None:
        if dependency_input_ref is not None or ancestors:
            raise HistoricalAcceptedSourceError("historical accepted source dependency receipt is missing")
    else:
        _dependency_receipt_shape(dependency_input, task_id, node_id, base_sha)
        if canonical_json(dependency_input.get("ancestors")) != canonical_json(ancestors):
            raise HistoricalAcceptedSourceError("historical accepted source ancestor receipt is inconsistent")
    source_base = _text(value["base_sha"], "historical accepted source base_sha", maximum=512)
    if source_base != base_sha:
        raise HistoricalAcceptedSourceError("historical accepted source base does not match current base")
    return {
        "attempt": attempt,
        "result_json": result_json,
        "result_sha256": result_sha256,
        "allocation_id": _text(value["allocation_id"], "historical accepted source allocation_id", maximum=512),
        "worktree": _text(value["worktree"], "historical accepted source worktree", maximum=4096),
        "branch": _text(value["branch"], "historical accepted source branch", maximum=512),
        "base_sha": source_base,
        "patch_ref": patch_ref,
        "patch_sha256": _digest(value["patch_sha256"], "historical accepted source patch_sha256"),
        "dependency_input_ref": dependency_input_ref,
        "dependency_input": dict(dependency_input) if isinstance(dependency_input, Mapping) else None,
        "ancestors": ancestors,
    }


def _current_binding(
    raw: object, task_id: str, node_id: str, base_sha: str
) -> HistoricalAcceptedSourceCurrent:
    value = _mapping_with_fields(
        raw,
        {
            "attempt", "result_json", "result_sha256", "allocation_id", "worktree", "branch",
            "base_sha", "dependency_input_ref", "dependency_input", "ancestors", "input_tree_sha",
            "source_delta_sha256", "legacy_reconstructed", "spec_sha256",
        },
        "historical accepted source current",
    )
    attempt = _positive_int(value["attempt"], "historical accepted source current attempt")
    result_json = _result_json(value["result_json"], "historical accepted source current result")
    result = _result(result_json, "historical accepted source current result")
    if result.get("status") != "blocked":
        raise HistoricalAcceptedSourceError("historical accepted source current result is not blocked")
    result_sha256 = _digest(value["result_sha256"], "historical accepted source current result_sha256")
    if sha256(result_json.encode()).hexdigest() != result_sha256:
        raise HistoricalAcceptedSourceError("historical accepted source current result hash is invalid")
    dependency_input_ref = value["dependency_input_ref"]
    if dependency_input_ref is not None:
        dependency_input_ref = _text(dependency_input_ref, "current dependency input ref", maximum=512)
    if _artifacts(result, "historical accepted source current").get("dependency-input") != dependency_input_ref:
        raise HistoricalAcceptedSourceError("historical accepted source current dependency input does not match result")
    dependency_input = value["dependency_input"]
    if not isinstance(dependency_input, Mapping):
        raise HistoricalAcceptedSourceError("historical accepted source current dependency receipt is invalid")
    _dependency_receipt_shape(dependency_input, task_id, node_id, base_sha)
    ancestors = _ancestor_receipt_list(value["ancestors"], "current accepted ancestors")
    if canonical_json(dependency_input.get("ancestors")) != canonical_json(ancestors):
        raise HistoricalAcceptedSourceError("current accepted ancestor receipt is inconsistent")
    input_tree_sha = _text(value["input_tree_sha"], "current input_tree_sha", maximum=512)
    if dependency_input.get("input_tree_sha") != input_tree_sha:
        raise HistoricalAcceptedSourceError("current dependency input tree is inconsistent")
    if value["legacy_reconstructed"] is not True and value["legacy_reconstructed"] is not False:
        raise HistoricalAcceptedSourceError("current legacy reconstruction marker is invalid")
    current_base = _text(value["base_sha"], "current base_sha", maximum=512)
    if current_base != base_sha:
        raise HistoricalAcceptedSourceError("current source base does not match binding")
    return {
        "attempt": attempt,
        "result_json": result_json,
        "result_sha256": result_sha256,
        "allocation_id": _text(value["allocation_id"], "current allocation_id", maximum=512),
        "worktree": _text(value["worktree"], "current worktree", maximum=4096),
        "branch": _text(value["branch"], "current branch", maximum=512),
        "base_sha": current_base,
        "dependency_input_ref": dependency_input_ref,
        "dependency_input": dict(dependency_input),
        "ancestors": ancestors,
        "input_tree_sha": input_tree_sha,
        "source_delta_sha256": _digest(value["source_delta_sha256"], "current source_delta_sha256"),
        "legacy_reconstructed": bool(value["legacy_reconstructed"]),
        "spec_sha256": _digest(value["spec_sha256"], "current spec_sha256"),
    }


def _overlays(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise HistoricalAcceptedSourceError("historical accepted source ready overlays are invalid")
    result: list[dict[str, Any]] = []
    cursors: list[int] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise HistoricalAcceptedSourceError("historical accepted source ready overlay is invalid")
        cursor = _positive_int(item.get("ready_event_cursor"), "ready overlay event cursor")
        cursors.append(cursor)
        result.append(dict(item))
    if cursors != sorted(set(cursors)):
        raise HistoricalAcceptedSourceError("historical accepted source ready overlays are not ordered")
    return result


def _assignment(
    raw: object, *, task_id: str, node_id: str, next_attempt: int
) -> dict[str, Any]:
    value = _mapping_with_fields(
        raw,
        {"target_attempt", "target_worktree", "target_branch", "assigned_at"},
        "historical accepted source assignment",
    )
    target_attempt = _positive_int(value["target_attempt"], "historical accepted source target attempt")
    if target_attempt != next_attempt:
        raise HistoricalAcceptedSourceError("historical accepted source assignment attempt is stale")
    target_worktree = _text(value["target_worktree"], "historical accepted source target worktree", maximum=4096)
    target_branch = _text(value["target_branch"], "historical accepted source target branch", maximum=512)
    if target_branch != WorktreeManager.branch_name(task_id, node_id, target_attempt):
        raise HistoricalAcceptedSourceError("historical accepted source assignment branch is invalid")
    return {
        "target_attempt": target_attempt,
        "target_worktree": target_worktree,
        "target_branch": target_branch,
        "assigned_at": _text(value["assigned_at"], "historical accepted source assigned_at", maximum=64),
    }


def _authorization(binding: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(binding)
    result["state"] = "authorized"
    result.pop("assignment", None)
    return result


def _preparation_snapshot(
    store: WorkbenchStore,
    binding: HistoricalAcceptedSourceBinding,
    target_attempt: int,
) -> dict[str, Any]:
    with store.connection() as connection:
        task = connection.execute(
            "SELECT state, contract_json, contract_hash FROM tasks WHERE task_id = ?", (binding["task_id"],)
        ).fetchone()
        node = connection.execute(
            """
            SELECT state, attempt, worktree, recovery_json, spec_json
            FROM nodes WHERE task_id = ? AND node_id = ?
            """,
            (binding["task_id"], binding["node_id"]),
        ).fetchone()
        current_allocation = connection.execute(
            "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
            (binding["current"]["allocation_id"],),
        ).fetchone()
        source_allocation = connection.execute(
            "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
            (binding["source"]["allocation_id"],),
        ).fetchone()
        target_allocation = connection.execute(
            """
            SELECT allocation_id FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ?
            """,
            (binding["task_id"], binding["node_id"], target_attempt),
        ).fetchone()
    if task is None or node is None:
        raise KeyError((binding["task_id"], binding["node_id"]))
    if task["state"] in {"paused", "cancelled"}:
        raise HistoricalAcceptedSourceError("historical accepted source target was paused or cancelled")
    contract = _json_mapping(task["contract_json"], "historical accepted source task contract")
    if (
        task["contract_hash"] != binding["current_contract_hash"]
        or contract.get("base_sha") != binding["current_base_sha"]
    ):
        raise HistoricalAcceptedSourceError("historical accepted source contract changed after authorization")
    if node["state"] != "running" or int(node["attempt"]) != target_attempt or node["worktree"] is not None:
        raise HistoricalAcceptedSourceError("historical accepted source target lease is stale")
    if canonical_hash(_json_mapping(node["spec_json"], "historical accepted source target spec")) != binding["current"]["spec_sha256"]:
        raise HistoricalAcceptedSourceError("historical accepted source target scope changed after authorization")
    current = parse_historical_accepted_source_binding(
        node["recovery_json"], next_attempt=target_attempt
    )
    if current is None or canonical_json(current) != canonical_json(binding):
        raise HistoricalAcceptedSourceError("historical accepted source target binding changed before preparation")
    _validate_allocation(
        current_allocation, task_id=binding["task_id"], node_id=binding["node_id"],
        attempt=binding["current_attempt"], repository=_text(contract.get("repository"), "repository", maximum=4096),
        base_sha=binding["current_base_sha"], result_json=binding["current"]["result_json"],
        required_state="active", require_worktree=binding["current"]["worktree"], label="current blocked source",
    )
    _validate_allocation(
        source_allocation, task_id=binding["task_id"], node_id=binding["node_id"],
        attempt=binding["source"]["attempt"], repository=_text(contract.get("repository"), "repository", maximum=4096),
        base_sha=binding["current_base_sha"], result_json=binding["source"]["result_json"],
        required_state=None, require_worktree=None, label="historical accepted source",
    )
    if target_allocation is not None:
        raise HistoricalAcceptedSourceError("historical accepted source target allocation already exists")
    if canonical_json(get_ready_lockfile_handoffs(store, binding["task_id"])) != canonical_json(binding["ready_overlays"]):
        raise HistoricalAcceptedSourceError("historical accepted source ready overlays changed")
    return {
        "repository": _text(contract.get("repository"), "repository", maximum=4096),
        "contract_hash": str(task["contract_hash"]),
        "current_allocation_id": binding["current"]["allocation_id"],
        "source_allocation_id": binding["source"]["allocation_id"],
        "target_attempt": target_attempt,
    }


def _validate_allocation(
    allocation: sqlite3.Row | None,
    *,
    task_id: str,
    node_id: str,
    attempt: int,
    repository: str,
    base_sha: str,
    result_json: str,
    required_state: str | None,
    require_worktree: str | None,
    label: str,
) -> None:
    if allocation is None:
        raise HistoricalAcceptedSourceError(label + " allocation is missing")
    expected_branch = WorktreeManager.branch_name(task_id, node_id, attempt)
    allowed_states = {"active"} if required_state == "active" else {"active", "superseded"}
    if (
        allocation["state"] not in allowed_states
        or allocation["repository"] != repository
        or allocation["base_sha"] != base_sha
        or allocation["branch"] != expected_branch
        or int(allocation["attempt"]) != attempt
        or allocation["node_result_json"] != result_json
        or not isinstance(allocation["current_path"], str)
        or not allocation["current_path"]
        or require_worktree is not None and allocation["current_path"] != require_worktree
    ):
        raise HistoricalAcceptedSourceError(label + " allocation does not match its receipt")


def _validate_current_source_worktree(
    *,
    repository: str,
    worktree: Path,
    branch: str,
    base_sha: str,
    dependency_input: DependencyInput,
) -> None:
    try:
        source = worktree.expanduser().resolve(strict=True)
        repository_path = Path(repository).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HistoricalAcceptedSourceError("current blocked source worktree is unavailable") from error
    if _git_text(source, "rev-parse", "HEAD") != _git_text(repository_path, "rev-parse", f"{base_sha}^{{commit}}"):
        raise HistoricalAcceptedSourceError("current blocked source is not at the contract base")
    if _git_text(source, "branch", "--show-current") != branch:
        raise HistoricalAcceptedSourceError("current blocked source branch changed")
    if _git_text(source, "rev-parse", "--path-format=absolute", "--git-common-dir") != _git_text(
        repository_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ):
        raise HistoricalAcceptedSourceError("current blocked source belongs to another repository")
    try:
        changed_paths = changed_paths_since_input_tree(source, dependency_input.input_tree_sha)
    except DependencyInputError as error:
        raise HistoricalAcceptedSourceError(
            f"current blocked source input cannot be inspected: {error}"
        ) from error
    unsafe_ignored = tuple(
        path for path in _ignored_paths(source) if not _generated_dependency_or_cache_path(path)
    )
    if changed_paths or unsafe_ignored:
        raise HistoricalAcceptedSourceError(
            "current blocked source has a local source delta; restoration will not drop it"
        )


def _validate_restored_patch_scope(
    task: Mapping[str, Any],
    node_id: str,
    worktree: Path,
    dependency_input: DependencyInput,
) -> None:
    """Require the actual restored delta to remain inside current write scope."""

    contract = task.get("contract")
    nodes = task.get("nodes")
    if not isinstance(contract, Mapping) or not isinstance(nodes, list):
        raise HistoricalAcceptedSourceError("historical accepted source current task is invalid")
    node = next(
        (candidate for candidate in nodes if isinstance(candidate, Mapping) and candidate.get("node_id") == node_id),
        None,
    )
    if node is None:
        raise HistoricalAcceptedSourceError("historical accepted source current node is missing")
    allowed_scope = contract.get("allowed_scope")
    forbidden_scope = contract.get("forbidden_scope")
    write_scopes = node.get("write_scopes")
    if not all(isinstance(value, list) for value in (allowed_scope, forbidden_scope, write_scopes)):
        raise HistoricalAcceptedSourceError("historical accepted source scope metadata is invalid")
    if not all(
        isinstance(path, str)
        for values in (allowed_scope, forbidden_scope, write_scopes)
        for path in values
    ):
        raise HistoricalAcceptedSourceError("historical accepted source scope metadata is invalid")
    try:
        restored = changed_paths_since_input_tree(worktree, dependency_input.input_tree_sha)
        for path in restored:
            if not scope_allows(path, allowed_scope, forbidden_scope):
                raise HistoricalAcceptedSourceError(
                    "historical accepted source patch is outside current task scope: " + path
                )
            if not scope_allows(path, write_scopes, []):
                raise HistoricalAcceptedSourceError(
                    "historical accepted source patch is outside current node write scope: " + path
                )
    except ValueError as error:
        if isinstance(error, HistoricalAcceptedSourceError):
            raise
        raise HistoricalAcceptedSourceError("historical accepted source scope metadata is invalid") from error


def _source_started_event(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    node_id: str,
    attempt: int,
    before_cursor: int,
) -> int:
    rows = connection.execute(
        """
        SELECT cursor, payload_json FROM events
        WHERE task_id = ? AND node_id = ? AND event_type = 'node.started' AND cursor < ?
        ORDER BY cursor DESC
        """,
        (task_id, node_id, before_cursor),
    ).fetchall()
    for row in rows:
        payload = _json_mapping(row["payload_json"], "historical source started event")
        if payload.get("attempt") == attempt:
            return int(row["cursor"])
    raise HistoricalAcceptedSourceError("historical accepted source has no matching started event")


def _latest_terminal_event_before(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    node_id: str,
    before_cursor: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT cursor, event_type, payload_json FROM events
        WHERE task_id = ? AND node_id = ? AND cursor < ?
          AND event_type IN ('node.started', 'node.accepted', 'node.failed', 'node.blocked', 'node.indeterminate')
        ORDER BY cursor DESC LIMIT 1
        """,
        (task_id, node_id, before_cursor),
    ).fetchone()


def _ancestor_ids(
    specs: Mapping[str, Mapping[str, Any]], node_id: str
) -> tuple[str, ...]:
    target = specs.get(node_id)
    if target is None:
        raise HistoricalAcceptedSourceError("historical accepted source node specification is missing")
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(candidate_id: str) -> None:
        if candidate_id in visited:
            return
        if candidate_id in visiting:
            raise HistoricalAcceptedSourceError("historical accepted source task graph contains a cycle")
        candidate = specs.get(candidate_id)
        if candidate is None:
            raise HistoricalAcceptedSourceError("historical accepted source dependency is missing")
        visiting.add(candidate_id)
        dependencies = candidate.get("depends_on")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ) or len(set(dependencies)) != len(dependencies):
            raise HistoricalAcceptedSourceError("historical accepted source dependencies are invalid")
        for dependency_id in dependencies:
            visit(dependency_id)
        visiting.remove(candidate_id)
        visited.add(candidate_id)
        ordered.append(candidate_id)

    dependencies = target.get("depends_on")
    if not isinstance(dependencies, list):
        raise HistoricalAcceptedSourceError("historical accepted source dependencies are invalid")
    for dependency_id in dependencies:
        visit(dependency_id)
    return tuple(ordered)


def _specs(rows: Sequence[sqlite3.Row], task_id: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        node_id = _text(row["node_id"], "historical accepted source specification node_id")
        spec = _json_mapping(row["spec_json"], "historical accepted source specification")
        if spec.get("task_id") != task_id or spec.get("node_id") != node_id or node_id in result:
            raise HistoricalAcceptedSourceError("historical accepted source specifications are invalid")
        result[node_id] = spec
    return result


def _artifact_bytes(
    store: WorkbenchStore,
    ref: str,
    expected_sha256: str | None,
    label: str,
) -> bytes:
    try:
        data = store.artifacts.verify(ref).read_bytes()
    except (OSError, ValueError) as error:
        raise HistoricalAcceptedSourceError(label + " is unavailable or invalid") from error
    digest = sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise HistoricalAcceptedSourceError(label + " hash does not match its binding")
    return data


def _artifacts(result: Mapping[str, Any], label: str) -> dict[str, str]:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping) or not all(
        isinstance(key, str) and key and isinstance(value, str) and value
        for key, value in artifacts.items()
    ):
        raise HistoricalAcceptedSourceError(label + " artifacts are invalid")
    return dict(artifacts)


def _result(raw: object, label: str) -> dict[str, Any]:
    value = _json_mapping(raw, label)
    if value.get("status") not in {"succeeded", "failed", "blocked", "indeterminate"}:
        raise HistoricalAcceptedSourceError(label + " status is invalid")
    _artifacts(value, label)
    changed_paths = value.get("changed_paths")
    if (
        not isinstance(changed_paths, list)
        or any(not isinstance(path, str) or not path for path in changed_paths)
        or changed_paths != sorted(set(changed_paths))
    ):
        raise HistoricalAcceptedSourceError(label + " changed paths are invalid")
    return value


def _result_json(raw: object, label: str) -> str:
    if not isinstance(raw, str):
        raise HistoricalAcceptedSourceError(label + " is invalid")
    value = _result(raw, label)
    normalized = canonical_json(value)
    if normalized != raw:
        raise HistoricalAcceptedSourceError(label + " is not canonical")
    return normalized


def _dependency_receipt_shape(
    raw: Mapping[str, Any], task_id: str, node_id: str, base_sha: str
) -> None:
    schema = raw.get("schema_version")
    required = {
        "schema_version", "kind", "task_id", "node_id", "contract_base_sha", "input_tree_sha", "ancestors",
    }
    if schema == 2:
        required.add("lockfile_handoffs")
    if schema not in {1, 2} or set(raw) != required:
        raise HistoricalAcceptedSourceError("dependency input receipt has an invalid shape")
    if (
        raw.get("kind") != "accepted-ancestor-patch-input"
        or raw.get("task_id") != task_id
        or raw.get("node_id") != node_id
        or raw.get("contract_base_sha") != base_sha
        or not isinstance(raw.get("input_tree_sha"), str)
        or not raw["input_tree_sha"]
    ):
        raise HistoricalAcceptedSourceError("dependency input receipt is invalid")
    _ancestor_receipt_list(raw.get("ancestors"), "dependency input ancestors")
    if schema == 2 and (
        isinstance(raw.get("lockfile_handoffs"), (str, bytes))
        or not isinstance(raw.get("lockfile_handoffs"), list)
        or not raw["lockfile_handoffs"]
    ):
        raise HistoricalAcceptedSourceError("dependency input lockfile handoffs are invalid")


def _ancestor_receipt_list(raw: object, label: str) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise HistoricalAcceptedSourceError(label + " are invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"node_id", "attempt", "patch_ref"}:
            raise HistoricalAcceptedSourceError(label + " entry is invalid")
        node_id = _text(item["node_id"], label + " node_id")
        if node_id in seen:
            raise HistoricalAcceptedSourceError(label + " duplicate node")
        seen.add(node_id)
        patch_ref = item["patch_ref"]
        if patch_ref is not None:
            patch_ref = _text(patch_ref, label + " patch_ref", maximum=512)
        result.append({"node_id": node_id, "attempt": _positive_int(item["attempt"], label + " attempt"), "patch_ref": patch_ref})
    return result


def _ignored_paths(worktree: Path) -> set[str]:
    data = _git_bytes(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    return {item.decode(errors="surrogateescape") for item in data.split(b"\0") if item}


def _generated_dependency_or_cache_path(path: str) -> bool:
    """Keep generated dependency and bytecode residue out of source-delta proof."""

    if is_python_bytecode_residue_path(path):
        return True
    try:
        relative = PurePosixPath(path)
    except TypeError:
        return False
    return (
        bool(path)
        and not relative.is_absolute()
        and ".." not in relative.parts
        and "node_modules" in relative.parts
    )


def _git_text(worktree: Path, *arguments: str) -> str:
    return _git_bytes(worktree, *arguments).decode(errors="replace").strip()


def _git_bytes(worktree: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HistoricalAcceptedSourceError(
            f"historical accepted source Git inspection failed: {error}"
        ) from error
    if result.returncode:
        raise HistoricalAcceptedSourceError(
            result.stderr.decode(errors="replace").strip()
            or result.stdout.decode(errors="replace").strip()
            or "historical accepted source Git inspection failed"
        )
    return result.stdout


def _mapping_with_fields(raw: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise HistoricalAcceptedSourceError(label + " has an invalid shape")
    return raw


def _json_mapping(raw: object, label: str) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HistoricalAcceptedSourceError(label + " is invalid JSON") from error
    else:
        value = raw
    if not isinstance(value, Mapping):
        raise HistoricalAcceptedSourceError(label + " is invalid")
    return dict(value)


def _text(raw: object, label: str, *, maximum: int = _MAX_TEXT) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or len(raw) > maximum
        or any(character in raw for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(label + " must be a bounded non-empty string")
    return raw


def _positive_int(raw: object, label: str) -> int:
    if type(raw) is not int or raw < 1:
        raise ValueError(label + " must be a positive integer")
    return raw


def _digest(raw: object, label: str) -> str:
    if not isinstance(raw, str) or _SHA256.fullmatch(raw) is None:
        raise ValueError(label + " must be a lowercase SHA-256 digest")
    return raw


__all__ = [
    "HISTORICAL_ACCEPTED_SOURCE_KIND",
    "HistoricalAcceptedSourceError",
    "PreparedHistoricalAcceptedSource",
    "assert_historical_accepted_source_dispatch",
    "historical_accepted_source",
    "parse_historical_accepted_source_binding",
    "prepare_historical_accepted_source",
]
