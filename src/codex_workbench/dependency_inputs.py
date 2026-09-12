from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .artifacts import ArtifactStore
from .lockfile_handoff_input import (
    LockfileHandoff,
    LockfileHandoffInputError,
    apply_pending_lockfile_handoffs,
    derive_lockfile_handoff_input_tree,
    normalize_lockfile_handoffs,
    recorded_handoff_identity,
    require_all_handoffs_applied,
    select_lockfile_handoffs_for_target,
    validate_lockfile_handoff_manifests,
)
from .worktrees import WorktreeError, WorktreeManager


class DependencyInputError(WorktreeError):
    """An accepted dependency cannot provide a reproducible worker input."""


_DEPENDENCY_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "task_id",
        "node_id",
        "contract_base_sha",
        "input_tree_sha",
        "ancestors",
    }
)
_DEPENDENCY_INPUT_SCHEMA_2_FIELDS = _DEPENDENCY_INPUT_FIELDS | {"lockfile_handoffs"}


@dataclass(frozen=True)
class DependencyInput:
    """Immutable ancestor patch input applied before one node executes."""

    input_tree_sha: str
    receipt: dict[str, Any]


def validate_dependency_input_lineage(
    task: Mapping[str, Any],
    node_id: str,
    dependency_input: DependencyInput,
    *,
    artifacts: ArtifactStore | None = None,
) -> None:
    """Require one loaded dependency input to match the current accepted closure.

    The task snapshot is authoritative for the ordered ancestor source records.
    A receipt from another task, node, contract base, or accepted-attempt
    closure is rejected before it can be used as worker input.
    """

    if not isinstance(task, Mapping):
        raise DependencyInputError("dependency task snapshot is invalid")
    if not isinstance(node_id, str) or not node_id:
        raise DependencyInputError("dependency target node is invalid")
    if not isinstance(dependency_input, DependencyInput):
        raise DependencyInputError("dependency input is invalid")
    task_id = task.get("task_id")
    contract = task.get("contract")
    if not isinstance(task_id, str) or not task_id:
        raise DependencyInputError("dependency task snapshot has an invalid task id")
    if not isinstance(contract, Mapping):
        raise DependencyInputError("dependency task snapshot has an invalid contract")
    base_sha = contract.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise DependencyInputError("dependency task snapshot lacks contract base")

    receipt = dependency_input.receipt
    if not isinstance(receipt, Mapping):
        raise DependencyInputError("dependency input receipt is invalid")
    if receipt.get("task_id") != task_id:
        raise DependencyInputError("dependency input receipt belongs to another task")
    if receipt.get("node_id") != node_id:
        raise DependencyInputError("dependency input receipt belongs to another node")
    if receipt.get("contract_base_sha") != base_sha:
        raise DependencyInputError("dependency input receipt has another contract base")
    input_tree_sha = receipt.get("input_tree_sha")
    if input_tree_sha != dependency_input.input_tree_sha:
        raise DependencyInputError("dependency input receipt tree does not match its input")
    if not isinstance(input_tree_sha, str) or not input_tree_sha:
        raise DependencyInputError("dependency input receipt tree is invalid")

    raw_ancestors = receipt.get("ancestors")
    if not isinstance(raw_ancestors, list):
        raise DependencyInputError("dependency input receipt ancestors are invalid")
    actual: list[tuple[str, int, str | None]] = []
    for source in raw_ancestors:
        if not isinstance(source, Mapping) or set(source) != {"node_id", "attempt", "patch_ref"}:
            raise DependencyInputError("dependency input receipt ancestor is invalid")
        source_node_id = source["node_id"]
        attempt = source["attempt"]
        patch_ref = source["patch_ref"]
        if not isinstance(source_node_id, str) or not source_node_id:
            raise DependencyInputError("dependency input receipt ancestor node is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise DependencyInputError("dependency input receipt ancestor attempt is invalid")
        if patch_ref is not None and (not isinstance(patch_ref, str) or not patch_ref):
            raise DependencyInputError("dependency input receipt ancestor patch is invalid")
        actual.append((source_node_id, attempt, patch_ref))

    expected = tuple(
        (
            source["node_id"],
            source["attempt"],
            source["patch_ref"],
        )
        for ancestor in accepted_ancestor_nodes(task, node_id)
        for source in (_source_receipt(ancestor),)
    )
    if tuple(actual) != expected:
        raise DependencyInputError(
            "dependency input receipt ancestor lineage does not match accepted closure"
        )
    handoffs = _receipt_lockfile_handoffs(receipt)
    if not handoffs:
        return
    if artifacts is None:
        raise DependencyInputError(
            "lockfile dependency input validation requires an artifact store"
        )
    try:
        selected = select_lockfile_handoffs_for_target(
            artifacts,
            task,
            node_id,
            ancestors=raw_ancestors,
            ready_handoffs=handoffs,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"lockfile dependency input is invalid: {error}"
        ) from error
    if len(selected) != len(handoffs):
        raise DependencyInputError(
            "dependency input includes a lockfile handoff that does not apply to its target"
        )


def base_dependency_input(
    *,
    task_id: str,
    node_id: str,
    base_sha: str,
    worktree: Path,
) -> DependencyInput:
    """Create the immutable empty-ancestor lineage for a base-only worker.

    Normal workers without dependencies historically had no input artifact.
    A dirty failed retry needs one so tracked and explicitly preserved
    untracked changes can be replayed against the exact original input tree.
    """

    input_tree_sha = _resolve_tree(worktree, base_sha)
    return DependencyInput(
        input_tree_sha=input_tree_sha,
        receipt={
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": task_id,
            "node_id": node_id,
            "contract_base_sha": base_sha,
            "input_tree_sha": input_tree_sha,
            "ancestors": [],
        },
    )


def load_recorded_dependency_input(
    artifacts: ArtifactStore,
    ref: str,
    *,
    task_id: str,
    node_id: str,
    base_sha: str,
) -> DependencyInput:
    """Load one immutable dependency-input receipt recorded for a worker.

    A later recovery must use the exact ancestor closure visible to the
    original worker, rather than whichever accepted nodes happen to exist at
    recovery time. The content-addressed receipt is therefore both the
    provenance record and the replay recipe.
    """

    if not isinstance(ref, str) or not ref:
        raise DependencyInputError("recorded dependency input ref is invalid")
    try:
        payload = json.loads(artifacts.verify(ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise DependencyInputError(
            "recorded dependency input artifact is unavailable or invalid"
        ) from error
    if not isinstance(payload, dict):
        raise DependencyInputError("recorded dependency input must be an object")
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        required = _DEPENDENCY_INPUT_FIELDS
    elif schema_version == 2:
        required = _DEPENDENCY_INPUT_SCHEMA_2_FIELDS
    else:
        raise DependencyInputError("recorded dependency input schema is unsupported")
    if set(payload) != required:
        raise DependencyInputError("recorded dependency input has an invalid shape")
    if payload["kind"] != "accepted-ancestor-patch-input":
        raise DependencyInputError("recorded dependency input schema is unsupported")
    if payload["task_id"] != task_id or payload["node_id"] != node_id:
        raise DependencyInputError("recorded dependency input belongs to another node")
    if payload["contract_base_sha"] != base_sha:
        raise DependencyInputError("recorded dependency input has another contract base")
    input_tree_sha = payload["input_tree_sha"]
    if not isinstance(input_tree_sha, str) or not input_tree_sha:
        raise DependencyInputError("recorded dependency input tree is invalid")
    ancestors = payload["ancestors"]
    if not isinstance(ancestors, list):
        raise DependencyInputError("recorded dependency input ancestors are invalid")
    normalized_ancestors: list[dict[str, Any]] = []
    for source in ancestors:
        if not isinstance(source, dict) or set(source) != {"node_id", "attempt", "patch_ref"}:
            raise DependencyInputError("recorded dependency input ancestor is invalid")
        source_node_id = source["node_id"]
        attempt = source["attempt"]
        patch_ref = source["patch_ref"]
        if not isinstance(source_node_id, str) or not source_node_id:
            raise DependencyInputError("recorded dependency input ancestor node is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise DependencyInputError("recorded dependency input ancestor attempt is invalid")
        if patch_ref is not None:
            if not isinstance(patch_ref, str) or not patch_ref:
                raise DependencyInputError("recorded dependency input ancestor patch is invalid")
            try:
                artifacts.verify(patch_ref)
            except ValueError as error:
                raise DependencyInputError(
                    "recorded dependency input ancestor patch is unavailable"
                ) from error
        normalized_ancestors.append(
            {"node_id": source_node_id, "attempt": attempt, "patch_ref": patch_ref}
        )
    receipt: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": "accepted-ancestor-patch-input",
        "task_id": task_id,
        "node_id": node_id,
        "contract_base_sha": base_sha,
        "input_tree_sha": input_tree_sha,
        "ancestors": normalized_ancestors,
    }
    if schema_version == 2:
        raw_handoffs = payload["lockfile_handoffs"]
        try:
            handoffs = normalize_lockfile_handoffs(
                artifacts,
                raw_handoffs,
                task_id=task_id,
            )
        except LockfileHandoffInputError as error:
            raise DependencyInputError(
                f"recorded dependency input lockfile handoffs are invalid: {error}"
            ) from error
        if not handoffs:
            raise DependencyInputError(
                "recorded dependency input schema 2 requires a lockfile handoff"
            )
        receipt["lockfile_handoffs"] = [
            dict(handoff.receipt) for handoff in handoffs
        ]
    return DependencyInput(input_tree_sha=input_tree_sha, receipt=receipt)


def apply_recorded_dependency_input(
    artifacts: ArtifactStore,
    manager: WorktreeManager,
    *,
    ref: str,
    task_id: str,
    node_id: str,
    base_sha: str,
    worktree: Path,
) -> DependencyInput:
    """Materialize one recorded dependency-input receipt onto a clean tree."""

    dependency_input = load_recorded_dependency_input(
        artifacts,
        ref,
        task_id=task_id,
        node_id=node_id,
        base_sha=base_sha,
    )
    _require_clean_worktree(worktree)
    handoffs = _normalized_recorded_handoffs(artifacts, dependency_input)
    applied_request_ids: set[str] = set()
    applied_sources: list[dict[str, Any]] = []
    try:
        apply_pending_lockfile_handoffs(
            worktree,
            artifacts,
            handoffs,
            applied_sources,
            applied_request_ids=applied_request_ids,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"cannot apply recorded lockfile handoff input: {error}"
        ) from error
    for source in dependency_input.receipt["ancestors"]:
        patch_ref = source["patch_ref"]
        if patch_ref is None:
            applied_sources.append(source)
        else:
            try:
                manager.apply_patch(worktree, artifacts.verify(patch_ref))
            except (ValueError, WorktreeError) as error:
                raise DependencyInputError(
                    f"cannot apply recorded dependency {source['node_id']} patch"
                ) from error
            applied_sources.append(source)
        try:
            apply_pending_lockfile_handoffs(
                worktree,
                artifacts,
                handoffs,
                applied_sources,
                applied_request_ids=applied_request_ids,
            )
        except LockfileHandoffInputError as error:
            raise DependencyInputError(
                f"cannot apply recorded lockfile handoff input: {error}"
            ) from error
    try:
        require_all_handoffs_applied(
            worktree,
            artifacts,
            handoffs,
            applied_request_ids=applied_request_ids,
        )
        actual_tree = (
            derive_lockfile_handoff_input_tree(
                worktree,
                write_input_tree(worktree),
                artifacts,
                handoffs,
            )
            if handoffs
            else write_input_tree(worktree)
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"recorded dependency input lockfile handoff did not reproduce: {error}"
        ) from error
    if actual_tree != dependency_input.input_tree_sha:
        raise DependencyInputError(
            "recorded dependency input did not reproduce its input tree"
        )
    return dependency_input


def reconstruct_prepared_dependency_input(
    task: Mapping[str, Any], node_id: str, worktree: Path, artifacts: ArtifactStore,
) -> DependencyInput:
    """Reproduce accepted patches in a private index and require the staged input to match.

    This recovers a completed preparation whose receipt was not saved before
    package installation failed. It never stages worker edits or changes the
    live index, files, branch, or task record. A differing staged tree is not
    evidence of the historical input and is rejected.
    """

    sources = [_source_receipt(node) for node in accepted_ancestor_nodes(task, node_id)]
    base_sha = task["contract"]["base_sha"]
    with tempfile.TemporaryDirectory(prefix="workbench-prepared-input-") as temporary:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}

        def git(*args: str, data: bytes | None = None) -> bytes:
            result = subprocess.run(
                ["git", "-C", str(worktree), *args], input=data,
                capture_output=True, env=environment, timeout=60,
            )
            if result.returncode:
                raise DependencyInputError("accepted preparation could not be reproduced in a private index")
            return result.stdout

        git("read-tree", base_sha)
        for source in sources:
            if source["patch_ref"] is not None:
                patch = artifacts.verify(source["patch_ref"]).read_bytes()
                if patch.strip():
                    git("apply", "--cached", "--binary", "-", data=patch)
        tree = git("write-tree").decode("ascii").strip()
    if _git_bytes(worktree, "diff", "--cached", "--name-only", tree, "--"):
        raise DependencyInputError("staged source does not match reconstructed accepted input")
    dependency_input = DependencyInput(tree, {
        "schema_version": 1, "kind": "accepted-ancestor-patch-input",
        "task_id": task["task_id"], "node_id": node_id,
        "contract_base_sha": base_sha, "input_tree_sha": tree, "ancestors": sources,
    })
    validate_dependency_input_lineage(task, node_id, dependency_input, artifacts=artifacts)
    return dependency_input


def apply_accepted_ancestor_patches(
    task: Mapping[str, Any],
    node_id: str,
    worktree: Path,
    artifacts: ArtifactStore,
    manager: WorktreeManager,
    *,
    ready_lockfile_handoffs: object = (),
) -> DependencyInput | None:
    """Apply just ``node_id``'s accepted dependency closure to ``worktree``.

    Patch refs are read from the same task snapshot only.  A post-order walk
    gives every transitive ancestor one application before its descendants.
    ``git write-tree`` snapshots that inherited state without changing either
    the contract base or the worktree branch's commit history.
    """

    ancestors = accepted_ancestor_nodes(task, node_id)
    sources = [_source_receipt(ancestor) for ancestor in ancestors]
    try:
        handoffs = select_lockfile_handoffs_for_target(
            artifacts,
            task,
            node_id,
            ancestors=sources,
            ready_handoffs=ready_lockfile_handoffs,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"lockfile handoff input is unavailable: {error}"
        ) from error
    if not ancestors and not handoffs:
        return None
    _require_clean_worktree(worktree)
    applied_sources: list[dict[str, Any]] = []
    applied_request_ids: set[str] = set()
    try:
        apply_pending_lockfile_handoffs(
            worktree,
            artifacts,
            handoffs,
            applied_sources,
            applied_request_ids=applied_request_ids,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"cannot apply lockfile handoff input: {error}"
        ) from error
    for source in sources:
        patch_ref = source["patch_ref"]
        if patch_ref is not None:
            try:
                patch = artifacts.verify(patch_ref)
            except ValueError as error:
                raise DependencyInputError(
                    f"accepted dependency {source['node_id']} has invalid patch artifact"
                ) from error
            try:
                manager.apply_patch(worktree, patch)
            except WorktreeError as error:
                raise DependencyInputError(
                    f"cannot apply accepted dependency {source['node_id']} patch"
                ) from error
        applied_sources.append(source)
        try:
            apply_pending_lockfile_handoffs(
                worktree,
                artifacts,
                handoffs,
                applied_sources,
                applied_request_ids=applied_request_ids,
            )
        except LockfileHandoffInputError as error:
            raise DependencyInputError(
                f"cannot apply lockfile handoff input: {error}"
            ) from error
    try:
        require_all_handoffs_applied(
            worktree,
            artifacts,
            handoffs,
            applied_request_ids=applied_request_ids,
        )
        input_tree_sha = (
            derive_lockfile_handoff_input_tree(
                worktree,
                write_input_tree(worktree),
                artifacts,
                handoffs,
            )
            if handoffs
            else write_input_tree(worktree)
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"lockfile handoff input did not reproduce: {error}"
        ) from error
    task_id = task.get("task_id")
    contract = task.get("contract")
    if not isinstance(task_id, str) or not isinstance(contract, Mapping):
        raise DependencyInputError("dependency task snapshot is malformed")
    base_sha = contract.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise DependencyInputError("dependency task snapshot lacks contract base")
    receipt: dict[str, Any] = {
        "schema_version": 2 if handoffs else 1,
        "kind": "accepted-ancestor-patch-input",
        "task_id": task_id,
        "node_id": node_id,
        "contract_base_sha": base_sha,
        "input_tree_sha": input_tree_sha,
        "ancestors": sources,
    }
    if handoffs:
        receipt["lockfile_handoffs"] = [
            dict(handoff.receipt) for handoff in handoffs
        ]
    return DependencyInput(input_tree_sha=input_tree_sha, receipt=receipt)


def apply_ready_lockfile_handoffs_to_dependency_input(
    task: Mapping[str, Any],
    node_id: str,
    worktree: Path,
    artifacts: ArtifactStore,
    dependency_input: DependencyInput,
    *,
    ready_lockfile_handoffs: object,
    manifest_phase: str = "baseline",
) -> DependencyInput:
    """Derive a new input tree after replaying ready lockfile handoffs.

    Callers restore the supplied dependency input before this function. The
    function stages only the lockfile in a temporary index, so a dirty source
    patch remains a worker delta rather than becoming part of the derived
    baseline.
    """

    if not isinstance(dependency_input, DependencyInput):
        raise DependencyInputError("dependency input is invalid")
    receipt = dependency_input.receipt
    if not isinstance(receipt, Mapping):
        raise DependencyInputError("dependency input receipt is invalid")
    raw_ancestors = receipt.get("ancestors")
    if not isinstance(raw_ancestors, list):
        raise DependencyInputError("dependency input receipt ancestors are invalid")
    try:
        existing = _normalized_recorded_handoffs(artifacts, dependency_input)
        selected = select_lockfile_handoffs_for_target(
            artifacts,
            task,
            node_id,
            ancestors=raw_ancestors,
            ready_handoffs=ready_lockfile_handoffs,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"lockfile handoff input is unavailable: {error}"
        ) from error
    if not selected:
        return dependency_input
    try:
        validate_lockfile_handoff_manifests(
            worktree,
            selected,
            manifest_phase=manifest_phase,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"lockfile handoff input is unavailable: {error}"
        ) from error
    existing_by_identity = {
        recorded_handoff_identity(handoff): handoff for handoff in existing
    }
    additions: list[LockfileHandoff] = []
    for selected_handoff in selected:
        identity = recorded_handoff_identity(selected_handoff)
        recorded = existing_by_identity.get(identity)
        if recorded is not None:
            if recorded.receipt != selected_handoff.receipt:
                raise DependencyInputError(
                    "recorded lockfile handoff drifted from the durable ready receipt"
                )
            continue
        additions.append(selected_handoff)
    if not additions:
        return dependency_input
    combined = (*existing, *additions)
    try:
        # Revalidate ordering and every artifact before changing the worktree.
        normalized_combined = normalize_lockfile_handoffs(
            artifacts,
            [handoff.receipt for handoff in combined],
            task_id=str(receipt.get("task_id", "")),
            contract=task.get("contract") if isinstance(task.get("contract"), Mapping) else None,
            contract_hash=(
                task.get("contract_hash")
                if isinstance(task.get("contract_hash"), str)
                else None
            ),
        )
        applied_request_ids: set[str] = set()
        apply_pending_lockfile_handoffs(
            worktree,
            artifacts,
            additions,
            raw_ancestors,
            applied_request_ids=applied_request_ids,
            manifest_phase=manifest_phase,
        )
        require_all_handoffs_applied(
            worktree,
            artifacts,
            additions,
            applied_request_ids=applied_request_ids,
        )
        input_tree_sha = derive_lockfile_handoff_input_tree(
            worktree,
            dependency_input.input_tree_sha,
            artifacts,
            additions,
        )
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"cannot derive lockfile handoff input: {error}"
        ) from error
    derived = {
        "schema_version": 2,
        "kind": "accepted-ancestor-patch-input",
        "task_id": receipt.get("task_id"),
        "node_id": receipt.get("node_id"),
        "contract_base_sha": receipt.get("contract_base_sha"),
        "input_tree_sha": input_tree_sha,
        "ancestors": [dict(source) for source in raw_ancestors],
        "lockfile_handoffs": [
            dict(handoff.receipt) for handoff in normalized_combined
        ],
    }
    return DependencyInput(input_tree_sha=input_tree_sha, receipt=derived)


def accepted_ancestor_nodes(task: Mapping[str, Any], node_id: str) -> tuple[Mapping[str, Any], ...]:
    """Return the accepted transitive predecessors in deterministic topological order."""

    raw_nodes = task.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DependencyInputError("dependency task snapshot has no node list")
    nodes: dict[str, Mapping[str, Any]] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            raise DependencyInputError("dependency task snapshot contains an invalid node")
        candidate_id = raw_node.get("node_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise DependencyInputError("dependency task snapshot contains a node without an id")
        if candidate_id in nodes:
            raise DependencyInputError(f"dependency task snapshot duplicates node {candidate_id}")
        nodes[candidate_id] = raw_node
    target = nodes.get(node_id)
    if target is None:
        raise DependencyInputError(f"dependency target {node_id} is missing from task")

    ordered: list[Mapping[str, Any]] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(candidate_id: str) -> None:
        if candidate_id in visited:
            return
        if candidate_id in visiting:
            raise DependencyInputError("dependency task graph contains a cycle")
        candidate = nodes.get(candidate_id)
        if candidate is None:
            raise DependencyInputError(f"dependency {candidate_id} is missing from task")
        if candidate.get("state") != "accepted":
            raise DependencyInputError(
                f"dependency {candidate_id} is {candidate.get('state')!r}, expected accepted"
            )
        if not isinstance(candidate.get("result"), Mapping):
            raise DependencyInputError(f"accepted dependency {candidate_id} lacks a result")
        visiting.add(candidate_id)
        for parent_id in _dependencies(candidate):
            visit(parent_id)
        visiting.remove(candidate_id)
        visited.add(candidate_id)
        ordered.append(candidate)

    for dependency_id in _dependencies(target):
        visit(dependency_id)
    return tuple(ordered)


def changed_paths_since_input_tree(worktree: Path, input_tree_sha: str) -> set[str]:
    """Return the worker's delta from an inherited tree, including untracked files."""

    baseline = _resolve_tree(worktree, input_tree_sha)
    changed = _nul_paths(
        _git_bytes(worktree, "diff", "--name-only", "--no-renames", "-z", baseline, "--")
    )
    changed.update(
        _nul_paths(
            _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        )
    )
    return changed


def _receipt_lockfile_handoffs(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    schema_version = receipt.get("schema_version")
    if schema_version == 1:
        if set(receipt) != _DEPENDENCY_INPUT_FIELDS:
            raise DependencyInputError("dependency input receipt has an invalid shape")
        return []
    if schema_version != 2 or set(receipt) != _DEPENDENCY_INPUT_SCHEMA_2_FIELDS:
        raise DependencyInputError("dependency input receipt has an invalid shape")
    handoffs = receipt.get("lockfile_handoffs")
    if isinstance(handoffs, (str, bytes)) or not isinstance(handoffs, list) or not handoffs:
        raise DependencyInputError("dependency input lockfile handoffs are invalid")
    if not all(isinstance(handoff, dict) for handoff in handoffs):
        raise DependencyInputError("dependency input lockfile handoff is invalid")
    return handoffs


def _normalized_recorded_handoffs(
    artifacts: ArtifactStore,
    dependency_input: DependencyInput,
) -> tuple[LockfileHandoff, ...]:
    receipt = dependency_input.receipt
    if not isinstance(receipt, Mapping):
        raise DependencyInputError("dependency input receipt is invalid")
    handoffs = _receipt_lockfile_handoffs(receipt)
    if not handoffs:
        return ()
    task_id = receipt.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise DependencyInputError("dependency input receipt has an invalid task id")
    try:
        return normalize_lockfile_handoffs(artifacts, handoffs, task_id=task_id)
    except LockfileHandoffInputError as error:
        raise DependencyInputError(
            f"dependency input lockfile handoffs are invalid: {error}"
        ) from error


def effective_spec_with_dependency_input(
    spec: Mapping[str, Any], dependency_input: DependencyInput | None
) -> dict[str, Any]:
    """Return an ephemeral cache spec; durable NodeSpec remains untouched.

    The full receipt is provenance for the node result and intentionally
    contains task/node identity. Evidence reuse instead binds only the input
    content closure: fixed contract base, materialized tree, and ordered patch
    artifact refs.
    """

    effective = dict(spec)
    if dependency_input is not None:
        receipt = dependency_input.receipt
        dependency_spec: dict[str, Any] = {
            "contract_base_sha": receipt["contract_base_sha"],
            "input_tree_sha": dependency_input.input_tree_sha,
            "ancestor_patch_refs": tuple(
                source["patch_ref"] for source in receipt["ancestors"]
            ),
        }
        handoffs = (
            _receipt_lockfile_handoffs(receipt)
            if receipt.get("schema_version") in {1, 2}
            else []
        )
        if handoffs:
            dependency_spec["lockfile_handoff_refs"] = tuple(
                (
                    handoff["request_id"],
                    handoff["ready_event_cursor"],
                    handoff["lockfile"]["artifact_ref"],
                    handoff["lockfile"]["sha256"],
                )
                for handoff in handoffs
            )
        effective["dependency_input"] = dependency_spec
    return effective


def write_input_tree(worktree: Path) -> str:
    """Persist the current index tree and return its canonical object id."""

    return _resolve_tree(worktree, _git_text(worktree, "write-tree"))


def _dependencies(node: Mapping[str, Any]) -> tuple[str, ...]:
    raw_dependencies = node.get("depends_on", ())
    if not isinstance(raw_dependencies, (list, tuple)) or not all(
        isinstance(value, str) and value for value in raw_dependencies
    ):
        node_id = node.get("node_id", "<unknown>")
        raise DependencyInputError(f"node {node_id} has invalid dependencies")
    return tuple(raw_dependencies)


def _source_receipt(node: Mapping[str, Any]) -> dict[str, Any]:
    node_id = node.get("node_id")
    result = node.get("result")
    if not isinstance(node_id, str) or not isinstance(result, Mapping):
        raise DependencyInputError("accepted dependency snapshot is malformed")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise DependencyInputError(f"accepted dependency {node_id} lacks artifacts")
    patch_ref = artifacts.get("patch")
    if patch_ref is not None and (not isinstance(patch_ref, str) or not patch_ref):
        raise DependencyInputError(f"accepted dependency {node_id} has an invalid patch ref")
    changed_paths = result.get("changed_paths", ())
    if not isinstance(changed_paths, (list, tuple)) or not all(
        isinstance(path, str) for path in changed_paths
    ):
        raise DependencyInputError(f"accepted dependency {node_id} has invalid changed paths")
    if changed_paths and patch_ref is None:
        raise DependencyInputError(f"accepted dependency {node_id} lacks its patch artifact")
    attempt = node.get("attempt")
    if not isinstance(attempt, int):
        raise DependencyInputError(f"accepted dependency {node_id} has an invalid attempt")
    return {"node_id": node_id, "attempt": attempt, "patch_ref": patch_ref}


def _require_clean_worktree(worktree: Path) -> None:
    if _git_bytes(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise DependencyInputError("dependency input worktree is not clean")


def _resolve_tree(worktree: Path, value: str) -> str:
    if not value:
        raise DependencyInputError("dependency input tree is missing")
    return _git_text(worktree, "rev-parse", "--verify", f"{value}^{{tree}}")


def _git_text(worktree: Path, *arguments: str) -> str:
    return _git_bytes(worktree, *arguments).decode(errors="replace").strip()


def _git_bytes(worktree: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DependencyInputError(f"dependency input Git command failed: {error}") from error
    if result.returncode:
        raise DependencyInputError(
            result.stderr.decode(errors="replace").strip()
            or result.stdout.decode(errors="replace").strip()
        )
    return result.stdout


def _nul_paths(data: bytes) -> set[str]:
    return {
        raw.decode(errors="surrogateescape")
        for raw in data.split(b"\0")
        if raw
    }
