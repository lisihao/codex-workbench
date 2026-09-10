"""Validate a narrow legacy scope-pattern normalization without glob scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from collections.abc import Sequence

from .dirty_worktree_recovery import (
    DirtyWorktreeRecoveryError,
    validate_recovery_source_path,
)
from .recovery_processes import RecoveryProcessError, assert_recovery_source_idle
from .worktrees import normalize_scope, scopes_overlap


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ScopeNormalizationError(ValueError):
    """A legacy scope cannot be proven safe to narrow to one source file."""


@dataclass(frozen=True)
class LegacyBasenamePattern:
    """One supported legacy wildcard limited to the final path basename."""

    value: str
    parent: str
    prefix: str
    suffix: str


@dataclass(frozen=True)
class ScopeNormalizationPlan:
    """A read-only source-bound replacement of one legacy scope string."""

    scope_pattern: str
    exact_path: str
    before_read_scopes: tuple[str, ...]
    before_write_scopes: tuple[str, ...]
    after_read_scopes: tuple[str, ...]
    after_write_scopes: tuple[str, ...]
    file_sha256: str
    matching_paths: tuple[str, ...]


def parse_legacy_basename_pattern(scope_pattern: object) -> LegacyBasenamePattern:
    """Accept exactly one ``*`` in a canonical final basename only."""

    if not isinstance(scope_pattern, str) or not scope_pattern:
        raise ScopeNormalizationError("scope_pattern must be a non-empty string")
    try:
        normalized = normalize_scope(scope_pattern)
    except ValueError as error:
        raise ScopeNormalizationError(f"scope_pattern is invalid: {scope_pattern!r}") from error
    if normalized != scope_pattern:
        raise ScopeNormalizationError("scope_pattern must use canonical repository-relative spelling")
    if any(character in scope_pattern for character in "?[]"):
        raise ScopeNormalizationError("scope_pattern only supports one final-basename asterisk")
    if scope_pattern.count("*") != 1:
        raise ScopeNormalizationError("scope_pattern must contain exactly one asterisk")
    path = PurePosixPath(scope_pattern)
    basename = path.name
    if "*" not in basename or any("*" in part for part in path.parts[:-1]):
        raise ScopeNormalizationError("scope_pattern asterisk must be in its final basename")
    prefix, suffix = basename.split("*", 1)
    parent = str(path.parent)
    return LegacyBasenamePattern(scope_pattern, parent, prefix, suffix)


def normalize_exact_path(exact_path: object, pattern: LegacyBasenamePattern) -> str:
    """Require one canonical literal file path matched by ``pattern``."""

    if not isinstance(exact_path, str) or not exact_path:
        raise ScopeNormalizationError("exact_path must be a non-empty string")
    if any(character in exact_path for character in "*?[]"):
        raise ScopeNormalizationError("exact_path must not contain wildcard syntax")
    try:
        normalized = normalize_scope(exact_path)
    except ValueError as error:
        raise ScopeNormalizationError(f"exact_path is invalid: {exact_path!r}") from error
    if normalized != exact_path or normalized == ".":
        raise ScopeNormalizationError("exact_path must use canonical repository-relative spelling")
    path = PurePosixPath(normalized)
    if str(path.parent) != pattern.parent:
        raise ScopeNormalizationError("exact_path must remain in the legacy pattern parent directory")
    if not _matches_basename(pattern, path.name):
        raise ScopeNormalizationError("exact_path does not match scope_pattern")
    return normalized


def inspect_scope_normalization(
    source: Path,
    *,
    scope_pattern: object,
    exact_path: object,
    read_scopes: Sequence[str],
    write_scopes: Sequence[str],
) -> ScopeNormalizationPlan:
    """Prove one declared legacy pattern resolves to one safe, regular file."""

    pattern = parse_legacy_basename_pattern(scope_pattern)
    literal = normalize_exact_path(exact_path, pattern)
    try:
        source_root = source.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ScopeNormalizationError("scope-normalization source worktree is unavailable") from error
    if not source_root.is_dir():
        raise ScopeNormalizationError("scope-normalization source worktree is not a directory")
    try:
        assert_recovery_source_idle(source_root)
    except RecoveryProcessError as error:
        raise ScopeNormalizationError(
            f"scope-normalization source cannot be shown idle: {error}"
        ) from error
    try:
        validate_recovery_source_path(source_root, literal)
    except DirtyWorktreeRecoveryError as error:
        raise ScopeNormalizationError(str(error)) from error

    parent = source_root if pattern.parent == "." else source_root / pattern.parent
    try:
        parent_metadata = parent.lstat()
    except FileNotFoundError as error:
        raise ScopeNormalizationError("scope_pattern parent directory does not exist") from error
    except OSError as error:
        raise ScopeNormalizationError("scope_pattern parent directory cannot be inspected") from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ScopeNormalizationError("scope_pattern parent directory is unsafe")

    try:
        matching_paths = tuple(sorted(
            _relative_child(pattern.parent, entry.name)
            for entry in parent.iterdir()
            if _matches_basename(pattern, entry.name)
        ))
    except OSError as error:
        raise ScopeNormalizationError("scope_pattern directory cannot be enumerated") from error
    if len(matching_paths) != 1:
        sample = ", ".join(matching_paths[:8])
        raise ScopeNormalizationError(
            "scope_pattern must resolve to exactly one existing path"
            + (f"; matched: {sample}" if sample else "")
        )
    if matching_paths[0] != literal:
        raise ScopeNormalizationError("scope_pattern resolved to a path other than exact_path")

    target = source_root / literal
    digest = _sha256_regular_file(target, literal)
    before_reads = _scope_tuple(read_scopes, "read_scopes")
    before_writes = _scope_tuple(write_scopes, "write_scopes")
    if pattern.value not in before_writes:
        raise ScopeNormalizationError("scope_pattern is not present in the node write_scopes")
    after_reads = _replace_scope(before_reads, pattern.value, literal)
    after_writes = _replace_scope(before_writes, pattern.value, literal)
    return ScopeNormalizationPlan(
        scope_pattern=pattern.value,
        exact_path=literal,
        before_read_scopes=before_reads,
        before_write_scopes=before_writes,
        after_read_scopes=after_reads,
        after_write_scopes=after_writes,
        file_sha256=digest,
        matching_paths=matching_paths,
    )


def validate_matching_file_hash(expected_file_sha256: object, actual_file_sha256: str) -> None:
    """Require the caller to bind an apply request to the previewed source file."""

    if not isinstance(expected_file_sha256, str) or _SHA256_RE.fullmatch(expected_file_sha256) is None:
        raise ScopeNormalizationError("expected_file_sha256 must be a lowercase SHA-256 digest")
    if expected_file_sha256 != actual_file_sha256:
        raise ScopeNormalizationError("expected_file_sha256 does not match the exact source file")


def validate_common_git_directory(repository: str, source: Path) -> None:
    """Bind the source worktree to the task repository's actual Git common dir."""

    repository_common = _git_common_directory(Path(repository).expanduser())
    source_common = _git_common_directory(source)
    if repository_common != source_common:
        raise ScopeNormalizationError(
            "scope-normalization source belongs to a different Git repository"
        )


def scope_conflict_pairs(
    candidate_reads: Sequence[str],
    candidate_writes: Sequence[str],
    running_reads: Sequence[str],
    running_writes: Sequence[str],
) -> tuple[dict[str, str], ...]:
    """Return literal read/write overlap details using scheduler semantics."""

    for label, values in (
        ("candidate", (*candidate_reads, *candidate_writes)),
        ("running", (*running_reads, *running_writes)),
    ):
        if any(_contains_wildcard(value) for value in values):
            raise ScopeNormalizationError(
                f"scope normalization cannot prove concurrency safety with active {label} wildcard scopes"
            )
    pairs: list[dict[str, str]] = []
    for candidate_kind, candidate_scopes, running_kind, running_scopes in (
        ("write", candidate_writes, "write", running_writes),
        ("write", candidate_writes, "read", running_reads),
        ("read", candidate_reads, "write", running_writes),
    ):
        for candidate_scope in candidate_scopes:
            for running_scope in running_scopes:
                try:
                    overlaps = scopes_overlap(candidate_scope, running_scope)
                except ValueError as error:
                    raise ScopeNormalizationError("scope conflict metadata is invalid") from error
                if not overlaps:
                    continue
                left = normalize_scope(candidate_scope)
                right = normalize_scope(running_scope)
                pairs.append({
                    "candidate_access": candidate_kind,
                    "candidate_scope": left,
                    "running_access": running_kind,
                    "running_scope": right,
                    "overlap": (
                        left if right == "." or (left != "." and len(left) >= len(right)) else right
                    ),
                })
    return tuple(pairs)


def _relative_child(parent: str, name: str) -> str:
    return name if parent == "." else f"{parent}/{name}"


def _matches_basename(pattern: LegacyBasenamePattern, name: str) -> bool:
    return (
        len(name) >= len(pattern.prefix) + len(pattern.suffix)
        and name.startswith(pattern.prefix)
        and name.endswith(pattern.suffix)
    )


def _contains_wildcard(value: str) -> bool:
    return value != "*" and any(character in value for character in "*?[]")


def _scope_tuple(values: Sequence[str], field: str) -> tuple[str, ...]:
    try:
        result = tuple(values)
    except TypeError as error:
        raise ScopeNormalizationError(f"{field} must be an explicit string sequence") from error
    if not all(isinstance(value, str) for value in result):
        raise ScopeNormalizationError(f"{field} must contain only strings")
    return result


def _replace_scope(values: tuple[str, ...], old: str, new: str) -> tuple[str, ...]:
    return tuple(new if value == old else value for value in values)


def _sha256_regular_file(path: Path, relative_path: str) -> str:
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise ScopeNormalizationError("exact_path does not exist") from error
    except OSError as error:
        raise ScopeNormalizationError("exact_path cannot be inspected") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ScopeNormalizationError("exact_path must be an existing regular non-symlink file")
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except OSError as error:
        raise ScopeNormalizationError("exact_path cannot be read safely") from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ScopeNormalizationError(
            f"exact_path changed while its digest was recorded: {relative_path}"
        )
    return digest.hexdigest()


def _git_common_directory(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScopeNormalizationError("scope-normalization Git repository cannot be inspected") from error
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ScopeNormalizationError("scope-normalization Git repository cannot be inspected")
    try:
        return str(Path(completed.stdout.strip()).resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as error:
        raise ScopeNormalizationError("scope-normalization Git common directory is unavailable") from error
