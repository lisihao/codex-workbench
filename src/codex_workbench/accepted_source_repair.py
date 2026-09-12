"""Prepare one accepted worker's source patch for an owner-only repair.

Verifier feedback may require a worker to continue from an already accepted
patch.  That is distinct from failed-attempt recovery: the original result
remains a succeeded worker receipt and is never rewritten as a failed or
blocked result.  This module seals the accepted source inputs in
``nodes.recovery_json`` and materializes them only after the ordinary
scheduler has claimed the next worker attempt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, TypedDict

from .dependency_inputs import (
    DependencyInput,
    DependencyInputError,
    apply_ready_lockfile_handoffs_to_dependency_input,
    apply_recorded_dependency_input,
    base_dependency_input,
    load_recorded_dependency_input,
    validate_dependency_input_lineage,
)
from .model import canonical_json
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeError, WorktreeManager


ACCEPTED_SOURCE_REPAIR_KIND = "accepted-source-repair-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class AcceptedSourceRepairError(StateConflictError):
    """An accepted worker cannot safely seed a repair attempt."""


class AcceptedSourceRepairSource(TypedDict):
    """Immutable source metadata sealed before an owner repair is queued."""

    attempt: int
    worktree: str
    branch: str
    base_sha: str
    patch_ref: str
    patch_sha256: str
    dependency_input_ref: str | None
    dependency_input: dict[str, Any] | None


class AcceptedSourceRepairBinding(TypedDict):
    """One durable, claimable accepted-source repair authorization."""

    schema_version: int
    kind: str
    state: str
    authorization_revision: int
    task_id: str
    node_id: str
    verifier_node_id: str
    verifier_attempt: int
    repair_node_ids: list[str]
    source_status: str
    source_allocation_id: str
    source_result_json: str
    source: AcceptedSourceRepairSource


class AcceptedSourceRepairPreparedReceipt(TypedDict):
    """Filesystem proof that a fresh repair target contains the sealed input."""

    schema_version: int
    kind: str
    state: str
    authorization_revision: int
    source_allocation_id: str
    source_attempt: int
    source_status: str
    target_attempt: int
    target_worktree: str
    target_branch: str
    dependency_input_ref: str | None
    dependency_input_tree_sha: str
    patch_ref: str
    patch_sha256: str
    binding_sha256: str


@dataclass(frozen=True)
class PreparedAcceptedSourceRepair:
    """A fresh worktree and immutable input ready for the normal executor."""

    worktree: Path
    dependency_input: DependencyInput
    receipt: AcceptedSourceRepairPreparedReceipt


def build_accepted_repair_bindings(
    store: WorkbenchStore,
    connection: sqlite3.Connection,
    task_id: str,
    repair_node_ids: Sequence[str],
    verifier_node_id: str,
    verifier_attempt: int,
    authorization_revision: int,
) -> dict[str, AcceptedSourceRepairBinding]:
    """Build filesystem-free source bindings for an explicit verifier repair.

    This function only reads the supplied SQLite connection and verified
    content-addressed artifacts.  Callers may invoke it while authorizing the
    verifier transition, but must perform all Git worktree preparation after
    that write transaction commits.  The caller is responsible for proving
    that the currently-running verifier result is a failed repair request;
    the signature intentionally does not accept a mutable verifier result.

    @param store: The authority store owning task state and artifacts.
    @param connection: A caller-owned SQLite connection used only for reads.
    @param task_id: Task whose verifier requested the repair.
    @param repair_node_ids: Exact accepted source owners to resume.
    @param verifier_node_id: The running verifier that authorized this repair.
    @param verifier_attempt: Current verifier attempt.
    @param authorization_revision: Task revision the caller will commit.
    @returns: One immutable binding for each requested owner.
    """

    task_id = _text(task_id, "task_id")
    verifier_node_id = _text(verifier_node_id, "verifier_node_id")
    requested = _repair_node_ids(repair_node_ids)
    _positive_int(verifier_attempt, "verifier_attempt")
    _positive_int(authorization_revision, "authorization_revision")

    task = connection.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if task is None:
        raise KeyError(task_id)
    if task["state"] != "verifying":
        raise AcceptedSourceRepairError(
            f"task {task_id} is {task['state']}, expected verifying"
        )
    if authorization_revision != int(task["state_revision"]) + 1:
        raise AcceptedSourceRepairError(
            "accepted-source repair authorization_revision must advance the current task revision by one"
        )
    contract = _json_object(task["contract_json"], "accepted-source repair task contract")
    _require_local_only_contract(contract)
    repository = _text(contract.get("repository"), "accepted-source repair repository")
    base_sha = _text(contract.get("base_sha"), "accepted-source repair contract base")

    rows = connection.execute(
        "SELECT * FROM nodes WHERE task_id = ? ORDER BY node_id", (task_id,)
    ).fetchall()
    nodes = _node_rows(task_id, rows)
    verifier = nodes.get(verifier_node_id)
    if verifier is None:
        raise AcceptedSourceRepairError("accepted-source repair verifier is missing")
    verifier_spec = verifier["spec"]
    if verifier_spec.get("verifier") is not True:
        raise AcceptedSourceRepairError("accepted-source repair node is not a verifier")
    if verifier["row"]["state"] != "running":
        raise AcceptedSourceRepairError("accepted-source repair verifier is not running")
    if int(verifier["row"]["attempt"]) != verifier_attempt:
        raise AcceptedSourceRepairError("accepted-source repair verifier attempt is stale")
    if verifier_node_id in requested:
        raise AcceptedSourceRepairError("accepted-source repair cannot select its verifier")
    for node_id in requested:
        if node_id not in nodes:
            raise AcceptedSourceRepairError(
                f"accepted-source repair owner {node_id} is missing"
            )

    dependencies = _dependencies(nodes)
    _assert_acyclic(dependencies)
    _require_repaired_accepted_descendants(nodes, dependencies, set(requested))
    snapshot = _task_snapshot(task_id, contract, nodes)

    bindings: dict[str, AcceptedSourceRepairBinding] = {}
    for node_id in requested:
        node = nodes.get(node_id)
        if node is None:
            raise AcceptedSourceRepairError(
                f"accepted-source repair owner {node_id} is missing"
            )
        spec = node["spec"]
        row = node["row"]
        if spec.get("verifier") is True:
            raise AcceptedSourceRepairError(
                f"accepted-source repair owner {node_id} is a verifier"
            )
        if row["state"] != "accepted":
            raise AcceptedSourceRepairError(
                f"accepted-source repair owner {node_id} is {row['state']}, expected accepted"
            )
        if row["recovery_json"] is not None:
            raise AcceptedSourceRepairError(
                f"accepted-source repair owner {node_id} already has recovery state"
            )
        result_json = row["result_json"]
        result = _accepted_source_result(result_json, node_id)
        artifacts = result["artifacts"]
        assert isinstance(artifacts, Mapping)
        patch_ref = _text(artifacts.get("patch"), "accepted-source patch ref")
        patch = _verified_artifact_bytes(store, patch_ref, "accepted-source patch")
        if not patch:
            raise AcceptedSourceRepairError("accepted-source patch artifact is empty")
        patch_sha256 = sha256(patch).hexdigest()

        allocation = connection.execute(
            """
            SELECT allocation_id, state, repository, base_sha, branch, current_path, attempt
            FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ?
            """,
            (task_id, node_id, int(row["attempt"])),
        ).fetchone()
        _validate_source_allocation(
            allocation,
            task_id=task_id,
            node_id=node_id,
            attempt=int(row["attempt"]),
            worktree=row["worktree"],
            repository=repository,
            base_sha=base_sha,
        )
        assert allocation is not None

        dependency_input_ref = artifacts.get("dependency-input")
        if dependency_input_ref is not None:
            dependency_input_ref = _text(
                dependency_input_ref, "accepted-source dependency-input ref"
            )
            try:
                dependency_input = load_recorded_dependency_input(
                    store.artifacts,
                    dependency_input_ref,
                    task_id=task_id,
                    node_id=node_id,
                    base_sha=base_sha,
                )
                validate_dependency_input_lineage(
                    snapshot,
                    node_id,
                    dependency_input,
                    artifacts=store.artifacts,
                )
            except (DependencyInputError, ValueError) as error:
                raise AcceptedSourceRepairError(
                    f"accepted-source repair owner {node_id} dependency input is invalid: {error}"
                ) from error
            frozen_dependency_input: dict[str, Any] | None = dict(
                dependency_input.receipt
            )
        else:
            if dependencies[node_id]:
                raise AcceptedSourceRepairError(
                    f"accepted-source repair owner {node_id} lacks its recorded dependency input"
                )
            frozen_dependency_input = None

        binding: AcceptedSourceRepairBinding = {
            "schema_version": 1,
            "kind": ACCEPTED_SOURCE_REPAIR_KIND,
            "state": "authorized",
            "authorization_revision": authorization_revision,
            "task_id": task_id,
            "node_id": node_id,
            "verifier_node_id": verifier_node_id,
            "verifier_attempt": verifier_attempt,
            "repair_node_ids": list(requested),
            "source_status": "succeeded",
            "source_allocation_id": str(allocation["allocation_id"]),
            "source_result_json": str(result_json),
            "source": {
                "attempt": int(row["attempt"]),
                "worktree": str(allocation["current_path"]),
                "branch": str(allocation["branch"]),
                "base_sha": base_sha,
                "patch_ref": patch_ref,
                "patch_sha256": patch_sha256,
                "dependency_input_ref": dependency_input_ref,
                "dependency_input": frozen_dependency_input,
            },
        }
        parsed = parse_accepted_source_repair_binding(
            binding, next_attempt=int(row["attempt"]) + 1
        )
        assert parsed is not None
        bindings[node_id] = parsed
    return bindings


def parse_accepted_source_repair_binding(
    raw: object,
    *,
    next_attempt: int | None = None,
) -> AcceptedSourceRepairBinding | None:
    """Decode one accepted-source authorization without reading Git or SQLite.

    Unrelated recovery kinds return ``None`` so the caller can preserve their
    existing lifecycle.  A document that names this kind is validated
    exhaustively and fails closed.  In particular, it must retain the original
    ``succeeded`` source receipt; a rewritten failed or blocked receipt is not
    accepted as an owner repair.

    @param raw: JSON stored in ``nodes.recovery_json`` or a parsed mapping.
    @param next_attempt: Optional claimed target attempt to bind to ``aN+1``.
    @returns: The validated binding, or ``None`` for another recovery kind.
    """

    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AcceptedSourceRepairError(
                "accepted-source repair binding is invalid JSON"
            ) from error
    else:
        value = raw
    if not isinstance(value, Mapping):
        raise AcceptedSourceRepairError("accepted-source repair binding is invalid")
    if value.get("kind") != ACCEPTED_SOURCE_REPAIR_KIND:
        return None
    required = {
        "schema_version",
        "kind",
        "state",
        "authorization_revision",
        "task_id",
        "node_id",
        "verifier_node_id",
        "verifier_attempt",
        "repair_node_ids",
        "source_status",
        "source_allocation_id",
        "source_result_json",
        "source",
    }
    if set(value) != required:
        raise AcceptedSourceRepairError(
            "accepted-source repair binding has an invalid shape"
        )
    if value["schema_version"] != 1 or value["state"] != "authorized":
        raise AcceptedSourceRepairError("accepted-source repair binding is not claimable")
    authorization_revision = _positive_int(
        value["authorization_revision"], "accepted-source authorization_revision"
    )
    task_id = _text(value["task_id"], "accepted-source task_id")
    node_id = _text(value["node_id"], "accepted-source node_id")
    verifier_node_id = _text(
        value["verifier_node_id"], "accepted-source verifier_node_id"
    )
    verifier_attempt = _positive_int(
        value["verifier_attempt"], "accepted-source verifier_attempt"
    )
    repair_node_ids = _repair_node_ids(value["repair_node_ids"])
    if node_id not in repair_node_ids:
        raise AcceptedSourceRepairError(
            "accepted-source repair binding does not select its source owner"
        )
    if verifier_node_id in repair_node_ids:
        raise AcceptedSourceRepairError(
            "accepted-source repair binding selects its verifier"
        )
    if value["source_status"] != "succeeded":
        raise AcceptedSourceRepairError(
            "accepted-source repair source_status must be succeeded"
        )
    allocation_id = _text(
        value["source_allocation_id"], "accepted-source source_allocation_id"
    )
    source_result_json = value["source_result_json"]
    if not isinstance(source_result_json, str):
        raise AcceptedSourceRepairError(
            "accepted-source repair source result is invalid"
        )
    result = _accepted_source_result(source_result_json, node_id)
    artifacts = result["artifacts"]
    assert isinstance(artifacts, Mapping)
    source = _source(value["source"], task_id, node_id)
    if source["attempt"] < 1:
        raise AcceptedSourceRepairError("accepted-source repair source attempt is invalid")
    if next_attempt is not None and source["attempt"] + 1 != next_attempt:
        raise AcceptedSourceRepairError(
            "accepted-source repair source does not match the claimed next attempt"
        )
    expected_branch = WorktreeManager.branch_name(task_id, node_id, source["attempt"])
    if source["branch"] != expected_branch:
        raise AcceptedSourceRepairError(
            "accepted-source repair source branch does not match its attempt"
        )
    if artifacts.get("patch") != source["patch_ref"]:
        raise AcceptedSourceRepairError(
            "accepted-source repair source result does not match its patch artifact"
        )
    if artifacts.get("dependency-input") != source["dependency_input_ref"]:
        raise AcceptedSourceRepairError(
            "accepted-source repair source result does not match its dependency input"
        )
    if source["dependency_input_ref"] is None:
        if source["dependency_input"] is not None:
            raise AcceptedSourceRepairError(
                "accepted-source repair base-only source cannot carry dependency input"
            )
    elif source["dependency_input"] is None:
        raise AcceptedSourceRepairError(
            "accepted-source repair dependency input receipt is missing"
        )
    else:
        _validate_frozen_dependency_receipt(
            source["dependency_input"],
            task_id=task_id,
            node_id=node_id,
            base_sha=source["base_sha"],
        )
    return {
        "schema_version": 1,
        "kind": ACCEPTED_SOURCE_REPAIR_KIND,
        "state": "authorized",
        "authorization_revision": authorization_revision,
        "task_id": task_id,
        "node_id": node_id,
        "verifier_node_id": verifier_node_id,
        "verifier_attempt": verifier_attempt,
        "repair_node_ids": list(repair_node_ids),
        "source_status": "succeeded",
        "source_allocation_id": allocation_id,
        "source_result_json": source_result_json,
        "source": source,
    }


def prepare_accepted_source_repair(
    store: WorkbenchStore,
    binding: Mapping[str, Any] | str,
    manager: WorktreeManager,
) -> PreparedAcceptedSourceRepair:
    """Materialize an accepted source patch on a fresh claimed ``aN+1`` tree.

    The caller must invoke this after ordinary scheduling claimed the repair
    owner and before its normal executor starts.  It deliberately performs no
    SQLite writes.  ``WorktreeManager.prepare_clean`` archives a stale target
    from a crashed preparation attempt, so the returned binding and target
    fields can be safely re-used by the caller's existing orphan recovery.

    @param store: Authority store used for read-only fencing and artifacts.
    @param binding: The durable accepted-source authorization.
    @param manager: Existing worktree manager for the task repository.
    @returns: Fresh worktree, reproduced dependency input, and a CAS receipt.
    """

    parsed = parse_accepted_source_repair_binding(binding)
    if parsed is None:
        raise AcceptedSourceRepairError("accepted-source repair binding is missing")
    target_attempt = parsed["source"]["attempt"] + 1
    initial = _preparation_snapshot(store, parsed, target_attempt)
    source = parsed["source"]
    repository = initial["repository"]
    _validate_source_worktree(
        repository=repository,
        source_worktree=source["worktree"],
        source_branch=source["branch"],
        base_sha=source["base_sha"],
    )
    try:
        target = manager.prepare_clean(
            repository,
            source["base_sha"],
            parsed["task_id"],
            parsed["node_id"],
            target_attempt,
        )
        dependency_input = _restore_dependency_input(
            store,
            manager,
            parsed,
            target,
        )
        patch = _verified_artifact_bytes(
            store, source["patch_ref"], "accepted-source patch"
        )
        if sha256(patch).hexdigest() != source["patch_sha256"]:
            raise AcceptedSourceRepairError(
                "accepted-source repair patch artifact hash does not match its binding"
        )
        manager.apply_patch(target, store.artifacts.verify(source["patch_ref"]))
        dependency_input = _apply_ready_lockfile_handoffs(
            store,
            parsed,
            target,
            dependency_input,
        )
        restored_patch = manager.diff_patch(target, dependency_input.input_tree_sha)
        if restored_patch != patch:
            raise AcceptedSourceRepairError(
                "accepted-source repair target does not match its recorded owner patch"
            )
    except (DependencyInputError, WorktreeError, OSError, ValueError) as error:
        if isinstance(error, AcceptedSourceRepairError):
            raise
        raise AcceptedSourceRepairError(
            f"accepted-source repair preparation failed: {error}"
        ) from error

    final = _preparation_snapshot(store, parsed, target_attempt)
    if canonical_json(initial) != canonical_json(final):
        raise AcceptedSourceRepairError(
            "accepted-source repair durable binding changed during target preparation"
        )
    target_branch = WorktreeManager.branch_name(
        parsed["task_id"], parsed["node_id"], target_attempt
    )
    receipt: AcceptedSourceRepairPreparedReceipt = {
        "schema_version": 1,
        "kind": ACCEPTED_SOURCE_REPAIR_KIND,
        "state": "prepared",
        "authorization_revision": parsed["authorization_revision"],
        "source_allocation_id": parsed["source_allocation_id"],
        "source_attempt": source["attempt"],
        "source_status": parsed["source_status"],
        "target_attempt": target_attempt,
        "target_worktree": str(target),
        "target_branch": target_branch,
        "dependency_input_ref": source["dependency_input_ref"],
        "dependency_input_tree_sha": dependency_input.input_tree_sha,
        "patch_ref": source["patch_ref"],
        "patch_sha256": source["patch_sha256"],
        "binding_sha256": sha256(canonical_json(parsed).encode()).hexdigest(),
    }
    return PreparedAcceptedSourceRepair(target, dependency_input, receipt)


def _node_rows(
    task_id: str, rows: Sequence[sqlite3.Row]
) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        node_id = _text(row["node_id"], "accepted-source node_id")
        if node_id in nodes:
            raise AcceptedSourceRepairError(
                f"accepted-source repair task duplicates node {node_id}"
            )
        spec = _json_object(row["spec_json"], f"accepted-source node {node_id} spec")
        if spec.get("task_id") != task_id or spec.get("node_id") != node_id:
            raise AcceptedSourceRepairError(
                f"accepted-source repair node {node_id} specification identity is invalid"
            )
        nodes[node_id] = {"row": row, "spec": spec}
    return nodes


def _dependencies(nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    dependencies: dict[str, tuple[str, ...]] = {}
    for node_id, node in nodes.items():
        raw = node["spec"].get("depends_on")
        if not isinstance(raw, list) or not all(
            isinstance(value, str) and value for value in raw
        ):
            raise AcceptedSourceRepairError(
                f"accepted-source repair node {node_id} dependencies are invalid"
            )
        if len(set(raw)) != len(raw):
            raise AcceptedSourceRepairError(
                f"accepted-source repair node {node_id} dependencies are duplicated"
            )
        for dependency in raw:
            if dependency not in nodes:
                raise AcceptedSourceRepairError(
                    f"accepted-source repair dependency {dependency} is missing"
                )
        dependencies[node_id] = tuple(raw)
    return dependencies


def _assert_acyclic(dependencies: Mapping[str, tuple[str, ...]]) -> None:
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise AcceptedSourceRepairError("accepted-source repair task graph contains a cycle")
        visiting.add(node_id)
        for parent in dependencies[node_id]:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in dependencies:
        visit(node_id)


def _require_repaired_accepted_descendants(
    nodes: Mapping[str, Mapping[str, Any]],
    dependencies: Mapping[str, tuple[str, ...]],
    requested: set[str],
) -> None:
    children: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for node_id, parents in dependencies.items():
        for parent in parents:
            children[parent].add(node_id)
    for source in requested:
        pending = list(children[source])
        visited: set[str] = set()
        while pending:
            descendant = pending.pop()
            if descendant in visited:
                continue
            visited.add(descendant)
            if (
                nodes[descendant]["row"]["state"] == "accepted"
                and descendant not in requested
            ):
                raise AcceptedSourceRepairError(
                    "accepted-source repair omits accepted descendant " + descendant
                )
            pending.extend(children[descendant])


def _task_snapshot(
    task_id: str,
    contract: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    snapshot_nodes: list[dict[str, Any]] = []
    for node_id in sorted(nodes):
        node = nodes[node_id]
        row = node["row"]
        raw_result = row["result_json"]
        if raw_result is None:
            result = None
        else:
            result = _json_object(
                raw_result, f"accepted-source task node {node_id} result"
            )
        snapshot_nodes.append(
            {
                **node["spec"],
                "state": str(row["state"]),
                "attempt": int(row["attempt"]),
                "result": result,
            }
        )
    return {
        "task_id": task_id,
        "contract": dict(contract),
        "nodes": snapshot_nodes,
    }


def _accepted_source_result(raw: object, node_id: str) -> dict[str, Any]:
    result = _json_object(raw, f"accepted-source owner {node_id} result")
    if result.get("status") != "succeeded":
        raise AcceptedSourceRepairError(
            f"accepted-source owner {node_id} source result is not succeeded"
        )
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in artifacts.items()
    ):
        raise AcceptedSourceRepairError(
            f"accepted-source owner {node_id} source artifacts are invalid"
        )
    changed_paths = result.get("changed_paths")
    if (
        not isinstance(changed_paths, list)
        or not changed_paths
        or not all(isinstance(path, str) and path for path in changed_paths)
        or changed_paths != sorted(set(changed_paths))
    ):
        raise AcceptedSourceRepairError(
            f"accepted-source owner {node_id} source changed paths are invalid"
        )
    return result


def _validate_source_allocation(
    allocation: sqlite3.Row | None,
    *,
    task_id: str,
    node_id: str,
    attempt: int,
    worktree: object,
    repository: str,
    base_sha: str,
) -> None:
    if allocation is None:
        raise AcceptedSourceRepairError(
            f"accepted-source owner {node_id} has no source allocation"
        )
    expected_branch = WorktreeManager.branch_name(task_id, node_id, attempt)
    if (
        allocation["state"] != "active"
        or not isinstance(worktree, str)
        or not worktree
        or allocation["current_path"] != worktree
        or allocation["repository"] != repository
        or allocation["base_sha"] != base_sha
        or allocation["branch"] != expected_branch
        or int(allocation["attempt"]) != attempt
    ):
        raise AcceptedSourceRepairError(
            f"accepted-source owner {node_id} allocation does not match its accepted attempt"
        )


def _verified_artifact_bytes(store: WorkbenchStore, ref: str, label: str) -> bytes:
    try:
        return store.artifacts.verify(ref).read_bytes()
    except (OSError, ValueError) as error:
        raise AcceptedSourceRepairError(f"{label} is unavailable or invalid") from error


def _source(raw: object, task_id: str, node_id: str) -> AcceptedSourceRepairSource:
    if not isinstance(raw, Mapping):
        raise AcceptedSourceRepairError("accepted-source repair source is invalid")
    required = {
        "attempt",
        "worktree",
        "branch",
        "base_sha",
        "patch_ref",
        "patch_sha256",
        "dependency_input_ref",
        "dependency_input",
    }
    if set(raw) != required:
        raise AcceptedSourceRepairError(
            "accepted-source repair source has an invalid shape"
        )
    attempt = _positive_int(raw["attempt"], "accepted-source source attempt")
    worktree = _text(raw["worktree"], "accepted-source source worktree")
    branch = _text(raw["branch"], "accepted-source source branch")
    base_sha = _text(raw["base_sha"], "accepted-source source base")
    patch_ref = _text(raw["patch_ref"], "accepted-source source patch ref")
    patch_sha256 = raw["patch_sha256"]
    if not isinstance(patch_sha256, str) or _SHA256.fullmatch(patch_sha256) is None:
        raise AcceptedSourceRepairError("accepted-source source patch_sha256 is invalid")
    dependency_input_ref = raw["dependency_input_ref"]
    if dependency_input_ref is not None:
        dependency_input_ref = _text(
            dependency_input_ref, "accepted-source source dependency input ref"
        )
    dependency_input = raw["dependency_input"]
    if dependency_input is not None and not isinstance(dependency_input, Mapping):
        raise AcceptedSourceRepairError(
            "accepted-source source dependency input receipt is invalid"
        )
    return {
        "attempt": attempt,
        "worktree": worktree,
        "branch": branch,
        "base_sha": base_sha,
        "patch_ref": patch_ref,
        "patch_sha256": patch_sha256,
        "dependency_input_ref": dependency_input_ref,
        "dependency_input": (
            dict(dependency_input) if isinstance(dependency_input, Mapping) else None
        ),
    }


def _validate_frozen_dependency_receipt(
    receipt: Mapping[str, Any],
    *,
    task_id: str,
    node_id: str,
    base_sha: str,
) -> None:
    base_required = {
        "schema_version",
        "kind",
        "task_id",
        "node_id",
        "contract_base_sha",
        "input_tree_sha",
        "ancestors",
    }
    schema_version = receipt.get("schema_version")
    required = (
        base_required
        if schema_version == 1
        else base_required | {"lockfile_handoffs"}
        if schema_version == 2
        else set()
    )
    if not required or set(receipt) != required:
        raise AcceptedSourceRepairError(
            "accepted-source repair dependency input receipt has an invalid shape"
        )
    if (
        receipt["kind"] != "accepted-ancestor-patch-input"
        or receipt["task_id"] != task_id
        or receipt["node_id"] != node_id
        or receipt["contract_base_sha"] != base_sha
        or not isinstance(receipt["input_tree_sha"], str)
        or not receipt["input_tree_sha"]
        or not isinstance(receipt["ancestors"], list)
    ):
        raise AcceptedSourceRepairError(
            "accepted-source repair dependency input receipt is invalid"
        )
    for ancestor in receipt["ancestors"]:
        if not isinstance(ancestor, Mapping) or set(ancestor) != {
            "node_id", "attempt", "patch_ref"
        }:
            raise AcceptedSourceRepairError(
                "accepted-source repair dependency input ancestor is invalid"
            )
        _text(ancestor["node_id"], "accepted-source dependency ancestor node")
        _positive_int(ancestor["attempt"], "accepted-source dependency ancestor attempt")
        if ancestor["patch_ref"] is not None:
            _text(ancestor["patch_ref"], "accepted-source dependency ancestor patch")
    if schema_version == 2:
        handoffs = receipt["lockfile_handoffs"]
        if (
            isinstance(handoffs, (str, bytes))
            or not isinstance(handoffs, list)
            or not handoffs
            or not all(isinstance(handoff, Mapping) for handoff in handoffs)
        ):
            raise AcceptedSourceRepairError(
                "accepted-source repair dependency input lockfile handoffs are invalid"
            )


def _preparation_snapshot(
    store: WorkbenchStore,
    binding: AcceptedSourceRepairBinding,
    target_attempt: int,
) -> dict[str, Any]:
    """Read and validate durable state before or after filesystem preparation."""

    with store.connection() as connection:
        task = connection.execute(
            "SELECT contract_json FROM tasks WHERE task_id = ?", (binding["task_id"],)
        ).fetchone()
        node = connection.execute(
            """
            SELECT state, attempt, worktree, spec_json, recovery_json
            FROM nodes WHERE task_id = ? AND node_id = ?
            """,
            (binding["task_id"], binding["node_id"]),
        ).fetchone()
        allocation = connection.execute(
            """
            SELECT allocation_id, state, repository, base_sha, branch, current_path, attempt
            FROM worktree_allocations WHERE allocation_id = ?
            """,
            (binding["source_allocation_id"],),
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
    contract = _json_object(task["contract_json"], "accepted-source repair task contract")
    _require_local_only_contract(contract)
    repository = _text(contract.get("repository"), "accepted-source repair repository")
    if contract.get("base_sha") != binding["source"]["base_sha"]:
        raise AcceptedSourceRepairError(
            "accepted-source repair contract base changed after authorization"
        )
    spec = _json_object(node["spec_json"], "accepted-source repair target spec")
    if spec.get("verifier") is True:
        raise AcceptedSourceRepairError("accepted-source repair target became a verifier")
    if node["state"] != "running" or int(node["attempt"]) != target_attempt:
        raise AcceptedSourceRepairError("accepted-source repair target lease is stale")
    if node["worktree"] is not None:
        raise AcceptedSourceRepairError("accepted-source repair target already has a worktree")
    current = parse_accepted_source_repair_binding(
        node["recovery_json"], next_attempt=target_attempt
    )
    if current is None or canonical_json(current) != canonical_json(binding):
        raise AcceptedSourceRepairError(
            "accepted-source repair target binding changed before preparation"
        )
    _validate_source_allocation(
        allocation,
        task_id=binding["task_id"],
        node_id=binding["node_id"],
        attempt=binding["source"]["attempt"],
        worktree=binding["source"]["worktree"],
        repository=repository,
        base_sha=binding["source"]["base_sha"],
    )
    if target_allocation is not None:
        raise AcceptedSourceRepairError(
            "accepted-source repair target allocation already exists"
        )
    if binding["source"]["dependency_input_ref"] is None and spec.get("depends_on"):
        raise AcceptedSourceRepairError(
            "accepted-source repair dependent owner lacks recorded dependency input"
        )
    return {
        "repository": repository,
        "contract_base_sha": binding["source"]["base_sha"],
        "node_state": str(node["state"]),
        "node_attempt": int(node["attempt"]),
        "node_recovery_json": str(node["recovery_json"]),
        "source_allocation": {
            "allocation_id": str(allocation["allocation_id"]),
            "state": str(allocation["state"]),
            "repository": str(allocation["repository"]),
            "base_sha": str(allocation["base_sha"]),
            "branch": str(allocation["branch"]),
            "current_path": str(allocation["current_path"]),
            "attempt": int(allocation["attempt"]),
        },
    }


def _validate_source_worktree(
    *,
    repository: str,
    source_worktree: str,
    source_branch: str,
    base_sha: str,
) -> None:
    try:
        source = Path(source_worktree).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AcceptedSourceRepairError(
            "accepted-source repair source worktree is unavailable"
        ) from error
    if not source.is_dir():
        raise AcceptedSourceRepairError("accepted-source repair source worktree is not a directory")
    repository_path = Path(repository).expanduser().resolve(strict=True)
    expected_base = _git_text(repository_path, "rev-parse", f"{base_sha}^{{commit}}")
    if _git_text(source, "rev-parse", "HEAD") != expected_base:
        raise AcceptedSourceRepairError(
            "accepted-source repair source worktree is not at its contract base"
        )
    if _git_text(source, "branch", "--show-current") != source_branch:
        raise AcceptedSourceRepairError(
            "accepted-source repair source worktree branch changed after authorization"
        )
    repository_common = _git_text(
        repository_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    source_common = _git_text(
        source, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if Path(repository_common).resolve() != Path(source_common).resolve():
        raise AcceptedSourceRepairError(
            "accepted-source repair source worktree belongs to another repository"
        )


def _restore_dependency_input(
    store: WorkbenchStore,
    manager: WorktreeManager,
    binding: AcceptedSourceRepairBinding,
    target: Path,
) -> DependencyInput:
    source = binding["source"]
    dependency_ref = source["dependency_input_ref"]
    if dependency_ref is None:
        return base_dependency_input(
            task_id=binding["task_id"],
            node_id=binding["node_id"],
            base_sha=source["base_sha"],
            worktree=target,
        )
    try:
        dependency_input = apply_recorded_dependency_input(
            store.artifacts,
            manager,
            ref=dependency_ref,
            task_id=binding["task_id"],
            node_id=binding["node_id"],
            base_sha=source["base_sha"],
            worktree=target,
        )
    except (DependencyInputError, ValueError, WorktreeError) as error:
        raise AcceptedSourceRepairError(
            f"accepted-source repair cannot reproduce recorded dependency input: {error}"
        ) from error
    frozen = source["dependency_input"]
    assert frozen is not None
    if canonical_json(dependency_input.receipt) != canonical_json(frozen):
        raise AcceptedSourceRepairError(
            "accepted-source repair dependency input artifact drifted from its binding"
        )
    return dependency_input


def _apply_ready_lockfile_handoffs(
    store: WorkbenchStore,
    binding: AcceptedSourceRepairBinding,
    target: Path,
    dependency_input: DependencyInput,
) -> DependencyInput:
    """Derive the claimed repair input before replaying its source patch."""

    try:
        from .lockfile_handoff import get_ready_lockfile_handoffs

        task = store.get_task(binding["task_id"])
        return apply_ready_lockfile_handoffs_to_dependency_input(
            task,
            binding["node_id"],
            target,
            store.artifacts,
            dependency_input,
            ready_lockfile_handoffs=get_ready_lockfile_handoffs(
                store,
                binding["task_id"],
            ),
            manifest_phase="prepared",
        )
    except (DependencyInputError, StateConflictError, ValueError) as error:
        raise AcceptedSourceRepairError(
            f"accepted-source repair cannot apply ready lockfile handoff: {error}"
        ) from error


def _git_text(worktree: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AcceptedSourceRepairError(
            f"accepted-source repair Git inspection failed: {error}"
        ) from error
    if result.returncode:
        raise AcceptedSourceRepairError(
            result.stderr.strip()
            or result.stdout.strip()
            or "accepted-source repair Git inspection failed"
        )
    return result.stdout.strip()


def _repair_node_ids(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise AcceptedSourceRepairError("accepted-source repair_node_ids are invalid")
    values = tuple(_text(value, "accepted-source repair node id") for value in raw)
    if not values:
        raise AcceptedSourceRepairError("accepted-source repair_node_ids are empty")
    if len(set(values)) != len(values):
        raise AcceptedSourceRepairError("accepted-source repair_node_ids are duplicated")
    return values


def _require_local_only_contract(contract: Mapping[str, Any]) -> None:
    if (
        contract.get("external_write_permission") is not False
        or contract.get("destructive_action_permission") is not False
    ):
        raise AcceptedSourceRepairError(
            "accepted-source repair requires no external or destructive permission"
        )


def _json_object(raw: object, label: str) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AcceptedSourceRepairError(f"{label} is invalid JSON") from error
    else:
        value = raw
    if not isinstance(value, dict):
        raise AcceptedSourceRepairError(f"{label} is invalid")
    return value


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise AcceptedSourceRepairError(f"{label} is invalid")
    return raw


def _positive_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise AcceptedSourceRepairError(f"{label} is invalid")
    return raw
