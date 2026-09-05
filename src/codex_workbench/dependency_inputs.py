from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from .artifacts import ArtifactStore
from .worktrees import WorktreeError, WorktreeManager


class DependencyInputError(WorktreeError):
    """An accepted dependency cannot provide a reproducible worker input."""


@dataclass(frozen=True)
class DependencyInput:
    """Immutable ancestor patch input applied before one node executes."""

    input_tree_sha: str
    receipt: dict[str, Any]


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
    required = {
        "schema_version",
        "kind",
        "task_id",
        "node_id",
        "contract_base_sha",
        "input_tree_sha",
        "ancestors",
    }
    if set(payload) != required:
        raise DependencyInputError("recorded dependency input has an invalid shape")
    if payload["schema_version"] != 1 or payload["kind"] != "accepted-ancestor-patch-input":
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
    return DependencyInput(
        input_tree_sha=input_tree_sha,
        receipt={
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": task_id,
            "node_id": node_id,
            "contract_base_sha": base_sha,
            "input_tree_sha": input_tree_sha,
            "ancestors": normalized_ancestors,
        },
    )


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
    for source in dependency_input.receipt["ancestors"]:
        patch_ref = source["patch_ref"]
        if patch_ref is None:
            continue
        try:
            manager.apply_patch(worktree, artifacts.verify(patch_ref))
        except (ValueError, WorktreeError) as error:
            raise DependencyInputError(
                f"cannot apply recorded dependency {source['node_id']} patch"
            ) from error
    actual_tree = write_input_tree(worktree)
    if actual_tree != dependency_input.input_tree_sha:
        raise DependencyInputError(
            "recorded dependency input did not reproduce its input tree"
        )
    return dependency_input


def apply_accepted_ancestor_patches(
    task: Mapping[str, Any],
    node_id: str,
    worktree: Path,
    artifacts: ArtifactStore,
    manager: WorktreeManager,
) -> DependencyInput | None:
    """Apply just ``node_id``'s accepted dependency closure to ``worktree``.

    Patch refs are read from the same task snapshot only.  A post-order walk
    gives every transitive ancestor one application before its descendants.
    ``git write-tree`` snapshots that inherited state without changing either
    the contract base or the worktree branch's commit history.
    """

    ancestors = accepted_ancestor_nodes(task, node_id)
    if not ancestors:
        return None
    _require_clean_worktree(worktree)
    sources: list[dict[str, Any]] = []
    for ancestor in ancestors:
        source = _source_receipt(ancestor)
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
        sources.append(source)
    input_tree_sha = write_input_tree(worktree)
    task_id = task.get("task_id")
    contract = task.get("contract")
    if not isinstance(task_id, str) or not isinstance(contract, Mapping):
        raise DependencyInputError("dependency task snapshot is malformed")
    base_sha = contract.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise DependencyInputError("dependency task snapshot lacks contract base")
    return DependencyInput(
        input_tree_sha=input_tree_sha,
        receipt={
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": task_id,
            "node_id": node_id,
            "contract_base_sha": base_sha,
            "input_tree_sha": input_tree_sha,
            "ancestors": sources,
        },
    )


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
        effective["dependency_input"] = {
            "contract_base_sha": receipt["contract_base_sha"],
            "input_tree_sha": dependency_input.input_tree_sha,
            "ancestor_patch_refs": tuple(
                source["patch_ref"] for source in receipt["ancestors"]
            ),
        }
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
