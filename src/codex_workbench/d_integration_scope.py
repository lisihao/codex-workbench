"""Atomically widen one fixed blocked DSH integration lane's declared scopes.

This is deliberately not a generic scope editor.  It binds the literal
``A -> B -> C -> D -> E`` recovery lane, reads the retained D worktree before
opening its short write transaction, and changes only the uncovered fixed
scope entries.  The Authority request journal owns external idempotency; a
direct repeat is rejected by the task revision compare-and-set.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any

from . import acceptance_amendment
from .config import WorkbenchConfig
from .d_integration_profile import (
    NODE_READ_ADDITIONS,
    NODE_WRITE_ADDITIONS,
    REQUIRED_PACKAGE_MARKERS,
    SCOPE_PROFILE_ID,
    TASK_ACCESS_ADDITIONS,
)
from .dirty_worktree_recovery import DirtyWorktreeRecoveryError
from .model import canonical_hash, canonical_json, now_iso
from .recovery_processes import RecoveryProcessError, assert_recovery_source_idle
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeManager


ACTION_NAME = "amend_blocked_integration_scope"
_EXPECTED_NODE_IDS = ("A", "B", "C", "D", "E")
_EXPECTED_DEPENDENCIES = {
    "A": (),
    "B": ("A",),
    "C": ("B",),
    "D": ("A", "B", "C"),
    "E": ("A", "B", "C", "D"),
}
_EXPECTED_ORDINALS = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REASON_LENGTH = 500
_EVENT_TYPE = "task.blocked_integration_scope_amended"


_ARGUMENT_SCHEMA = {
    "additionalProperties": False,
    "required": [
        "task_id",
        "node_id",
        "expected_revision",
        "expected_attempt",
        "expected_contract_hash",
        "profile_id",
        "reason",
        "dry_run",
    ],
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "node_id": {"const": "D"},
        "expected_revision": {"type": "integer", "minimum": 0},
        "expected_attempt": {"type": "integer", "minimum": 1},
        "expected_contract_hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "profile_id": {"const": SCOPE_PROFILE_ID},
        "reason": {"type": "string", "minLength": 1, "maxLength": _MAX_REASON_LENGTH},
        "dry_run": {"type": "boolean"},
        "expected_fingerprint": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
    },
}


class IntegrationScopeAmendmentError(ValueError):
    """The fixed scope amendment cannot be safely constructed."""


def amend_blocked_integration_scope(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    raw_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Preview or atomically record the fixed D/E integration scope coverage.

    Args:
        config: Authority configuration retained for the public control-action
            signature. This amendment has no runtime or source-write command.
        store: Durable Workbench task-state authority.
        raw_arguments: Strict task identity, expected state, and preview/CAS fields.

    Returns:
        A read-only preview or the compare-and-set amendment receipt.
    """

    # The configuration belongs in the public action signature.  Scope planning
    # deliberately has no configurable command or runtime selection.
    del config
    arguments = _arguments(raw_arguments)
    preflight = _preflight(store, arguments)
    if arguments["dry_run"]:
        return _response(
            arguments,
            preflight,
            dry_run=True,
            revision=int(arguments["expected_revision"]) + 1,
        )
    if preflight["fingerprint"] != arguments["expected_fingerprint"]:
        raise StateConflictError(
            "integration scope amendment fingerprint changed; obtain a fresh preview"
        )
    return _apply(store, arguments, preflight)


def _arguments(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the explicit identity and preview/CAS fields without defaults."""

    if not isinstance(raw, Mapping):
        raise ValueError("integration scope amendment arguments must be an object")
    properties = set(_ARGUMENT_SCHEMA["properties"])
    required = set(_ARGUMENT_SCHEMA["required"])
    supplied = set(raw)
    if supplied - properties or required - supplied:
        raise ValueError(
            "integration scope amendment accepts only its explicit identity and preview fields"
        )
    values = dict(raw)
    for key in ("task_id", "node_id", "reason"):
        value = values[key]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{key} must be a non-empty string without outer whitespace")
    if values["node_id"] != "D":
        raise ValueError("node_id must be the fixed blocked integration node 'D'")
    if len(values["reason"]) > _MAX_REASON_LENGTH:
        raise ValueError(f"reason must be at most {_MAX_REASON_LENGTH} characters")
    for key, minimum in (("expected_revision", 0), ("expected_attempt", 1)):
        if type(values[key]) is not int or values[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    _digest(values["expected_contract_hash"], "expected_contract_hash")
    if values["profile_id"] != SCOPE_PROFILE_ID:
        raise ValueError(f"profile_id must be {SCOPE_PROFILE_ID!r}")
    if type(values["dry_run"]) is not bool:
        raise ValueError("dry_run must be a boolean")
    fingerprint = values.get("expected_fingerprint")
    if fingerprint is not None:
        _digest(fingerprint, "expected_fingerprint")
    if not values["dry_run"] and fingerprint is None:
        raise ValueError("apply requires expected_fingerprint from a fresh preview")
    return values


def _preflight(store: WorkbenchStore, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Bind durable state and retained source without opening a write transaction."""

    # Keep the native blocked/active-allocation/approval/recovery/validation
    # fence visible as a separate snapshot.  The richer binding below repeats
    # it while adding the fixed A-E lane checks.
    native_before = acceptance_amendment._snapshot(store, arguments)
    before = _durable_binding(store, arguments)
    if canonical_json(native_before) != canonical_json(before["candidate"]):
        raise StateConflictError("integration scope amendment native binding changed during preview")

    retained_e_before = _retained_e_worktree_identity(before)
    source_before, delta_before = _source_snapshot(store, arguments)
    _assert_source_matches_binding(source_before, before)
    source_identity = _source_identity(before, source_before)

    # Source inspection is intentionally outside a SQL write transaction.  A
    # second source snapshot detects source, result, allocation, or dependency
    # input drift while marker files and Git identity were read.
    source_after, delta_after = _source_snapshot(store, arguments)
    if canonical_json(source_before) != canonical_json(source_after) or delta_before != delta_after:
        raise StateConflictError("integration scope amendment source changed during preview")

    after = _durable_binding(store, arguments)
    native_after = acceptance_amendment._snapshot(store, arguments)
    if canonical_json(native_after) != canonical_json(after["candidate"]):
        raise StateConflictError("integration scope amendment native binding changed during preview")
    if canonical_json(before) != canonical_json(after):
        raise StateConflictError("integration scope amendment durable binding changed during preview")
    retained_e_after = _retained_e_worktree_identity(after)
    if canonical_json(retained_e_before) != canonical_json(retained_e_after):
        raise StateConflictError("integration scope amendment retained verifier identity changed during preview")

    plan = _plan(before)
    source = {
        "allocation": dict(before["d"]["allocation"]),
        "worktree_identity": source_identity,
        "source_delta": _delta_payload(delta_before),
        "retained_verifier": retained_e_before,
    }
    fingerprint = canonical_hash(
        {
            "schema_version": 1,
            "kind": "d-integration-scope-amendment-v1",
            "profile_id": SCOPE_PROFILE_ID,
            "task_id": arguments["task_id"],
            "node_id": arguments["node_id"],
            "expected_revision": arguments["expected_revision"],
            "expected_attempt": arguments["expected_attempt"],
            "expected_contract_hash": arguments["expected_contract_hash"],
            "reason": arguments["reason"],
            "binding": before,
            "plan": _fingerprint_plan(plan),
            "source": source,
        }
    )
    return {
        "binding": before,
        "plan": plan,
        "source": source,
        "fingerprint": fingerprint,
    }


def _durable_binding(
    store: WorkbenchStore,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Return every SQL value that the later CAS must keep fixed."""

    with store.connection() as connection:
        return _durable_binding_from_connection(store, connection, arguments)


def _durable_binding_from_connection(
    store: WorkbenchStore,
    connection: Any,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the fixed A-E durable binding from one SQLite read snapshot."""

    candidate = acceptance_amendment._candidate(store, connection, arguments)
    task = candidate["task"]
    contract = task["contract"]
    if not isinstance(contract, dict):
        raise StateConflictError("integration scope amendment task contract is invalid")
    if canonical_hash(contract) != task["contract_hash"]:
        raise StateConflictError("integration scope amendment task contract hash is invalid")
    allowed_scope = _scope_list(contract.get("allowed_scope"), "task allowed_scope", nonempty=True)
    forbidden_scope = _scope_list(contract.get("forbidden_scope", []), "task forbidden_scope")
    _validate_fixed_profile_paths()

    rows = connection.execute(
        """
        SELECT task_id, node_id, spec_json, state, attempt, worker_id, worktree,
               effective_executor, effective_model, started_at, settled_at,
               result_json, recovery_json, coordinator_epoch, lease_epoch, updated_at
        FROM nodes
        WHERE task_id = ?
        ORDER BY node_id
        """,
        (arguments["task_id"],),
    ).fetchall()
    if tuple(str(row["node_id"]) for row in rows) != _EXPECTED_NODE_IDS:
        raise IntegrationScopeAmendmentError(
            "integration scope amendment requires exactly the fixed A/B/C/D/E lane"
        )
    nodes = {
        str(row["node_id"]): _node_binding(row, str(arguments["task_id"]))
        for row in rows
    }
    _validate_lane(nodes, arguments)

    d = nodes["D"]
    allocation = candidate["allocation"]
    if (
        d["worktree"] != allocation["current_path"]
        or d["attempt"] != allocation["attempt"]
        or d["node_id"] != candidate["node"]["node_id"]
    ):
        raise StateConflictError("integration scope amendment D allocation binding changed")
    # ``_blocked_source_only_recovery_durable_snapshot()`` retains the active
    # state as part of its allocation document; carry that same fixed fact in
    # this binding so the two independent source snapshots can compare exactly.
    d["allocation"] = {**dict(allocation), "state": "active"}

    e_retained_allocation = _retained_e_allocation(
        connection,
        task_id=str(arguments["task_id"]),
        contract=contract,
        e=nodes["E"],
    )
    nodes["E"]["retained_allocation"] = e_retained_allocation

    return {
        "candidate": candidate,
        "task": {
            "task_id": str(task["task_id"]),
            "state": str(task["state"]),
            "revision": int(task["revision"]),
            "contract_json": str(task["contract_json"]),
            "contract_hash": str(task["contract_hash"]),
            "contract": dict(contract),
            "allowed_scope": allowed_scope,
            "forbidden_scope": forbidden_scope,
        },
        "a": nodes["A"],
        "b": nodes["B"],
        "c": nodes["C"],
        "d": d,
        "e": nodes["E"],
    }


def _node_binding(row: Any, task_id: str) -> dict[str, Any]:
    """Parse one stored node without reconstructing or normalizing its spec."""

    try:
        spec = json.loads(str(row["spec_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise StateConflictError(
            f"integration scope amendment node {row['node_id']} specification is invalid JSON"
        ) from error
    if not isinstance(spec, dict):
        raise StateConflictError(
            f"integration scope amendment node {row['node_id']} specification is not an object"
        )
    node_id = str(row["node_id"])
    if spec.get("node_id") != node_id or spec.get("task_id") != task_id:
        raise StateConflictError("integration scope amendment node specification identity is invalid")
    return {
        "node_id": node_id,
        "spec_json": str(row["spec_json"]),
        "spec": spec,
        "state": str(row["state"]),
        "attempt": _nonnegative_int(row["attempt"], f"node {node_id} attempt"),
        "worker_id": row["worker_id"],
        "worktree": row["worktree"],
        "effective_executor": row["effective_executor"],
        "effective_model": row["effective_model"],
        "started_at": row["started_at"],
        "settled_at": row["settled_at"],
        "result_json": row["result_json"],
        "recovery_json": row["recovery_json"],
        "coordinator_epoch": _nonnegative_int(
            row["coordinator_epoch"], f"node {node_id} coordinator epoch"
        ),
        "lease_epoch": _nonnegative_int(row["lease_epoch"], f"node {node_id} lease epoch"),
        "updated_at": str(row["updated_at"]),
    }


def _validate_lane(nodes: Mapping[str, Mapping[str, Any]], arguments: Mapping[str, Any]) -> None:
    """Fail closed unless durable nodes are the literal stopped A-E repair lane."""

    for node_id in _EXPECTED_NODE_IDS:
        node = nodes[node_id]
        spec = node["spec"]
        if not isinstance(spec, Mapping):
            raise StateConflictError("integration scope amendment node spec is invalid")
        expected_verifier = node_id == "E"
        if spec.get("verifier") is not expected_verifier:
            raise IntegrationScopeAmendmentError(
                f"integration scope amendment node {node_id} verifier role is incompatible"
            )
        if type(spec.get("ordinal")) is not int or spec["ordinal"] != _EXPECTED_ORDINALS[node_id]:
            raise IntegrationScopeAmendmentError(
                f"integration scope amendment node {node_id} ordinal is incompatible"
            )
        depends_on = _string_list(spec.get("depends_on"), f"node {node_id} depends_on")
        if tuple(depends_on) != _EXPECTED_DEPENDENCIES[node_id]:
            raise IntegrationScopeAmendmentError(
                f"integration scope amendment node {node_id} dependencies are incompatible"
            )

    for node_id in ("A", "B", "C"):
        node = nodes[node_id]
        if node["state"] != "accepted" or node["attempt"] < 1 or node["recovery_json"] is not None:
            raise StateConflictError(
                f"integration scope amendment requires accepted stable ancestor {node_id}"
            )
        result = _result_object(node["result_json"], f"accepted ancestor {node_id} result")
        if result.get("status") != "succeeded":
            raise StateConflictError(
                f"integration scope amendment ancestor {node_id} lacks an accepted success result"
            )

    d = nodes["D"]
    if d["state"] != "blocked" or d["attempt"] != arguments["expected_attempt"]:
        raise StateConflictError("integration scope amendment D state or attempt changed")
    if d["recovery_json"] is not None or not isinstance(d["worktree"], str) or not d["worktree"]:
        raise StateConflictError("integration scope amendment D recovery or worktree is incompatible")
    _result_object(d["result_json"], "blocked D result")
    _scope_list(d["spec"].get("read_scopes"), "D read_scopes")
    _scope_list(d["spec"].get("write_scopes"), "D write_scopes")

    e = nodes["E"]
    if e["state"] != "pending":
        raise StateConflictError("integration scope amendment requires pending verifier E")
    if any(
        e[field] is not None
        for field in (
            "worker_id",
            "worktree",
            "effective_executor",
            "effective_model",
            "started_at",
            "settled_at",
            "result_json",
            "recovery_json",
        )
    ) or e["coordinator_epoch"] != 0 or e["lease_epoch"] != 0:
        raise StateConflictError("integration scope amendment pending verifier E has stale execution state")
    _scope_list(e["spec"].get("read_scopes"), "E read_scopes")
    if _scope_list(e["spec"].get("write_scopes"), "E write_scopes"):
        raise IntegrationScopeAmendmentError(
            "integration scope amendment verifier E must retain empty write_scopes"
        )


def _retained_e_allocation(
    connection: Any,
    *,
    task_id: str,
    contract: Mapping[str, Any],
    e: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Prove one active E allocation is a settled historical verifier record.

    A verifier failure resets its node to pending so the selected owner can
    repair, but the terminal allocation remains active for recovery and audit.
    A currently active allocation is therefore acceptable only when its native
    allocation, failure, and repair events prove that exact completed attempt.
    """

    attempt = _nonnegative_int(e.get("attempt"), "E attempt")
    rows = connection.execute(
        """
        SELECT allocation_id, task_id, node_id, attempt, repository, base_sha,
               branch, current_path, state, node_result_json, created_at, updated_at
        FROM worktree_allocations
        WHERE task_id = ? AND node_id = 'E'
        ORDER BY attempt, allocation_id
        """,
        (task_id,),
    ).fetchall()
    future = [row for row in rows if int(row["attempt"]) > attempt]
    if future:
        raise StateConflictError("pending verifier E has a future allocation")
    live = [row for row in rows if row["state"] in {"active", "quarantine_pending"}]
    if not live:
        return None
    if len(live) != 1:
        raise StateConflictError("pending verifier E has multiple live allocations")
    allocation = live[0]
    if allocation["state"] != "active":
        raise StateConflictError("pending verifier E allocation is not an active retained record")
    if int(allocation["attempt"]) != attempt:
        raise StateConflictError("pending verifier E has a mismatched live allocation attempt")
    repository = contract.get("repository")
    base_sha = contract.get("base_sha")
    expected_branch = WorktreeManager.branch_name(task_id, "E", attempt)
    if (
        allocation["task_id"] != task_id
        or allocation["node_id"] != "E"
        or allocation["repository"] != repository
        or allocation["base_sha"] != base_sha
        or allocation["branch"] != expected_branch
        or not isinstance(allocation["current_path"], str)
        or not allocation["current_path"]
        or not isinstance(allocation["node_result_json"], str)
        or not allocation["node_result_json"]
    ):
        raise StateConflictError("pending verifier E allocation does not match its task attempt")
    result = _result_object(allocation["node_result_json"], "retained verifier E result")
    if (
        result.get("status") != "failed"
        or result.get("result_kind") != "verifier"
        or result.get("verdict") != "needs_fix"
    ):
        raise StateConflictError("pending verifier E allocation lacks a terminal failed verifier result")

    allocated_event = _single_event(
        connection,
        task_id=task_id,
        node_id="E",
        event_type="worktree.allocated",
        predicate=lambda payload: (
            payload.get("allocation_id") == allocation["allocation_id"]
            and payload.get("attempt") == attempt
            and payload.get("path") == allocation["current_path"]
            and payload.get("branch") == allocation["branch"]
        ),
        label="retained verifier E allocation",
    )
    failed_event = _single_event(
        connection,
        task_id=task_id,
        node_id="E",
        event_type="node.failed",
        predicate=lambda payload: (
            payload.get("attempt") == attempt
            and isinstance(payload.get("result"), dict)
            and canonical_json(payload["result"]) == canonical_json(result)
        ),
        label="retained verifier E failure",
    )
    repair_event = _single_event(
        connection,
        task_id=task_id,
        node_id="E",
        event_type="task.repair_scheduled",
        predicate=lambda payload: payload.get("verifier_attempt") == attempt,
        label="retained verifier E repair",
    )
    if not (
        allocated_event["cursor"] < failed_event["cursor"] < repair_event["cursor"]
    ):
        raise StateConflictError("retained verifier E event ordering is invalid")
    later_lifecycle = connection.execute(
        """
        SELECT event_type FROM events
        WHERE task_id = ? AND node_id = 'E' AND cursor > ?
          AND event_type IN ('node.started', 'node.indeterminate', 'node.accepted',
                             'node.failed', 'node.blocked')
        LIMIT 1
        """,
        (task_id, failed_event["cursor"]),
    ).fetchone()
    if later_lifecycle is not None:
        raise StateConflictError("pending verifier E has later lifecycle evidence")
    later_allocation_event = connection.execute(
        """
        SELECT cursor FROM events
        WHERE task_id = ? AND node_id = 'E' AND cursor > ?
          AND event_type = 'worktree.allocated'
        LIMIT 1
        """,
        (task_id, failed_event["cursor"]),
    ).fetchone()
    if later_allocation_event is not None:
        raise StateConflictError("pending verifier E has later allocation evidence")
    return {
        "allocation": dict(allocation),
        "allocated_event": allocated_event,
        "failed_event": failed_event,
        "repair_event": repair_event,
    }


def _single_event(
    connection: Any,
    *,
    task_id: str,
    node_id: str,
    event_type: str,
    predicate: Callable[[Mapping[str, Any]], bool],
    label: str,
) -> dict[str, Any]:
    """Return exactly one native task event whose JSON payload matches a proof."""

    rows = connection.execute(
        """
        SELECT cursor, payload_json, created_at FROM events
        WHERE task_id = ? AND node_id = ? AND event_type = ?
        ORDER BY cursor
        """,
        (task_id, node_id, event_type),
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise StateConflictError(f"{label} event payload is invalid JSON") from error
        if not isinstance(payload, dict):
            raise StateConflictError(f"{label} event payload is invalid")
        if predicate(payload):
            matches.append(
                {
                    "cursor": int(row["cursor"]),
                    "created_at": str(row["created_at"]),
                    "payload": payload,
                }
            )
    if len(matches) != 1:
        raise StateConflictError(f"{label} evidence is missing or ambiguous")
    return matches[0]


def _retained_e_worktree_identity(binding: Mapping[str, Any]) -> dict[str, Any] | None:
    """Require a historically settled retained E worktree to be idle and intact."""

    task = binding.get("task")
    e = binding.get("e")
    if not isinstance(task, Mapping) or not isinstance(e, Mapping):
        raise StateConflictError("integration scope amendment retained verifier binding is invalid")
    retained = e.get("retained_allocation")
    if retained is None:
        return None
    if not isinstance(retained, Mapping):
        raise StateConflictError("integration scope amendment retained verifier allocation is invalid")
    allocation = retained.get("allocation")
    if not isinstance(allocation, Mapping):
        raise StateConflictError("integration scope amendment retained verifier allocation is invalid")
    root = _worktree_root(str(allocation.get("current_path", "")))
    try:
        assert_recovery_source_idle(root)
    except RecoveryProcessError as error:
        raise StateConflictError(
            "integration scope amendment cannot prove retained verifier E is idle: " + str(error)
        ) from error
    top_level = _git_text(root, "rev-parse", "--show-toplevel")
    if Path(top_level).resolve(strict=True) != root:
        raise StateConflictError("retained verifier E is not an isolated Git worktree")
    head = _git_text(root, "rev-parse", "HEAD")
    branch = _git_text(root, "branch", "--show-current")
    common_dir = _git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    contract = task.get("contract")
    if not isinstance(contract, Mapping):
        raise StateConflictError("integration scope amendment task contract is invalid")
    repository = contract.get("repository")
    if not isinstance(repository, str) or not repository:
        raise StateConflictError("integration scope amendment task repository is invalid")
    repository_common = _git_text(
        Path(repository).expanduser().resolve(strict=True),
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if (
        head != allocation.get("base_sha")
        or branch != allocation.get("branch")
        or common_dir != repository_common
    ):
        raise StateConflictError("retained verifier E Git identity no longer matches its allocation")
    return {
        "allocation_id": allocation.get("allocation_id"),
        "attempt": allocation.get("attempt"),
        "worktree": str(root),
        "top_level": top_level,
        "head": head,
        "branch": branch,
        "git_common_dir": common_dir,
        "result_sha256": sha256(str(allocation["node_result_json"]).encode()).hexdigest(),
        "event_cursors": {
            "allocated": retained["allocated_event"]["cursor"],
            "failed": retained["failed_event"]["cursor"],
            "repair": retained["repair_event"]["cursor"],
        },
        "idle_observation": "idle",
    }


def _source_snapshot(
    store: WorkbenchStore,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Use the existing source-only snapshot/delta helper without mutating it."""

    try:
        snapshot, delta = store._prepare_source_only_blocked_recovery(
            str(arguments["task_id"]),
            "D",
            expected_revision=int(arguments["expected_revision"]),
            expected_attempt=int(arguments["expected_attempt"]),
            preserve_untracked=None,
            expected_checkpoint_sha=None,
            expected_source_delta_sha256=None,
        )
    except (DirtyWorktreeRecoveryError, ValueError) as error:
        raise IntegrationScopeAmendmentError(
            "integration scope amendment source inspection failed: " + str(error)
        ) from error
    if not isinstance(snapshot, dict):
        raise StateConflictError("integration scope amendment source snapshot is invalid")
    return snapshot, delta


def _assert_source_matches_binding(
    source_snapshot: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    """Require the source-only helper to describe this exact D durable record."""

    task = binding["task"]
    d = binding["d"]
    if not isinstance(task, Mapping) or not isinstance(d, Mapping):
        raise StateConflictError("integration scope amendment durable binding is invalid")
    if (
        source_snapshot.get("contract_json") != task["contract_json"]
        or source_snapshot.get("contract_hash") != task["contract_hash"]
        or source_snapshot.get("spec_json") != d["spec_json"]
        or source_snapshot.get("historical_result_json") != d["result_json"]
    ):
        raise StateConflictError("integration scope amendment source snapshot does not match D")
    allocation = source_snapshot.get("allocation")
    if not isinstance(allocation, Mapping) or canonical_json(allocation) != canonical_json(d["allocation"]):
        raise StateConflictError("integration scope amendment source allocation does not match D")


def _source_identity(
    binding: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a non-symlinked D worktree, Git identity, and package markers."""

    d = binding["d"]
    task = binding["task"]
    if not isinstance(d, Mapping) or not isinstance(task, Mapping):
        raise StateConflictError("integration scope amendment source binding is invalid")
    allocation = d["allocation"]
    if not isinstance(allocation, Mapping):
        raise StateConflictError("integration scope amendment allocation is invalid")
    root = _worktree_root(str(allocation["current_path"]))
    marker_hashes: dict[str, dict[str, str]] = {}
    for relative, package_name in REQUIRED_PACKAGE_MARKERS.items():
        data = _regular_worktree_file(root, relative)
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise IntegrationScopeAmendmentError(
                f"required package marker is invalid JSON: {relative}"
            ) from error
        if not isinstance(parsed, dict) or parsed.get("name") != package_name:
            raise IntegrationScopeAmendmentError(
                f"required package marker name is invalid: {relative}"
            )
        marker_hashes[relative] = {"name": package_name, "sha256": sha256(data).hexdigest()}
    scope_path_facts = _fixed_scope_path_facts(root)

    top_level = _git_text(root, "rev-parse", "--show-toplevel")
    if Path(top_level).resolve(strict=True) != root:
        raise IntegrationScopeAmendmentError("D worktree is not an isolated Git worktree")
    head = _git_text(root, "rev-parse", "HEAD")
    branch = _git_text(root, "branch", "--show-current")
    common_dir = _git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    expected_branch = str(allocation["branch"])
    if head != allocation["base_sha"] or branch != expected_branch:
        raise StateConflictError("D worktree Git identity no longer matches its allocation")
    repository = task["contract"].get("repository")
    if not isinstance(repository, str) or not repository:
        raise StateConflictError("integration scope amendment task repository is invalid")
    repository_common = _git_text(
        Path(repository).expanduser().resolve(strict=True),
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if common_dir != repository_common:
        raise StateConflictError("D worktree no longer belongs to the task repository")
    if source_snapshot.get("allocation") != allocation:
        raise StateConflictError("integration scope amendment source allocation drifted")
    return {
        "worktree": str(root),
        "top_level": top_level,
        "head": head,
        "branch": branch,
        "git_common_dir": common_dir,
        "package_markers": marker_hashes,
        "fixed_scope_path_facts": scope_path_facts,
    }


def _worktree_root(raw: str) -> Path:
    """Resolve one allocated D source directory without accepting a symlink leaf."""

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise IntegrationScopeAmendmentError("D worktree path must be absolute")
    try:
        metadata = candidate.lstat()
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IntegrationScopeAmendmentError("D worktree is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IntegrationScopeAmendmentError("D worktree must be a non-symlink directory")
    return root


def _regular_worktree_file(root: Path, relative: str) -> bytes:
    """Read one required regular non-symlink file inside the D worktree."""

    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != relative
        or "\\" in relative
    ):
        raise IntegrationScopeAmendmentError(f"required package marker path is invalid: {relative}")
    current = root
    try:
        for index, component in enumerate(path.parts):
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise IntegrationScopeAmendmentError(
                    f"required package marker path is a symlink: {relative}"
                )
            if index < len(path.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise IntegrationScopeAmendmentError(
                    f"required package marker has a non-directory parent: {relative}"
                )
            if index == len(path.parts) - 1 and not stat.S_ISREG(metadata.st_mode):
                raise IntegrationScopeAmendmentError(
                    f"required package marker is not a regular file: {relative}"
                )
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
        before = current.lstat()
        data = current.read_bytes()
        after = current.lstat()
    except IntegrationScopeAmendmentError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise IntegrationScopeAmendmentError(
            f"required package marker is unavailable: {relative}"
        ) from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise IntegrationScopeAmendmentError(
            f"required package marker changed while read: {relative}"
        )
    return data


def _fixed_scope_path_facts(root: Path) -> dict[str, dict[str, Any]]:
    """Bind safe filesystem facts for every fixed read/write source path.

    New output leaves may not exist yet, but every existing path component must
    remain a regular directory or regular file inside the allocated worktree.
    This rejects a symlink escape before a scope declaration can authorize it.
    """

    paths = tuple(dict.fromkeys(TASK_ACCESS_ADDITIONS))
    return {path: _fixed_scope_path_fact(root, path) for path in paths}


def _fixed_scope_path_fact(root: Path, relative: str) -> dict[str, Any]:
    """Inspect one fixed source path without requiring a future output leaf."""

    if not _is_exact_literal_scope(relative) or relative == ".":
        raise IntegrationScopeAmendmentError("fixed integration scope path is invalid")
    path = PurePosixPath(relative)
    current = root
    for index, component in enumerate(path.parts):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.resolve(strict=False).relative_to(root)
            except (OSError, RuntimeError, ValueError) as error:
                raise IntegrationScopeAmendmentError(
                    f"fixed integration scope escapes D worktree: {relative}"
                ) from error
            return {
                "state": "missing",
                "first_missing_component": "/".join(path.parts[: index + 1]),
            }
        except OSError as error:
            raise IntegrationScopeAmendmentError(
                f"cannot inspect fixed integration scope: {relative}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise IntegrationScopeAmendmentError(
                f"fixed integration scope path is a symlink: {relative}"
            )
        if index < len(path.parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode):
                raise IntegrationScopeAmendmentError(
                    f"fixed integration scope has a non-directory parent: {relative}"
                )
            continue
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise IntegrationScopeAmendmentError(
                f"fixed integration scope escapes D worktree: {relative}"
            ) from error
        if stat.S_ISREG(metadata.st_mode):
            before = metadata
            try:
                data = current.read_bytes()
                after = current.lstat()
            except OSError as error:
                raise IntegrationScopeAmendmentError(
                    f"cannot read fixed integration scope: {relative}"
                ) from error
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise IntegrationScopeAmendmentError(
                    f"fixed integration scope changed while read: {relative}"
                )
            return {"state": "regular_file", "sha256": sha256(data).hexdigest()}
        if stat.S_ISDIR(metadata.st_mode):
            return {
                "state": "directory",
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "mtime_ns": metadata.st_mtime_ns,
            }
        raise IntegrationScopeAmendmentError(
            f"fixed integration scope is neither a regular file nor directory: {relative}"
        )
    raise IntegrationScopeAmendmentError("fixed integration scope path is empty")


def _git_text(worktree: Path, *arguments: str) -> str:
    """Run one bounded read-only Git query for a sealed source binding."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IntegrationScopeAmendmentError("cannot inspect D worktree Git identity") from error
    if completed.returncode:
        raise IntegrationScopeAmendmentError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "cannot inspect D worktree Git identity"
        )
    return completed.stdout.strip()


def _plan(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Construct only the fixed uncovered additions without normalizing legacy text."""

    task = binding["task"]
    d = binding["d"]
    e = binding["e"]
    if not all(isinstance(value, Mapping) for value in (task, d, e)):
        raise StateConflictError("integration scope amendment durable plan binding is invalid")
    old_contract = task["contract"]
    old_d_spec = d["spec"]
    old_e_spec = e["spec"]
    if not all(isinstance(value, dict) for value in (old_contract, old_d_spec, old_e_spec)):
        raise StateConflictError("integration scope amendment durable documents are invalid")

    allowed = _scope_list(task["allowed_scope"], "task allowed_scope", nonempty=True)
    forbidden = _scope_list(task["forbidden_scope"], "task forbidden_scope")
    d_reads = _scope_list(old_d_spec.get("read_scopes"), "D read_scopes")
    d_writes = _scope_list(old_d_spec.get("write_scopes"), "D write_scopes")
    e_reads = _scope_list(old_e_spec.get("read_scopes"), "E read_scopes")
    e_writes = _scope_list(old_e_spec.get("write_scopes"), "E write_scopes")
    if e_writes:
        raise IntegrationScopeAmendmentError("verifier E write_scopes must be empty")

    _reject_forbidden_fixed_paths(TASK_ACCESS_ADDITIONS, forbidden)
    task_added = _uncovered(allowed, TASK_ACCESS_ADDITIONS)
    d_read_added = _uncovered(d_reads, NODE_READ_ADDITIONS)
    d_write_added = _uncovered(d_writes, NODE_WRITE_ADDITIONS)
    e_read_added = _uncovered(e_reads, NODE_READ_ADDITIONS)
    new_allowed = [*allowed, *task_added]
    for label, paths in (
        ("D read_scopes", [*d_reads, *d_read_added]),
        ("D write_scopes", [*d_writes, *d_write_added]),
        ("E read_scopes", [*e_reads, *e_read_added]),
    ):
        _require_fixed_paths_allowed(paths, new_allowed, forbidden, label)
    if not any((task_added, d_read_added, d_write_added, e_read_added)):
        raise IntegrationScopeAmendmentError("the fixed D/E integration scopes are already covered")

    new_contract = {**old_contract, "allowed_scope": new_allowed}
    new_d_spec = {
        **old_d_spec,
        "read_scopes": [*d_reads, *d_read_added],
        "write_scopes": [*d_writes, *d_write_added],
    }
    new_e_spec = {
        **old_e_spec,
        "read_scopes": [*e_reads, *e_read_added],
        "write_scopes": list(e_writes),
    }
    _assert_only_field_changed(old_contract, new_contract, "allowed_scope", "task contract")
    _assert_only_scope_fields_changed(old_d_spec, new_d_spec, "D specification")
    _assert_only_scope_fields_changed(old_e_spec, new_e_spec, "E specification")
    return {
        "old_contract": dict(old_contract),
        "old_contract_hash": str(task["contract_hash"]),
        "new_contract": new_contract,
        "new_contract_hash": canonical_hash(new_contract),
        "old_d_spec": dict(old_d_spec),
        "new_d_spec": new_d_spec,
        "old_e_spec": dict(old_e_spec),
        "new_e_spec": new_e_spec,
        "d_spec_changed": canonical_json(old_d_spec) != canonical_json(new_d_spec),
        "e_spec_changed": canonical_json(old_e_spec) != canonical_json(new_e_spec),
        "additions": {
            "task_allowed_scope": list(task_added),
            "d_read_scopes": list(d_read_added),
            "d_write_scopes": list(d_write_added),
            "e_read_scopes": list(e_read_added),
        },
    }


def _validate_fixed_profile_paths() -> None:
    """Fail closed if a source profile constant ceases to be exact paths."""

    for label, paths in (
        ("TASK_ACCESS_ADDITIONS", TASK_ACCESS_ADDITIONS),
        ("NODE_READ_ADDITIONS", NODE_READ_ADDITIONS),
        ("NODE_WRITE_ADDITIONS", NODE_WRITE_ADDITIONS),
    ):
        if not isinstance(paths, tuple) or not paths or len(paths) != len(set(paths)):
            raise IntegrationScopeAmendmentError(f"{label} must be a non-empty unique tuple")
        for path in paths:
            if not _is_exact_literal_scope(path) or path == ".":
                raise IntegrationScopeAmendmentError(f"{label} contains a non-literal path")
    if not set(NODE_WRITE_ADDITIONS).issubset(NODE_READ_ADDITIONS):
        raise IntegrationScopeAmendmentError("D write additions must also be declared as D read inputs")
    if not set(NODE_READ_ADDITIONS).issubset(TASK_ACCESS_ADDITIONS):
        raise IntegrationScopeAmendmentError("task scope additions must cover every D/E read input")
    if not set(NODE_WRITE_ADDITIONS).issubset(TASK_ACCESS_ADDITIONS):
        raise IntegrationScopeAmendmentError("task scope additions must cover every D write output")


def _scope_list(raw: object, label: str, *, nonempty: bool = False) -> list[str]:
    """Read legacy scope strings verbatim without canonicalizing their spelling."""

    if not isinstance(raw, list) or any(not isinstance(value, str) or not value for value in raw):
        raise IntegrationScopeAmendmentError(f"{label} must be an explicit string list")
    if nonempty and not raw:
        raise IntegrationScopeAmendmentError(f"{label} must not be empty")
    return list(raw)


def _string_list(raw: object, label: str) -> list[str]:
    """Validate one stored JSON string list without accepting coercions."""

    if not isinstance(raw, list) or any(not isinstance(value, str) or not value for value in raw):
        raise IntegrationScopeAmendmentError(f"{label} must be an explicit string list")
    return list(raw)


def _uncovered(existing: Sequence[str], required: Sequence[str]) -> tuple[str, ...]:
    """Return fixed paths not covered by a canonical literal existing scope.

    Legacy wildcard and non-canonical spellings remain byte-for-byte untouched;
    they are deliberately not widened, narrowed, or treated as implicit coverage.
    """

    additions: list[str] = []
    for path in required:
        if not any(_literal_scope_covers(scope, path) for scope in existing):
            additions.append(path)
    return tuple(additions)


def _reject_forbidden_fixed_paths(paths: Sequence[str], forbidden: Sequence[str]) -> None:
    """Reject a fixed added path blocked by an exact existing forbidden scope."""

    ambiguous = [scope for scope in forbidden if not _is_exact_literal_scope(scope)]
    if ambiguous:
        raise IntegrationScopeAmendmentError(
            "task forbidden_scope contains a non-literal rule; fixed scope coverage is ambiguous"
        )
    for path in paths:
        if any(_literal_scope_covers(scope, path) for scope in forbidden):
            raise IntegrationScopeAmendmentError(
                "fixed integration scope is forbidden by the task contract: " + path
            )


def _require_fixed_paths_allowed(
    paths: Sequence[str],
    allowed: Sequence[str],
    forbidden: Sequence[str],
    label: str,
) -> None:
    """Check only fixed literal additions against the planner's task-level rule."""

    for path in paths:
        if path not in NODE_READ_ADDITIONS and path not in NODE_WRITE_ADDITIONS:
            continue
        if not any(_literal_scope_covers(scope, path) for scope in allowed):
            raise IntegrationScopeAmendmentError(f"{label} is outside task allowed_scope: {path}")
        if any(_literal_scope_covers(scope, path) for scope in forbidden):
            raise IntegrationScopeAmendmentError(f"{label} is forbidden by the task contract: {path}")


def _is_exact_literal_scope(value: object) -> bool:
    """Return whether a scope can be compared without reinterpreting legacy text."""

    if not isinstance(value, str) or not value or "\\" in value or any(
        token in value for token in "*?[]\x00"
    ):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value


def _literal_scope_covers(scope: str, path: str) -> bool:
    """Use only exact canonical literal scopes for coverage calculations."""

    if not _is_exact_literal_scope(scope) or not _is_exact_literal_scope(path):
        return False
    return scope == "." or path == scope or path.startswith(scope + "/")


def _assert_only_field_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    field: str,
    label: str,
) -> None:
    """Ensure a document update cannot alter an unrelated contract field."""

    before_other = {key: value for key, value in before.items() if key != field}
    after_other = {key: value for key, value in after.items() if key != field}
    if canonical_json(before_other) != canonical_json(after_other):
        raise IntegrationScopeAmendmentError(f"{label} would change an unrelated field")


def _assert_only_scope_fields_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    label: str,
) -> None:
    """Ensure a node update touches only its declared read/write scope arrays."""

    before_other = {
        key: value for key, value in before.items() if key not in {"read_scopes", "write_scopes"}
    }
    after_other = {
        key: value for key, value in after.items() if key not in {"read_scopes", "write_scopes"}
    }
    if canonical_json(before_other) != canonical_json(after_other):
        raise IntegrationScopeAmendmentError(f"{label} would change an unrelated field")


def _apply(
    store: WorkbenchStore,
    arguments: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the short SQL-only CAS after all source and Git reads completed."""

    binding = preflight["binding"]
    plan = preflight["plan"]
    if not isinstance(binding, Mapping) or not isinstance(plan, Mapping):
        raise StateConflictError("integration scope amendment preflight is invalid")
    timestamp = now_iso()
    with store.transaction() as connection:
        current = _durable_binding_from_connection(store, connection, arguments)
        if canonical_json(current) != canonical_json(binding):
            raise StateConflictError("integration scope amendment durable binding changed before compare-and-set")
        task = current["task"]
        d = current["d"]
        e = current["e"]
        if not all(isinstance(value, Mapping) for value in (task, d, e)):
            raise StateConflictError("integration scope amendment durable state is invalid")
        revision = int(arguments["expected_revision"]) + 1
        task_changed = connection.execute(
            """
            UPDATE tasks
            SET contract_json = ?, contract_hash = ?, state_revision = ?, updated_at = ?
            WHERE task_id = ? AND state = 'blocked' AND state_revision = ?
              AND contract_hash = ? AND contract_json = ?
            """,
            (
                canonical_json(plan["new_contract"]),
                plan["new_contract_hash"],
                revision,
                timestamp,
                arguments["task_id"],
                arguments["expected_revision"],
                plan["old_contract_hash"],
                task["contract_json"],
            ),
        ).rowcount
        if task_changed != 1:
            raise StateConflictError("integration scope amendment task compare-and-set failed")
        d_changed = 0
        if plan["d_spec_changed"]:
            d_changed = connection.execute(
                """
                UPDATE nodes
                SET spec_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = 'D' AND state = 'blocked' AND attempt = ?
                  AND spec_json = ? AND worker_id IS ? AND worktree IS ?
                  AND effective_executor IS ? AND effective_model IS ?
                  AND started_at IS ? AND settled_at IS ? AND result_json IS ?
                  AND recovery_json IS NULL AND coordinator_epoch = ? AND lease_epoch = ?
                """,
                (
                    canonical_json(plan["new_d_spec"]),
                    timestamp,
                    arguments["task_id"],
                    arguments["expected_attempt"],
                    d["spec_json"],
                    d["worker_id"],
                    d["worktree"],
                    d["effective_executor"],
                    d["effective_model"],
                    d["started_at"],
                    d["settled_at"],
                    d["result_json"],
                    d["coordinator_epoch"],
                    d["lease_epoch"],
                ),
            ).rowcount
            if d_changed != 1:
                raise StateConflictError("integration scope amendment D compare-and-set failed")
        e_changed = 0
        if plan["e_spec_changed"]:
            e_changed = connection.execute(
                """
                UPDATE nodes
                SET spec_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = 'E' AND state = 'pending' AND attempt = ?
                  AND spec_json = ? AND worker_id IS NULL AND worktree IS NULL
                  AND effective_executor IS NULL AND effective_model IS NULL
                  AND started_at IS NULL AND settled_at IS NULL AND result_json IS NULL
                  AND recovery_json IS NULL AND coordinator_epoch = 0 AND lease_epoch = 0
                """,
                (
                    canonical_json(plan["new_e_spec"]),
                    timestamp,
                    arguments["task_id"],
                    e["attempt"],
                    e["spec_json"],
                ),
            ).rowcount
            if e_changed != 1:
                raise StateConflictError("integration scope amendment E compare-and-set failed")
        event_payload = {
            "profile_id": SCOPE_PROFILE_ID,
            "reason": arguments["reason"],
            "preview_fingerprint": preflight["fingerprint"],
            "attempt": arguments["expected_attempt"],
            "revision": revision,
            "old_contract": plan["old_contract"],
            "old_contract_hash": plan["old_contract_hash"],
            "new_contract": plan["new_contract"],
            "new_contract_hash": plan["new_contract_hash"],
            "d_scope_change": {
                "old_spec": plan["old_d_spec"],
                "new_spec": plan["new_d_spec"],
            },
            "e_scope_change": {
                "old_spec": plan["old_e_spec"],
                "new_spec": plan["new_e_spec"],
            },
            "additions": plan["additions"],
            "source": preflight["source"],
        }
        event_cursor = store._event(
            connection,
            _EVENT_TYPE,
            str(arguments["task_id"]),
            "D",
            event_payload,
            created_at=timestamp,
        )
    return _response(
        arguments,
        preflight,
        dry_run=False,
        revision=revision,
        event_cursor=event_cursor,
    )


def _response(
    arguments: Mapping[str, Any],
    preflight: Mapping[str, Any],
    *,
    dry_run: bool,
    revision: int,
    event_cursor: int | None = None,
) -> dict[str, Any]:
    """Return the fixed scope recipe without implying source work was run."""

    plan = preflight["plan"]
    if not isinstance(plan, Mapping):
        raise StateConflictError("integration scope amendment plan is invalid")
    scope_changes = {
        "task_allowed_scope": {
            "before": list(plan["old_contract"]["allowed_scope"]),
            "added": list(plan["additions"]["task_allowed_scope"]),
            "after": list(plan["new_contract"]["allowed_scope"]),
        },
        "D": {
            "read_scopes": {
                "before": list(plan["old_d_spec"]["read_scopes"]),
                "added": list(plan["additions"]["d_read_scopes"]),
                "after": list(plan["new_d_spec"]["read_scopes"]),
            },
            "write_scopes": {
                "before": list(plan["old_d_spec"]["write_scopes"]),
                "added": list(plan["additions"]["d_write_scopes"]),
                "after": list(plan["new_d_spec"]["write_scopes"]),
            },
        },
        "E": {
            "read_scopes": {
                "before": list(plan["old_e_spec"]["read_scopes"]),
                "added": list(plan["additions"]["e_read_scopes"]),
                "after": list(plan["new_e_spec"]["read_scopes"]),
            },
            "write_scopes": {
                "before": list(plan["old_e_spec"]["write_scopes"]),
                "added": [],
                "after": list(plan["new_e_spec"]["write_scopes"]),
            },
        },
    }
    value = {
        "task_id": arguments["task_id"],
        "node_id": "D",
        "expected_attempt": arguments["expected_attempt"],
        "profile_id": SCOPE_PROFILE_ID,
        "dry_run": dry_run,
        "old_contract_hash": plan["old_contract_hash"],
        "new_contract_hash": plan["new_contract_hash"],
        "old_revision": arguments["expected_revision"],
        "new_revision": revision,
        "fingerprint": preflight["fingerprint"],
        "scope_changes": scope_changes,
        "source": preflight["source"],
        "task_state_changed": False,
        "nodes_would_change": plan["d_spec_changed"] or plan["e_spec_changed"],
        "nodes_changed": not dry_run and (plan["d_spec_changed"] or plan["e_spec_changed"]),
        "allocations_changed": False,
        "queued": False,
        "commands_executed": False,
        "source_written": False,
    }
    if event_cursor is not None:
        value["event_cursor"] = event_cursor
    return value


def _fingerprint_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the immutable new documents and exact additions in a fingerprint."""

    return {
        "old_contract_hash": plan["old_contract_hash"],
        "new_contract_hash": plan["new_contract_hash"],
        "new_contract": plan["new_contract"],
        "new_d_spec": plan["new_d_spec"],
        "new_e_spec": plan["new_e_spec"],
        "additions": plan["additions"],
    }


def _delta_payload(delta: Any) -> dict[str, Any]:
    """Return the complete source-delta identity without creating an artifact."""

    try:
        return {
            "comparison_tree": str(delta.comparison_tree),
            "changed_paths": list(delta.changed_paths),
            "untracked_paths": list(delta.untracked_paths),
            "entries": [entry.to_dict() for entry in delta.entries],
            "sha256": str(delta.sha256),
        }
    except (AttributeError, TypeError) as error:
        raise StateConflictError("integration scope amendment source delta is invalid") from error


def _result_object(raw: object, label: str) -> dict[str, Any]:
    """Parse one unchanged durable result receipt for binding only."""

    if not isinstance(raw, str) or not raw:
        raise StateConflictError(f"{label} is missing")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StateConflictError(f"{label} is invalid JSON") from error
    if not isinstance(result, dict):
        raise StateConflictError(f"{label} is not an object")
    return result


def _digest(value: object, label: str) -> str:
    """Require one lower-case SHA-256 text value."""

    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    """Return one stored non-negative integer without accepting booleans."""

    if type(value) is not int or value < 0:
        raise StateConflictError(f"{label} is invalid")
    return value


__all__ = [
    "IntegrationScopeAmendmentError",
    "amend_blocked_integration_scope",
]
