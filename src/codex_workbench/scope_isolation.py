"""Read-only scope-entity alias evidence for admission diagnostics.

This module deliberately reports only observed shared writable entities.  It
does not establish a concurrent-execution permission or inspect Git state.
"""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
from typing import Any

from .worktrees import normalize_scope


def scope_entity_alias_conflicts(
    *,
    candidate_worktree: str | Path,
    candidate_reads: Sequence[str],
    candidate_writes: Sequence[str],
    running_worktree: str | Path,
    running_reads: Sequence[str],
    running_writes: Sequence[str],
) -> dict[str, Any]:
    """Inspect conflicting accesses for one writable entity reached by aliases.

    Different repository-relative spellings can resolve through symlinks to one
    writable file or directory outside otherwise private worktrees.  This is
    deny-only evidence: ``conflict`` must block admission, while
    ``observed_distinct`` and ``unproven`` never authorize a concurrency
    exception.  Missing or unresolvable targets remain ``unproven``.

    Args:
        candidate_worktree: Existing candidate worktree root.
        candidate_reads: Candidate repository-relative read scopes.
        candidate_writes: Candidate repository-relative write scopes.
        running_worktree: Existing running worktree root.
        running_reads: Running repository-relative read scopes.
        running_writes: Running repository-relative write scopes.

    Returns:
        ``conflict`` with all observed shared writable entities,
        ``observed_distinct`` when every inspected pair is distinct, or
        ``unproven`` when live filesystem evidence is unavailable.  Only the
        first result is an admission decision, and it is a denial.
    """

    candidate_root = _existing_directory(candidate_worktree)
    if candidate_root is None:
        return {"status": "unproven", "reason": "candidate_worktree_unavailable"}
    running_root = _existing_directory(running_worktree)
    if running_root is None:
        return {"status": "unproven", "reason": "running_worktree_unavailable"}

    candidate_accesses = (
        *(("write", scope) for scope in candidate_writes),
        *(("read", scope) for scope in candidate_reads),
    )
    running_accesses = (
        *(("write", scope) for scope in running_writes),
        *(("read", scope) for scope in running_reads),
    )
    if not candidate_accesses or not running_accesses:
        return {"status": "unproven", "reason": "no_conflicting_accesses"}

    unresolved = False
    shared_not_writable = False
    conflicts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate_access, candidate_scope in candidate_accesses:
        candidate_entity, candidate_normalized = _scope_entity(candidate_root, candidate_scope)
        if candidate_entity is None:
            unresolved = True
            continue
        for running_access, running_scope in running_accesses:
            if candidate_access == running_access == "read":
                continue
            running_entity, running_normalized = _scope_entity(running_root, running_scope)
            if running_entity is None:
                unresolved = True
                continue
            if not _entities_overlap(candidate_entity, running_entity):
                continue
            writable_entities = (
                candidate_entity if candidate_access == "write" else None,
                running_entity if running_access == "write" else None,
            )
            if not any(
                entity is not None and _is_writable_entity(entity)
                for entity in writable_entities
            ):
                shared_not_writable = True
                continue
            identity = (
                candidate_access,
                candidate_normalized,
                running_access,
                running_normalized,
                str(candidate_entity),
            )
            if identity in seen:
                continue
            seen.add(identity)
            conflicts.append(
                {
                    "candidate_access": candidate_access,
                    "candidate_scope": candidate_normalized,
                    "running_access": running_access,
                    "running_scope": running_normalized,
                    "entity": str(candidate_entity),
                    "reason": "shared_writable_scope_entity",
                }
            )
    if conflicts:
        return {"status": "conflict", "conflicts": conflicts}
    if unresolved:
        return {"status": "unproven", "reason": "scope_entity_unavailable"}
    if shared_not_writable:
        return {"status": "unproven", "reason": "shared_entity_writability_unavailable"}
    return {"status": "observed_distinct", "conflicts": []}


def _existing_directory(value: str | Path) -> Path | None:
    """Resolve an existing scope root without manufacturing a candidate path."""

    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_dir() else None


def _scope_entity(root: Path, scope: str) -> tuple[Path | None, str]:
    """Resolve a validated repository-relative scope to its live entity."""

    try:
        normalized = normalize_scope(scope)
        path = root if normalized == "." else root.joinpath(*normalized.split("/"))
        return path.resolve(strict=True), normalized
    except (OSError, RuntimeError, ValueError):
        return None, scope


def _entities_overlap(left: Path, right: Path) -> bool:
    """Return whether two canonical file or directory entities intersect."""

    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _is_writable_entity(path: Path) -> bool:
    """Check whether this process can currently mutate a file or directory."""

    mode = os.W_OK | os.X_OK if path.is_dir() else os.W_OK
    return os.access(path, mode)
