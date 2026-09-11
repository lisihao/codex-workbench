from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping

from .artifacts import ArtifactStore
from .dependency_inputs import (
    DependencyInputError,
    apply_recorded_dependency_input,
    changed_paths_since_input_tree,
    load_recorded_dependency_input,
)
from .executors import codex_subscription_environment
from .recovery_processes import RecoveryProcessError, assert_recovery_source_idle
from .worktrees import WorktreeError, WorktreeManager, scope_allows


class DirtyWorktreeRecoveryError(WorktreeError):
    """A blocked dirty worktree cannot be resumed without losing provenance."""


_RECOVERY_ACCEPTANCE_ENVIRONMENT = frozenset(
    {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
    }
)

# These are the only child-environment values that the recovery verifier sets
# or explicitly permits.  The record deliberately omits HOME, credential
# names, and every inherited environment value; runtime executables are bound
# separately by their resolved file identities.
_RECOVERY_EVIDENCE_ENVIRONMENT = (
    "CI",
    "NO_UPDATE_NOTIFIER",
    "npm_config_offline",
    "npm_config_pm_on_fail",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "PYTHONPYCACHEPREFIX",
    "ZDOTDIR",
)
_RECOVERY_EVIDENCE_PATH_VALUES = frozenset(
    {"PATH", "PYTHONPATH", "PYTHONPYCACHEPREFIX", "ZDOTDIR"}
)
_RECOVERY_EVIDENCE_LAUNCHER_SUFFIXES = frozenset(
    {".cjs", ".js", ".jsx", ".mjs", ".py", ".sh", ".ts", ".tsx"}
)
_RECOVERY_EVIDENCE_MAX_LAUNCHER_BYTES = 1_048_576

_RECOVERY_ERROR_PATH_LIMIT = 8
_RECOVERY_ERROR_PATH_CHARS = 80
_RECOVERY_ERROR_SUMMARY_CHARS = 768


@dataclass(frozen=True)
class SourceDeltaEntry:
    """One verified tracked or untracked file in a recoverable source delta."""

    path: str
    kind: str
    deleted: bool
    mode: str | None
    content_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the canonical digest fields for this source file."""

        return {
            "path": self.path,
            "kind": self.kind,
            "deleted": self.deleted,
            "mode": self.mode,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class SourceDelta:
    """A twice-read, content-bound delta from one immutable input tree."""

    comparison_tree: str
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    entries: tuple[SourceDeltaEntry, ...]
    sha256: str


def summarize_recovery_paths(paths: tuple[str, ...]) -> str:
    """Return a count and bounded escaped sample of recovery paths for an error."""

    total = len(paths)
    if not total:
        return "0 paths"
    rendered = []
    for path in paths[:_RECOVERY_ERROR_PATH_LIMIT]:
        item = repr(path)
        if len(item) > _RECOVERY_ERROR_PATH_CHARS:
            item = item[: _RECOVERY_ERROR_PATH_CHARS - 3] + "..."
        rendered.append(item)
    omitted = total - len(rendered)
    suffix = f"; {omitted} additional path(s) omitted" if omitted else ""
    summary = f"{total} path(s): " + ", ".join(rendered) + suffix
    if len(summary) <= _RECOVERY_ERROR_SUMMARY_CHARS:
        return summary
    return summary[: _RECOVERY_ERROR_SUMMARY_CHARS - 3] + "..."


def is_python_bytecode_residue_path(value: object) -> bool:
    """Return whether a Git-relative path is a generated Python cache file."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and ".." not in path.parts
        and "__pycache__" in path.parts[:-1]
        and path.suffix == ".pyc"
    )


def partition_recovery_paths(paths: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separate recoverable source edits from narrowly classified bytecode residue."""

    generated = tuple(sorted(path for path in paths if is_python_bytecode_residue_path(path)))
    generated_set = set(generated)
    recoverable = tuple(sorted(path for path in paths if path not in generated_set))
    return recoverable, generated


def validate_recovery_source_path(worktree: Path, relative_path: str) -> None:
    """Reject a source path whose existing leaf or parent is a symlink."""

    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or "\x00" in relative_path
        or relative.is_absolute()
        or str(relative) != relative_path
        or ".." in relative.parts
    ):
        raise DirtyWorktreeRecoveryError(
            f"recovery source path is invalid: {relative_path!r}"
        )
    root = worktree.resolve(strict=True)
    candidate = root
    for index, component in enumerate(relative.parts):
        candidate = candidate / component
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                f"recovery source path cannot be inspected: {relative_path}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise DirtyWorktreeRecoveryError(
                f"recovery source path or parent must not be a symlink: {relative_path}"
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise DirtyWorktreeRecoveryError(
                f"recovery source path has a non-directory parent: {relative_path}"
            )
        if index == len(relative.parts) - 1 and not stat.S_ISREG(metadata.st_mode):
            raise DirtyWorktreeRecoveryError(
                f"recovery source path must be a regular file when present: {relative_path}"
            )
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as error:
        raise DirtyWorktreeRecoveryError(
            f"recovery source path escapes its worktree: {relative_path}"
        ) from error


def _source_delta_json(
    comparison_tree: str,
    entries: tuple[SourceDeltaEntry, ...],
) -> bytes:
    """Serialize only the input tree and verified source-delta entries for hashing."""

    return json.dumps(
        {
            "comparison_tree": comparison_tree,
            "entries": [entry.to_dict() for entry in entries],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _read_source_delta_file(worktree: Path, relative_path: str) -> tuple[str, str]:
    """Read one regular source file twice enough to reject a concurrent rewrite."""

    validate_recovery_source_path(worktree, relative_path)
    candidate = worktree / relative_path
    try:
        before = candidate.lstat()
        payload = candidate.read_bytes()
        after = candidate.lstat()
    except FileNotFoundError as error:
        raise DirtyWorktreeRecoveryError(
            f"source delta path disappeared while it was inspected: {relative_path}"
        ) from error
    except OSError as error:
        raise DirtyWorktreeRecoveryError(
            f"source delta path cannot be read: {relative_path}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DirtyWorktreeRecoveryError(
            f"source delta path must be a regular non-symlink file: {relative_path}"
        )
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        stat.S_IMODE(before.st_mode),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        stat.S_IMODE(after.st_mode),
    )
    if (
        before_identity != after_identity
        or stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
    ):
        raise DirtyWorktreeRecoveryError(
            f"source delta path changed while it was inspected: {relative_path}"
        )
    return f"{stat.S_IMODE(before.st_mode):04o}", sha256(payload).hexdigest()


def _recovery_source_delta_paths(
    worktree: Path,
    comparison_tree: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """List the tracked/untracked delta without reading any source-file content."""

    changed_paths = tuple(
        sorted(changed_paths_since_input_tree(worktree, comparison_tree))
    )
    untracked_paths = DirtyWorktreeRecovery.untracked_paths(worktree)
    untracked = set(untracked_paths)
    if not untracked.issubset(changed_paths):
        raise DirtyWorktreeRecoveryError(
            "source delta untracked paths are missing from its change set"
        )
    return changed_paths, untracked_paths


def _inspect_recovery_source_delta_once(
    worktree: Path,
    comparison_tree: str,
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
) -> SourceDelta:
    """Hash one previously validated complete tracked/untracked delta snapshot."""

    untracked = set(untracked_paths)
    entries: list[SourceDeltaEntry] = []
    for relative_path in changed_paths:
        validate_recovery_source_path(worktree, relative_path)
        candidate = worktree / relative_path
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if relative_path in untracked:
                raise DirtyWorktreeRecoveryError(
                    "source delta untracked path disappeared while it was inspected: "
                    + relative_path
                )
            entries.append(
                SourceDeltaEntry(
                    path=relative_path,
                    kind="tracked",
                    deleted=True,
                    mode=None,
                    content_sha256=None,
                )
            )
            continue
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                f"source delta path cannot be inspected: {relative_path}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DirtyWorktreeRecoveryError(
                f"source delta path must be a regular non-symlink file: {relative_path}"
            )
        mode, content_sha256 = _read_source_delta_file(worktree, relative_path)
        entries.append(
            SourceDeltaEntry(
                path=relative_path,
                kind="untracked" if relative_path in untracked else "tracked",
                deleted=False,
                mode=mode,
                content_sha256=content_sha256,
            )
        )
    canonical_entries = tuple(entries)
    return SourceDelta(
        comparison_tree=comparison_tree,
        changed_paths=changed_paths,
        untracked_paths=untracked_paths,
        entries=canonical_entries,
        sha256=sha256(_source_delta_json(comparison_tree, canonical_entries)).hexdigest(),
    )


def inspect_recovery_source_delta(
    worktree: Path,
    comparison_tree: str,
    *,
    validate_paths: Callable[[tuple[str, ...]], None] | None = None,
) -> SourceDelta:
    """Return a stable digest for the current recoverable delta, excluding ignored files.

    The caller supplies the recorded input tree rather than an artifact
    reference so the digest remains identical for root and dependent workers
    that share the same materialized input. The entire path/content snapshot
    is read twice; a path, deletion, executable-mode, or byte change between
    reads is rejected instead of producing a race-prone digest.
    """

    root = worktree.resolve(strict=True)
    resolved_tree = DirtyWorktreeRecovery._git_text(
        root,
        "rev-parse",
        "--verify",
        f"{comparison_tree}^{{tree}}",
    )
    first_paths, first_untracked = _recovery_source_delta_paths(root, resolved_tree)
    if validate_paths is not None:
        validate_paths(first_paths)
    first = _inspect_recovery_source_delta_once(
        root,
        resolved_tree,
        first_paths,
        first_untracked,
    )
    second_paths, second_untracked = _recovery_source_delta_paths(root, resolved_tree)
    if validate_paths is not None:
        validate_paths(second_paths)
    second = _inspect_recovery_source_delta_once(
        root,
        resolved_tree,
        second_paths,
        second_untracked,
    )
    if first != second:
        raise DirtyWorktreeRecoveryError(
            "source delta changed while it was inspected; rerun source-only recovery dry-run"
        )
    return second


def _validate_recovery_common_git_directory(repository: str, worktree: Path) -> None:
    """Require the source allocation and task repository to share Git metadata."""

    repository_path = Path(repository).expanduser().resolve(strict=True)
    repository_common = DirtyWorktreeRecovery._git_text(
        repository_path,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    worktree_common = DirtyWorktreeRecovery._git_text(
        worktree,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if repository_common != worktree_common:
        raise DirtyWorktreeRecoveryError(
            "source-only recovery source belongs to a different Git repository"
        )


def inspect_indeterminate_source_delta(
    candidate: Mapping[str, Any],
    *,
    dependency_input_ref: str | None,
    artifacts: ArtifactStore,
    expected_checkpoint_sha: str | None = None,
    recovery_label: str = "indeterminate",
) -> SourceDelta:
    """Validate one normalized source-only allocation and return its live delta.

    The legacy entry point is shared by indeterminate and blocked recovery.
    This read-only preflight checks process quiescence, the physical
    repository/base/branch/common-Git binding, dependency input, task and node
    scopes, and every source path before the store authorizes extraction.
    """

    if recovery_label not in {"indeterminate", "blocked"}:
        raise ValueError("source-only recovery label is invalid")
    task = candidate.get("task")
    node = candidate.get("node")
    if not isinstance(task, Mapping) or not isinstance(node, Mapping):
        raise DirtyWorktreeRecoveryError(f"{recovery_label} source recovery candidate is invalid")
    repository = task.get("repository")
    base_sha = task.get("base_sha")
    worktree = node.get("worktree")
    branch = node.get("branch")
    depends_on = node.get("depends_on")
    if not all(
        isinstance(value, str) and value
        for value in (repository, base_sha, worktree, branch)
    ):
        raise DirtyWorktreeRecoveryError(
            f"{recovery_label} source recovery candidate has invalid repository binding"
        )
    if not isinstance(depends_on, tuple) or not all(
        isinstance(dependency, str) and dependency for dependency in depends_on
    ):
        raise DirtyWorktreeRecoveryError(
            f"{recovery_label} source recovery candidate has invalid dependency metadata"
        )
    recovery = DirtyWorktreeRecovery(
        artifacts,
        WorktreeManager(artifacts.root.parent / "worktrees"),
    )
    source = recovery.validate_retry_source(
        repository=repository,
        base_sha=base_sha,
        worktree=worktree,
        branch=branch,
        expected_checkpoint_sha=expected_checkpoint_sha,
    )
    _validate_recovery_common_git_directory(repository, source)
    try:
        assert_recovery_source_idle(source)
    except RecoveryProcessError as error:
        raise DirtyWorktreeRecoveryError(
            f"source-only recovery cannot prove the source worktree is idle: {error}"
        ) from error
    if depends_on and dependency_input_ref is None:
        raise DirtyWorktreeRecoveryError(
            f"{recovery_label} node recovery requires the recorded dependency-input artifact"
        )
    if dependency_input_ref is not None:
        dependency_input = load_recorded_dependency_input(
            artifacts,
            dependency_input_ref,
            task_id=str(task.get("task_id", "")),
            node_id=str(node.get("node_id", "")),
            base_sha=base_sha,
        )
        comparison_tree = dependency_input.input_tree_sha
    else:
        comparison_tree = base_sha
    allowed_scope = task.get("allowed_scope")
    forbidden_scope = task.get("forbidden_scope")
    write_scopes = node.get("write_scopes")
    if not (
        isinstance(allowed_scope, tuple)
        and all(isinstance(scope, str) for scope in allowed_scope)
        and isinstance(forbidden_scope, tuple)
        and all(isinstance(scope, str) for scope in forbidden_scope)
        and isinstance(write_scopes, tuple)
        and all(isinstance(scope, str) for scope in write_scopes)
    ):
        raise DirtyWorktreeRecoveryError(f"{recovery_label} source recovery scopes are invalid")

    def validate_paths(paths: tuple[str, ...]) -> None:
        for relative_path in paths:
            if not scope_allows(
                relative_path,
                list(allowed_scope),
                list(forbidden_scope),
            ):
                raise DirtyWorktreeRecoveryError(
                    f"{recovery_label} node recovery path is outside task scope: "
                    + relative_path
                )
            if not scope_allows(relative_path, list(write_scopes), []):
                raise DirtyWorktreeRecoveryError(
                    f"{recovery_label} node recovery path is outside node write scope: "
                    + relative_path
                )

    return inspect_recovery_source_delta(
        source,
        comparison_tree,
        validate_paths=validate_paths,
    )


def observed_indeterminate_recovery_paths(
    candidate: Mapping[str, Any],
    *,
    dependency_input_ref: str | None,
    artifacts: ArtifactStore,
    source_only: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Inspect one owned indeterminate worktree and prove every change is in scope.

    ``candidate`` is the shape returned by
    ``WorkbenchStore.indeterminate_local_recovery_candidate``. Returns the
    recoverable changed paths and the generated-residue paths. Any foreign,
    out-of-scope, unsafe-ignored, or symlinked untracked path raises before
    the caller can assert the explicit confirmation flags.
    """

    task = candidate["task"]
    node = candidate["node"]
    worktree = Path(str(node["worktree"])).expanduser().resolve(strict=True)
    if source_only:
        try:
            assert_recovery_source_idle(worktree)
        except RecoveryProcessError as error:
            raise DirtyWorktreeRecoveryError(
                f"source-only recovery cannot prove the source worktree is idle: {error}"
            ) from error
    base_sha = str(task["base_sha"])
    depends_on = node.get("depends_on")
    if not isinstance(depends_on, tuple) or not all(
        isinstance(dependency, str) and dependency for dependency in depends_on
    ):
        raise DirtyWorktreeRecoveryError(
            "indeterminate node recovery has invalid dependency metadata"
        )
    if depends_on and dependency_input_ref is None:
        raise DirtyWorktreeRecoveryError(
            "indeterminate node recovery requires the recorded dependency-input artifact"
        )
    if dependency_input_ref is not None:
        dependency_input = load_recorded_dependency_input(
            artifacts,
            dependency_input_ref,
            task_id=str(task["task_id"]),
            node_id=str(node["node_id"]),
            base_sha=base_sha,
        )
        comparison_tree = dependency_input.input_tree_sha
    else:
        comparison_tree = base_sha

    ignored = DirtyWorktreeRecovery.ignored_paths(worktree)
    recoverable_ignored, generated_residue_paths = partition_recovery_paths(ignored)
    if recoverable_ignored and not source_only:
        raise DirtyWorktreeRecoveryError(
            "indeterminate node worktree contains ignored paths that cannot be recovered safely: "
            + summarize_recovery_paths(recoverable_ignored)
        )
    if source_only:
        generated_residue_paths = ()
    changed_paths = tuple(sorted(changed_paths_since_input_tree(worktree, comparison_tree)))
    allowed_scope = list(task["allowed_scope"])
    forbidden_scope = list(task["forbidden_scope"])
    write_scopes = list(node["write_scopes"])
    for relative_path in changed_paths:
        if not scope_allows(relative_path, allowed_scope, forbidden_scope):
            raise DirtyWorktreeRecoveryError(
                f"indeterminate node recovery path is outside task scope: {relative_path}"
            )
        if not scope_allows(relative_path, write_scopes, []):
            raise DirtyWorktreeRecoveryError(
                f"indeterminate node recovery path is outside node write scope: {relative_path}"
            )
        validate_recovery_source_path(worktree, relative_path)
    return changed_paths, generated_residue_paths


@dataclass(frozen=True)
class CommandOutcome:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    evidence_inputs: Mapping[str, object] | None = None
    evidence_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        # Existing materialization and legacy recovery receipts do not invent
        # a fingerprint.  Consumers that know only the original four fields
        # can therefore continue reading them unchanged.
        if self.evidence_inputs is not None:
            result["evidence_inputs"] = dict(self.evidence_inputs)
        if self.evidence_fingerprint is not None:
            result["evidence_fingerprint"] = self.evidence_fingerprint
        return result


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    summary: str
    artifacts: dict[str, str]
    checks: tuple[str, ...]
    changed_paths: tuple[str, ...]
    exit_code: int | None = None
    prepared_recovery: dict[str, object] | None = None


def _bounded(text: str, *, limit: int = 1_000_000) -> str:
    if len(text.encode("utf-8", errors="replace")) <= limit:
        return text
    encoded = text.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="ignore") + "\n[output truncated by Workbench recovery]\n"


def _canonical_evidence_json(value: object) -> bytes:
    """Encode a JSON-safe evidence value with a stable digest representation."""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_evidence_copy(value: object) -> dict[str, object] | None:
    """Copy one bounded evidence object only when it is safe to serialize."""

    try:
        encoded = _canonical_evidence_json(value)
        decoded = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _evidence_path_reference(path: Path, cwd: Path) -> str:
    """Return a worktree-relative path or a non-reversible external identity."""

    try:
        return "worktree:" + str(path.relative_to(cwd))
    except ValueError:
        return "sha256:" + sha256(os.fsencode(str(path))).hexdigest()


def _small_launcher_digest(path: Path, metadata: os.stat_result) -> tuple[str | None, bool]:
    """Digest a small script launcher, never a large runtime binary."""

    if metadata.st_size > _RECOVERY_EVIDENCE_MAX_LAUNCHER_BYTES:
        return None, False
    should_hash = path.suffix.lower() in _RECOVERY_EVIDENCE_LAUNCHER_SUFFIXES
    if not should_hash:
        try:
            with path.open("rb") as handle:
                should_hash = handle.read(2) == b"#!"
        except OSError:
            return None, True
    if not should_hash:
        return None, False
    try:
        return sha256(path.read_bytes()).hexdigest(), False
    except OSError:
        return None, True


def _runtime_file_identity(path: Path | None, cwd: Path) -> dict[str, object]:
    """Bind one resolved launcher to inexpensive file metadata and script content."""

    if path is None:
        return {"status": "unresolved"}
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return {"status": "unavailable"}
    if not stat.S_ISREG(metadata.st_mode):
        return {
            "status": "not-regular",
            "resolved_path": _evidence_path_reference(resolved, cwd),
        }
    result: dict[str, object] = {
        "status": "resolved",
        "resolved_path": _evidence_path_reference(resolved, cwd),
        "stat": {
            "dev": int(metadata.st_dev),
            "ino": int(metadata.st_ino),
            "size": int(metadata.st_size),
            "mtime_ns": int(metadata.st_mtime_ns),
        },
    }
    digest, hash_unavailable = _small_launcher_digest(resolved, metadata)
    if digest is not None:
        result["launcher_sha256"] = digest
    if hash_unavailable:
        result["launcher_hash_status"] = "unavailable"
    return result


def _resolve_child_executable(
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> Path | None:
    """Resolve an argv entry exactly as the direct child environment can find it."""

    if not executable:
        return None
    if os.sep in executable or (os.altsep is not None and os.altsep in executable):
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
        except OSError:
            return None
    # ``shutil.which`` resolves relative PATH entries against this authority
    # process, not the child ``cwd``.  Recovery evidence must describe the
    # latter, because that is what execvp will use for an argv-only command.
    search_path = environment.get("PATH", os.defpath)
    for raw_entry in search_path.split(os.pathsep):
        directory = cwd if not raw_entry else Path(raw_entry)
        if not directory.is_absolute():
            directory = cwd / directory
        candidate = directory / executable
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _same_resolved_path(left: Path | None, right: Path) -> bool:
    """Compare a PATH result with the private per-command pnpm shim safely."""

    if left is None:
        return False
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except OSError:
        return False


def _effective_command_entry(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    shim_directory: Path,
) -> Path | None:
    """Resolve the executable ultimately selected by the controlled pnpm shim."""

    entry = _resolve_child_executable(command[0], cwd, environment)
    shim = shim_directory / "pnpm"
    if command[0] != "pnpm" or not _same_resolved_path(entry, shim):
        return entry
    if len(command) >= 3 and command[1] == "exec":
        local_name = command[2]
        if re.fullmatch(r"[A-Za-z0-9._-]+", local_name):
            local_entry = cwd / "node_modules" / ".bin" / local_name
            if local_entry.is_file() and os.access(local_entry, os.X_OK):
                return local_entry
    configured_pnpm = environment.get("CODEX_WORKBENCH_PNPM")
    if configured_pnpm:
        return _resolve_child_executable(configured_pnpm, cwd, environment)
    # The private shim itself is not a stable input: it is created afresh for
    # every command.  Treat the missing underlying launcher as incomplete
    # coverage rather than binding an ephemeral inode into the fingerprint.
    return None


def _direct_node_script_entry(
    command: tuple[str, ...], cwd: Path
) -> tuple[Path | None, bool]:
    """Return the direct Node script argument when its location is unambiguous."""

    if Path(command[0]).name != "node" or len(command) < 2 or command[1].startswith("-"):
        return None, False
    candidate = Path(command[1])
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return (candidate if candidate.is_file() else None), True
    except OSError:
        return None, True


def _recovery_evidence_coverage(incomplete: list[str]) -> dict[str, object]:
    """Describe the bounded inputs for which a recovery fingerprint is evidence."""

    return {
        "complete": not incomplete,
        "incomplete_reasons": sorted(set(incomplete)),
        "scope": {
            "kind": "recorded-recovery-command-inputs-v1",
            "environment": "fixed-child-fields-only",
            "dependency_closure": "not-recorded",
            "direct_node_entry": "recorded-when-unambiguous",
        },
        "reuse_authorized": False,
    }


def _environment_evidence(
    environment: Mapping[str, str], shim_directory: Path
) -> dict[str, object]:
    """Record only verification-relevant child environment inputs without secrets."""

    fixed: dict[str, object] = {}
    for name in _RECOVERY_EVIDENCE_ENVIRONMENT:
        value = environment.get(name)
        if value is None:
            fixed[name] = None
        elif name == "ZDOTDIR" and _same_resolved_path(Path(value), shim_directory):
            # The helper creates this directory per invocation.  Its role and
            # the resolved pnpm runtime are inputs, but its random directory
            # name is not.
            fixed[name] = "private-recovery-pnpm-shim"
        elif name in _RECOVERY_EVIDENCE_PATH_VALUES:
            fixed[name] = {
                "value_sha256": sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest(),
                "bytes": len(value.encode("utf-8", errors="surrogateescape")),
            }
        else:
            fixed[name] = value
    path_value = environment.get("PATH", "")
    path_entries = tuple(entry for entry in path_value.split(os.pathsep) if entry)
    path_identities = tuple(
        "private-recovery-pnpm-shim"
        if _same_resolved_path(Path(entry), shim_directory)
        else "sha256:" + sha256(os.fsencode(entry)).hexdigest()
        for entry in path_entries
    )
    return {
        "fixed": fixed,
        "PATH": {
            "entries": len(path_entries),
            "identity_sha256": sha256(
                _canonical_evidence_json(path_identities)
            ).hexdigest(),
        },
    }


class PnpmOfflineMaterializer:
    """Materialize a worktree-local pnpm linker without network access.

    Each recovered worktree retains an independent pnpm linker tree, including
    package-local ``node_modules`` directories. A verified template is cloned
    into the target rather than shared or symlinked, so workspace links remain
    local to the target source tree.
    A complete linker carrying the matching Workbench marker is reused in
    place, avoiding a second offline install for the same worktree inputs.
    On APFS this is copy-on-write and avoids repeated pnpm linker work.
    """

    # pnpm 11 verifies a loaded lockfile against registry publish metadata by
    # default, even when the install itself is offline.  Recovery only accepts
    # a fixed, frozen lockfile from the already-authorized Git base, so it must
    # explicitly trust that lockfile rather than retry registry attestations.
    # The bounded recovery lease still prevents any upstream behavior from
    # becoming an unbounded worker lease.
    MINIMUM_PNPM_11_VERSION = (11, 25, 0)
    MAX_MATERIALIZATION_SECONDS = 120
    MAX_TEMPLATE_SEED_SECONDS = 360
    BINARY_ENVIRONMENT_VARIABLE = "CODEX_WORKBENCH_PNPM"
    STORE_ENVIRONMENT_VARIABLE = "CODEX_WORKBENCH_PNPM_STORE"
    LOCK_FILENAME = ".codex-workbench-pnpm-materialization.lock"
    TEMPLATE_DIRECTORY_NAME = ".codex-workbench-pnpm-linker-templates"
    TEMPLATE_METADATA_FILENAME = "materialization.json"
    TEMPLATE_MARKER_FILENAME = ".codex-workbench-template-key"

    def __init__(
        self,
        *,
        binary: str | None = None,
        store_dir: Path | None = None,
        template_dir: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.binary = binary or os.environ.get(self.BINARY_ENVIRONMENT_VARIABLE, "pnpm")
        configured_store = os.environ.get(self.STORE_ENVIRONMENT_VARIABLE)
        self.store_dir = store_dir or (Path(configured_store).expanduser() if configured_store else None)
        self.template_dir = template_dir
        self.runner = runner

    def materialize(
        self,
        worktree: Path,
        *,
        timeout_seconds: int,
        require_cached_template: bool = False,
    ) -> dict[str, object]:
        manifest_path = worktree / "package.json"
        lockfile = worktree / "pnpm-lock.yaml"
        if not manifest_path.is_file() and not lockfile.is_file():
            return {
                "schema_version": 1,
                "kind": "not-applicable",
                "reason": "worktree has no Node package manifest or pnpm lockfile",
            }
        if not manifest_path.is_file() or not lockfile.is_file():
            raise DirtyWorktreeRecoveryError(
                "pnpm recovery requires both package.json and pnpm-lock.yaml"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DirtyWorktreeRecoveryError(f"cannot read package manifest: {error}") from error
        declared = manifest.get("packageManager") if isinstance(manifest, dict) else None
        if not isinstance(declared, str) or not declared.startswith("pnpm@"):
            raise DirtyWorktreeRecoveryError(
                "pnpm recovery requires package.json packageManager=pnpm@<version>"
            )
        binary = shutil.which(self.binary) if "/" not in self.binary else self.binary
        if not binary:
            raise DirtyWorktreeRecoveryError("pnpm is unavailable on the Workbench authority")
        cache_root = self._template_root()
        maximum_seconds = (
            self.MAX_TEMPLATE_SEED_SECONDS
            if cache_root is not None
            else self.MAX_MATERIALIZATION_SECONDS
        )
        effective_timeout = min(timeout_seconds, maximum_seconds)
        if effective_timeout <= 0:
            raise DirtyWorktreeRecoveryError("pnpm recovery timeout must be positive")
        environment = os.environ.copy()
        environment.update({
            "CI": "true",
            "NO_UPDATE_NOTIFIER": "1",
            "npm_config_offline": "true",
            # pnpm 11 otherwise verifies release-age attestations against the
            # registry even when installation itself is declared offline.
            # The frozen base lockfile is the trusted dependency boundary for
            # this recovery, not the live registry.
            "npm_config_minimum_release_age": "0",
            "npm_config_trust_lockfile": "true",
        })
        deadline = time.monotonic() + effective_timeout
        # pnpm consults workspace configuration even for --version. Probe the
        # Workbench-managed binary in a neutral directory so a broken target
        # workspace cannot consume the whole recovery lease before install.
        version = self._run(
            (binary, "--version"),
            Path(tempfile.gettempdir()),
            environment,
            self._remaining_seconds(deadline),
        )
        if version.exit_code != 0:
            raise DirtyWorktreeRecoveryError(
                f"pnpm version probe failed: {version.stderr.strip() or version.stdout.strip()}"
            )
        actual_version = version.stdout.strip()
        declared_version = declared.split("@", 1)[1].split("+", 1)[0]
        actual_semver = self._semver(actual_version, label="authority pnpm")
        declared_semver = self._semver(declared_version, label="declared pnpm")
        if actual_semver[0] != declared_semver[0]:
            raise DirtyWorktreeRecoveryError(
                f"pnpm major mismatch: package declares {declared_version}, authority provides {actual_version}"
            )
        if actual_semver[0] == 11 and actual_semver < self.MINIMUM_PNPM_11_VERSION:
            minimum = ".".join(str(part) for part in self.MINIMUM_PNPM_11_VERSION)
            raise DirtyWorktreeRecoveryError(
                f"pnpm {actual_version} is unsupported for offline recovery; pnpm 11 must be at least "
                f"{minimum}. Configure {self.BINARY_ENVIRONMENT_VARIABLE} to the Workbench-managed runtime."
            )
        template_signature = self._template_signature(
            worktree, declared, actual_version
        )
        template_directory = (
            cache_root / template_signature["key"]
            if cache_root is not None
            else None
        )

        install_command: tuple[str, ...] = (
            binary,
            "install",
            "--offline",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--pm-on-fail=ignore",
            "--config.minimumReleaseAge=0",
            "--config.trustLockfile=true",
            "--reporter=append-only",
        )
        if self.store_dir is not None:
            store_dir = self.store_dir.resolve(strict=False)
            if not store_dir.is_dir():
                raise DirtyWorktreeRecoveryError(
                    f"configured pnpm store is unavailable: {store_dir}"
                )
            install_command += ("--store-dir", str(store_dir))
        # Use the fixed Workbench pnpm runtime even when a trusted project
        # manifest declares an older compatible pnpm.  Recovery is already
        # frozen/offline and the fixed runtime was qualified before dispatch;
        # allowing pnpm to download a manifest-pinned CLI would reintroduce an
        # external registry dependency into this local linker step.
        # pnpm writes shared store metadata while materializing a worktree-local
        # linker. The same lock also protects cache publication so no recovery
        # can observe a partially copied template.
        template_publish: CommandOutcome | None = None
        with self._shared_store_lock(deadline) as (lock_path, lock_wait_seconds):
            if not require_cached_template and self._has_template_marker(
                worktree / "node_modules", template_signature["key"]
            ):
                return {
                    "schema_version": 1,
                    "kind": "pnpm-offline-materialization",
                    "package_manager": declared,
                    "pnpm_version": actual_version,
                    "lockfile_sha256": sha256(lockfile.read_bytes()).hexdigest(),
                    "materialization_timeout_seconds": effective_timeout,
                    "store_dir": str(self.store_dir.resolve(strict=False)) if self.store_dir else None,
                    "shared_store_lock": {
                        "path": str(lock_path),
                        "wait_seconds": lock_wait_seconds,
                    },
                    "template": {
                        "state": "reuse",
                        "key": template_signature["key"],
                        "path": str(worktree / "node_modules"),
                    },
                    "commands": [version.to_dict(), {
                        "command": ["pnpm-worktree", "reuse", str(worktree)],
                        "exit_code": 0,
                        "stdout": "reused complete worktree-local pnpm linker tree\n",
                        "stderr": "",
                    }],
                }
            if template_directory is not None:
                cached_clone = self._clone_cached_template(
                    template_directory, template_signature, worktree, deadline
                )
                if cached_clone is not None:
                    clone, replaced_interrupted_node_modules = cached_clone
                    return {
                        "schema_version": 1,
                        "kind": "pnpm-offline-materialization",
                        "package_manager": declared,
                        "pnpm_version": actual_version,
                        "lockfile_sha256": sha256(lockfile.read_bytes()).hexdigest(),
                        "materialization_timeout_seconds": effective_timeout,
                        "store_dir": str(self.store_dir.resolve(strict=False)) if self.store_dir else None,
                        "shared_store_lock": {
                            "path": str(lock_path),
                            "wait_seconds": lock_wait_seconds,
                        },
                        "template": {
                            "state": "hit",
                            "key": template_signature["key"],
                            "path": str(template_directory),
                            "replaced_interrupted_node_modules": replaced_interrupted_node_modules,
                        },
                        "commands": [version.to_dict(), clone.to_dict()],
                    }
            if require_cached_template:
                raise DirtyWorktreeRecoveryError(
                    "source-only recovery requires a cached verified pnpm linker template"
                )
            self._discard_incomplete_node_modules(worktree)
            install = self._run(
                install_command,
                worktree,
                environment,
                self._remaining_seconds(deadline),
            )
            if install.exit_code != 0:
                raise DirtyWorktreeRecoveryError(
                    "offline pnpm materialization failed: "
                    f"{install.stderr.strip() or install.stdout.strip()}"
                )
            if template_directory is not None:
                self._write_template_marker(worktree / "node_modules", template_signature["key"])
                template_publish = self._publish_template(
                    template_directory, template_signature, worktree, deadline
                )
            elif self._node_modules_is_complete(worktree / "node_modules"):
                self._write_template_marker(worktree / "node_modules", template_signature["key"])
        return {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "package_manager": declared,
            "pnpm_version": actual_version,
            "lockfile_sha256": sha256(lockfile.read_bytes()).hexdigest(),
            "materialization_timeout_seconds": effective_timeout,
            "store_dir": str(self.store_dir.resolve(strict=False)) if self.store_dir else None,
            "shared_store_lock": {
                "path": str(lock_path),
                "wait_seconds": lock_wait_seconds,
            },
            "template": (
                {
                    "state": "seeded",
                    "key": template_signature["key"],
                    "path": str(template_directory),
                    "clone": template_publish.to_dict(),
                }
                if template_publish is not None
                and template_directory is not None
                else {"state": "disabled" if cache_root is None else "not-created"}
            ),
            "commands": [version.to_dict(), install.to_dict()],
        }

    def _template_root(self) -> Path | None:
        if self.template_dir is not None:
            return self.template_dir.expanduser().resolve(strict=False)
        if self.store_dir is None:
            return None
        return self.store_dir.resolve(strict=False).parent / self.TEMPLATE_DIRECTORY_NAME

    def _template_signature(
        self, worktree: Path, package_manager: str, pnpm_version: str
    ) -> dict[str, str]:
        input_paths = {Path("pnpm-lock.yaml"), Path("pnpm-workspace.yaml"), Path(".npmrc")}
        for manifest in worktree.rglob("package.json"):
            relative = manifest.relative_to(worktree)
            if "node_modules" not in relative.parts and ".git" not in relative.parts:
                input_paths.add(relative)
        digest = sha256()
        for relative in sorted(input_paths, key=str):
            path = worktree / relative
            digest.update(str(relative).encode("utf-8"))
            digest.update(b"\0")
            if path.is_file():
                try:
                    digest.update(path.read_bytes())
                except OSError as error:
                    raise DirtyWorktreeRecoveryError(
                        f"cannot read pnpm template input {path}: {error}"
                    ) from error
            else:
                digest.update(b"<missing>")
            digest.update(b"\0")
        payload = {
            # v3 captures package-local pnpm linkers as well as the root
            # node_modules tree. Reusing a v2 root-only cache would leave
            # isolated-workspace package imports unresolved.
            "schema_version": "3",
            "package_manager": package_manager,
            "pnpm_version": pnpm_version,
            "platform": platform.system().lower(),
            "machine": platform.machine(),
            "workspace_input_sha256": digest.hexdigest(),
        }
        key = sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {**payload, "key": key}

    def _clone_cached_template(
        self,
        template_directory: Path,
        signature: Mapping[str, str],
        worktree: Path,
        deadline: float,
    ) -> tuple[CommandOutcome, bool] | None:
        if not template_directory.exists():
            return None
        metadata_path = template_directory / self.TEMPLATE_METADATA_FILENAME
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template metadata is unreadable: {metadata_path}: {error}"
            ) from error
        if not isinstance(metadata, dict) or metadata.get("signature") != dict(signature):
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template signature does not match recovery inputs: {template_directory}"
            )
        linker_paths = self._template_linker_paths(metadata, template_directory)
        source = template_directory / "node_modules"
        if not source.is_dir():
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template is missing node_modules: {template_directory}"
            )
        if not self._has_template_marker(source, signature["key"]):
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template is incomplete or stale: {template_directory}"
            )
        if self._linker_tree_is_complete(worktree, linker_paths, signature["key"]):
            return (
                CommandOutcome(
                    ("pnpm-template", "reuse", str(worktree)),
                    0,
                    "reused complete worktree-local pnpm linker tree\n",
                    "",
                ),
                False,
            )
        replaced_interrupted_node_modules = self._remove_linker_tree(worktree, linker_paths)
        return (
            self._clone_linker_tree(template_directory, worktree, linker_paths, deadline),
            replaced_interrupted_node_modules,
        )

    def _publish_template(
        self,
        template_directory: Path,
        signature: Mapping[str, str],
        source: Path,
        deadline: float,
    ) -> CommandOutcome | None:
        if not (source / "node_modules").is_dir() or template_directory.exists():
            return None
        template_directory.parent.mkdir(parents=True, exist_ok=True)
        staging = template_directory.parent / (
            f".{template_directory.name}.staging-{os.getpid()}-{time.monotonic_ns()}"
        )
        try:
            staging.mkdir()
            linker_paths = self._linker_paths(source)
            outcome = self._clone_linker_tree(source, staging, linker_paths, deadline)
            metadata = {
                "schema_version": 2,
                "signature": dict(signature),
                "linker_paths": [str(path) for path in linker_paths],
            }
            (staging / self.TEMPLATE_METADATA_FILENAME).write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            staging.replace(template_directory)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return outcome

    @staticmethod
    def _linker_paths(worktree: Path) -> tuple[Path, ...]:
        """Return pnpm linker directories without walking root dependencies."""

        root = Path("node_modules")
        paths = {root}
        for current, directories, _files in os.walk(worktree):
            current_path = Path(current)
            relative = current_path.relative_to(worktree)
            directories[:] = [name for name in directories if name != ".git"]
            if relative == Path("."):
                directories[:] = [name for name in directories if name != "node_modules"]
                continue
            if "node_modules" not in directories:
                continue
            candidate = relative / "node_modules"
            directories.remove("node_modules")
            paths.add(candidate)
        return tuple(sorted(paths, key=str))

    @staticmethod
    def _template_linker_paths(
        metadata: Mapping[str, object], template_directory: Path
    ) -> tuple[Path, ...]:
        raw_paths = metadata.get("linker_paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template is missing linker paths: {template_directory}"
            )
        paths: list[Path] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                raise DirtyWorktreeRecoveryError(
                    f"pnpm dependency template has an invalid linker path: {template_directory}"
                )
            relative = Path(raw_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[-1] != "node_modules"
            ):
                raise DirtyWorktreeRecoveryError(
                    f"pnpm dependency template has an unsafe linker path: {template_directory}"
                )
            paths.append(relative)
        normalized = tuple(sorted(set(paths), key=str))
        if Path("node_modules") not in normalized:
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency template is missing root node_modules: {template_directory}"
            )
        for relative in normalized:
            linker = template_directory / relative
            if not linker.is_dir() and not linker.is_symlink():
                raise DirtyWorktreeRecoveryError(
                    f"pnpm dependency template is missing linker directory {relative}: {template_directory}"
                )
        return normalized

    def _linker_tree_is_complete(
        self, worktree: Path, linker_paths: tuple[Path, ...], key: str
    ) -> bool:
        if not self._has_template_marker(worktree / "node_modules", key):
            return False
        return all(
            (worktree / relative).is_dir() or (worktree / relative).is_symlink()
            for relative in linker_paths
        )

    def _remove_linker_tree(self, worktree: Path, linker_paths: tuple[Path, ...]) -> bool:
        removed = False
        for relative in sorted(linker_paths, key=lambda path: len(path.parts), reverse=True):
            target = worktree / relative
            if target.exists() or target.is_symlink():
                self._remove_node_modules(target)
                removed = True
        return removed

    def _clone_linker_tree(
        self,
        source_root: Path,
        destination_root: Path,
        linker_paths: tuple[Path, ...],
        deadline: float,
    ) -> CommandOutcome:
        for relative in linker_paths:
            self._clone_directory(source_root / relative, destination_root / relative, deadline)
        return CommandOutcome(
            ("pnpm-template", "clone", str(source_root), str(destination_root)),
            0,
            f"cloned {len(linker_paths)} worktree-local pnpm linker directories\n",
            "",
        )

    def _discard_incomplete_node_modules(self, worktree: Path) -> None:
        """Remove only an interrupted linker tree before a fresh offline seed.

        Recovery targets are newly allocated worktrees. A directory without
        pnpm's two completion markers can only be an interrupted prior
        materialization; retaining it makes pnpm rebuild the whole graph.
        """

        destination = worktree / "node_modules"
        if (destination.exists() or destination.is_symlink()) and not self._node_modules_is_complete(
            destination
        ):
            self._remove_node_modules(destination)

    @classmethod
    def _has_template_marker(cls, node_modules: Path, key: str) -> bool:
        if not cls._node_modules_is_complete(node_modules):
            return False
        try:
            return (node_modules / cls.TEMPLATE_MARKER_FILENAME).read_text(
                encoding="utf-8"
            ).strip() == key
        except (OSError, UnicodeError):
            return False

    @classmethod
    def _write_template_marker(cls, node_modules: Path, key: str) -> None:
        if not cls._node_modules_is_complete(node_modules):
            raise DirtyWorktreeRecoveryError(
                f"offline pnpm materialization did not produce a complete linker: {node_modules}"
            )
        (node_modules / cls.TEMPLATE_MARKER_FILENAME).write_text(key + "\n", encoding="utf-8")

    @staticmethod
    def _node_modules_is_complete(node_modules: Path) -> bool:
        return (
            node_modules.is_dir()
            and (node_modules / ".modules.yaml").is_file()
            and (node_modules / ".bin").is_dir()
        )

    @staticmethod
    def _remove_node_modules(node_modules: Path) -> None:
        if node_modules.is_symlink() or node_modules.is_file():
            node_modules.unlink()
        else:
            shutil.rmtree(node_modules)

    def _clone_directory(
        self, source: Path, destination: Path, deadline: float
    ) -> CommandOutcome:
        if destination.exists():
            raise DirtyWorktreeRecoveryError(
                f"pnpm dependency clone destination already exists: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        clone_option = "-cR" if platform.system() == "Darwin" else "-R"
        outcome = self._run(
            ("/bin/cp", clone_option, str(source), str(destination)),
            source.parent,
            os.environ.copy(),
            self._remaining_seconds(deadline),
        )
        if outcome.exit_code != 0:
            raise DirtyWorktreeRecoveryError(
                "pnpm dependency template clone failed: "
                f"{outcome.stderr.strip() or outcome.stdout.strip()}"
            )
        return outcome

    @staticmethod
    def _remaining_seconds(deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DirtyWorktreeRecoveryError(
                "offline pnpm materialization exhausted its bounded recovery window"
            )
        return max(1, math.ceil(remaining))

    @contextmanager
    def _shared_store_lock(self, deadline: float) -> Iterator[tuple[Path, float]]:
        lock_directory = self.store_dir or Path(tempfile.gettempdir())
        lock_path = lock_directory / self.LOCK_FILENAME
        try:
            handle = lock_path.open("a+", encoding="utf-8")
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                f"cannot open shared pnpm store lock {lock_path}: {error}"
            ) from error
        started = time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DirtyWorktreeRecoveryError(
                            "offline pnpm materialization timed out waiting for the shared pnpm store lock"
                        )
                    time.sleep(0.1)
            yield lock_path, round(time.monotonic() - started, 3)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _semver(value: str, *, label: str) -> tuple[int, int, int]:
        normalized = value.strip().split("+", 1)[0].split("-", 1)[0]
        fields = normalized.split(".")
        if len(fields) != 3 or any(not field.isdigit() for field in fields):
            raise DirtyWorktreeRecoveryError(f"{label} version is not semantic: {value!r}")
        return tuple(int(field) for field in fields)  # type: ignore[return-value]

    def _run(
        self,
        command: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandOutcome:
        try:
            completed = self.runner(
                list(command),
                cwd=cwd,
                env=dict(environment),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DirtyWorktreeRecoveryError(
                "offline pnpm materialization timed out after "
                f"{timeout_seconds}s; recovery stopped without retrying indefinitely"
            ) from error
        except OSError as error:
            raise DirtyWorktreeRecoveryError(f"cannot run {' '.join(command)}: {error}") from error
        return CommandOutcome(
            command,
            int(completed.returncode),
            _bounded(completed.stdout or ""),
            _bounded(completed.stderr or ""),
        )


class DirtyWorktreeRecovery:
    """Freeze, verify, and recover a code-bearing blocked worktree once."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        worktrees: WorktreeManager,
        *,
        materializer: PnpmOfflineMaterializer | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.artifacts = artifacts
        self.worktrees = worktrees
        self.materializer = materializer or PnpmOfflineMaterializer(runner=runner)
        self.runner = runner

    def capture(
        self,
        *,
        repository: str,
        base_sha: str,
        worktree: str,
        branch: str,
        attempt: int,
        expected_changed_paths: tuple[str, ...],
        task_id: str | None = None,
        node_id: str | None = None,
        input_tree_sha: str | None = None,
        dependency_input_ref: str | None = None,
        preserve_untracked_paths: tuple[str, ...] = (),
        expected_generated_residue_paths: tuple[str, ...] = (),
        expected_checkpoint_sha: str | None = None,
        source_only: bool = False,
        expected_source_delta_sha256: str | None = None,
    ) -> dict[str, object]:
        """Capture only the blocked worker's own patch.

        A dependent worker starts from a materialized ancestor tree rather
        than the contract commit. Its recovery receipt pins that exact input
        artifact and calculates the worker delta from that tree, so accepted
        ancestor patches never become part of the worker's patch. A root
        worker instead records the contract base tree directly and never
        fabricates a dependency-input artifact.
        """

        path = self._validate_worktree(repository, base_sha, worktree, branch,
                                       checkpoint_sha=expected_checkpoint_sha)
        if source_only:
            try:
                assert_recovery_source_idle(path)
            except RecoveryProcessError as error:
                raise DirtyWorktreeRecoveryError(
                    f"source-only recovery cannot prove the source worktree is idle: {error}"
                ) from error
        if dependency_input_ref is None:
            if any(value is not None for value in (task_id, node_id, input_tree_sha)):
                raise DirtyWorktreeRecoveryError(
                    "dependency recovery input requires its artifact ref"
                )
            comparison_tree = self._git_text(path, "rev-parse", f"{base_sha}^{{tree}}")
            recovery_context: dict[str, object] = {"schema_version": 1}
        else:
            if not all(
                isinstance(value, str) and value
                for value in (task_id, node_id, input_tree_sha, dependency_input_ref)
            ):
                raise DirtyWorktreeRecoveryError(
                    "dependency recovery input receipt is incomplete"
                )
            comparison_tree = self._git_text(
                path,
                "rev-parse",
                "--verify",
                f"{input_tree_sha}^{{tree}}",
            )
            recovery_context = {
                "schema_version": 2,
                "source_task_id": task_id,
                "source_node_id": node_id,
                "input_tree_sha": comparison_tree,
                "dependency_input_ref": dependency_input_ref,
            }
        reported_recoverable, reported_generated = partition_recovery_paths(
            tuple(sorted(expected_changed_paths))
        )
        expected_generated = tuple(
            sorted(set(reported_generated) | set(expected_generated_residue_paths))
        )
        if any(
            not is_python_bytecode_residue_path(relative_path)
            for relative_path in expected_generated
        ):
            raise DirtyWorktreeRecoveryError(
                "generated residue receipt contains a non-bytecode path"
            )
        if source_only and expected_generated:
            raise DirtyWorktreeRecoveryError(
                "source-only recovery must not discard generated residue"
            )
        if expected_source_delta_sha256 is not None and (
            not source_only
            or not isinstance(expected_source_delta_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_source_delta_sha256) is None
        ):
            raise DirtyWorktreeRecoveryError(
                "source-only recovery capture has an invalid source delta digest"
            )

        def validate_expected_paths(paths: tuple[str, ...]) -> None:
            if paths != reported_recoverable:
                raise DirtyWorktreeRecoveryError(
                    "worktree changed paths do not match the blocked worker receipt"
                )

        source_delta = (
            inspect_recovery_source_delta(
                path,
                comparison_tree,
                validate_paths=validate_expected_paths,
            )
            if expected_source_delta_sha256 is not None
            else None
        )
        if source_delta is not None and source_delta.sha256 != expected_source_delta_sha256:
            raise DirtyWorktreeRecoveryError(
                "source-only recovery source delta drifted before patch capture"
            )
        changed_paths = (
            source_delta.changed_paths
            if source_delta is not None
            else tuple(sorted(changed_paths_since_input_tree(path, comparison_tree)))
        )
        validate_expected_paths(changed_paths)
        for relative_path in changed_paths:
            validate_recovery_source_path(path, relative_path)
        generated_residue_ref = (
            None
            if source_only
            else self.discard_generated_residue(path, expected_generated)
        )
        untracked_paths = self.untracked_paths(path)
        requested_untracked = tuple(sorted(preserve_untracked_paths))
        if untracked_paths:
            if requested_untracked != untracked_paths:
                raise DirtyWorktreeRecoveryError(
                    "dirty worktree contains untracked files; pass the exact paths through explicit preservation: "
                    + summarize_recovery_paths(untracked_paths)
                )
            recovery_context = {
                **recovery_context,
                "schema_version": 3 if dependency_input_ref is not None else 7,
                "untracked_paths": list(untracked_paths),
            }
        elif requested_untracked:
            raise DirtyWorktreeRecoveryError(
                "explicit untracked preservation paths no longer match the dirty worktree"
            )
        if generated_residue_ref is not None:
            recovery_context = {
                **recovery_context,
                "schema_version": {1: 4, 2: 5, 3: 6, 7: 8}[int(recovery_context["schema_version"])],
                "generated_residue_paths": list(expected_generated),
                "generated_residue_ref": generated_residue_ref,
            }
        check = self._git_text(path, "diff", "--check", comparison_tree)
        if check:
            raise DirtyWorktreeRecoveryError(f"dirty worktree fails git diff --check: {check}")
        patch = self.captured_patch(path, comparison_tree, untracked_paths)
        if not patch:
            raise DirtyWorktreeRecoveryError("dirty worktree has no patch to preserve")
        if expected_source_delta_sha256 is not None:
            final_delta = inspect_recovery_source_delta(
                path,
                comparison_tree,
                validate_paths=validate_expected_paths,
            )
            if final_delta.sha256 != expected_source_delta_sha256:
                raise DirtyWorktreeRecoveryError(
                    "source-only recovery source delta drifted during patch capture"
                )
        patch_ref = self.artifacts.put_bytes(patch, "blocked-worktree.patch")
        # Recheck the explicit HEAD binding after filesystem capture, before sealing.
        self._validate_worktree(repository, base_sha, worktree, branch,
                                checkpoint_sha=expected_checkpoint_sha)
        if expected_checkpoint_sha is not None:
            recovery_context["source_checkpoint_sha"] = expected_checkpoint_sha
        return {
            **recovery_context,
            "source_attempt": attempt,
            "source_worktree": str(path),
            "source_branch": branch,
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
            "patch_ref": patch_ref,
            "patch_sha256": sha256(patch).hexdigest(),
        }

    def validate_retry_source(
        self,
        *,
        repository: str,
        base_sha: str,
        worktree: str,
        branch: str,
        expected_checkpoint_sha: str | None = None,
    ) -> Path:
        """Check a failed source binding before deciding whether it is clean."""

        return self._validate_worktree(repository, base_sha, worktree, branch,
                                       checkpoint_sha=expected_checkpoint_sha)

    def prepare(
        self,
        *,
        repository: str,
        source_worktree: str,
        target_worktree: str,
        target_branch: str,
        target_attempt: int,
        recovery: Mapping[str, object],
        acceptance_commands: tuple[str, ...],
        timeout_seconds: int,
        source_only: bool = False,
        expected_source_delta_sha256: str | None = None,
    ) -> RecoveryOutcome:
        """Prepare a verified recovery target without ever executing in source.

        The caller persists the prepared receipt only after this method has
        completed. Any failure therefore leaves the original blocked node and
        its source allocation authoritative.
        """

        if expected_source_delta_sha256 is not None and (
            not source_only
            or not isinstance(expected_source_delta_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_source_delta_sha256) is None
        ):
            raise DirtyWorktreeRecoveryError(
                "source-only recovery preparation has an invalid source delta digest"
            )
        if source_only and expected_source_delta_sha256 is None:
            raise DirtyWorktreeRecoveryError(
                "source-only recovery preparation requires a verified source delta digest"
            )
        try:
            source = self._validate_snapshot(
                repository, source_worktree, recovery, source_only=source_only
            )
            if source_only:
                assert expected_source_delta_sha256 is not None
                self._validate_source_only_delta(
                    source,
                    recovery,
                    expected_source_delta_sha256,
                    phase="before clean-target preparation",
                )
            target = self._validate_target(
                repository,
                target_worktree,
                target_branch,
                target_attempt,
                recovery,
            )
            comparison_tree = self._restore_recorded_input(target, recovery)
            patch = self._load_patch(recovery)
            patch_path = self._patch_path(recovery)
            self.worktrees.apply_patch(target, patch_path)
            self.mark_untracked_intent_to_add(
                target,
                self._recovery_untracked_paths(recovery),
            )
            if self._git_bytes(target, "diff", "--binary", comparison_tree) != patch:
                raise DirtyWorktreeRecoveryError(
                    "recovery target patch does not exactly match the captured source patch"
                )
            checks = [
                "PASS: blocked dirty worktree snapshot is unchanged",
                "PASS: recorded dependency input was reproduced on the clean recovery target",
                "PASS: captured worker patch was applied to the clean recovery target",
            ]
            materialization = self.materializer.materialize(target, timeout_seconds=timeout_seconds)
            materialization_ref = self.artifacts.put_text(
                json.dumps(materialization, ensure_ascii=False, sort_keys=True),
                "dependency-materialization.json",
            )
            checks.append(f"PASS: {materialization['kind']}")
            if not acceptance_commands:
                raise DirtyWorktreeRecoveryError(
                    "blocked worktree recovery requires declared acceptance_commands"
                )
            evidence_inputs = self._acceptance_evidence_inputs(
                comparison_tree=comparison_tree,
                recovery=recovery,
                materialization=materialization,
            )
            outcomes: list[CommandOutcome] = []
            prior_successful_command_fingerprints: list[str] = []
            for command_source in acceptance_commands:
                declared_command, command, environment_overrides = self._parse_command(
                    command_source
                )
                outcome = self._run_command(
                    declared_command,
                    target,
                    timeout_seconds,
                    executable=command,
                    environment_overrides=environment_overrides,
                    evidence_inputs=evidence_inputs,
                    prior_evidence_fingerprints=tuple(prior_successful_command_fingerprints),
                )
                outcomes.append(outcome)
                checks.append(
                    ("PASS" if outcome.exit_code == 0 else "FAIL")
                    + f": {' '.join(declared_command)} (exit {outcome.exit_code})"
                )
                if outcome.exit_code != 0:
                    log_ref = self._store_logs(materialization, outcomes)
                    self._validate_snapshot(
                        repository, str(source), recovery, source_only=source_only
                    )
                    if source_only:
                        assert expected_source_delta_sha256 is not None
                        self._validate_source_only_delta(
                            source,
                            recovery,
                            expected_source_delta_sha256,
                            phase="during clean-target preparation",
                        )
                    return RecoveryOutcome(
                        "failed",
                        "declared recovery acceptance command failed: "
                        + " ".join(declared_command),
                        {
                            "recovery-snapshot": str(recovery["patch_ref"]),
                            "dependency-materialization": materialization_ref,
                            "test-log": log_ref,
                        },
                        tuple(checks),
                        tuple(str(path) for path in recovery["changed_paths"]),
                        outcome.exit_code,
                    )
                if outcome.evidence_fingerprint is not None:
                    prior_successful_command_fingerprints.append(outcome.evidence_fingerprint)
            if self._git_bytes(target, "diff", "--binary", comparison_tree) != patch:
                raise DirtyWorktreeRecoveryError(
                    "recovery target changed after offline materialization or acceptance"
                )
            self._validate_snapshot(
                repository, str(source), recovery, source_only=source_only
            )
            if source_only:
                assert expected_source_delta_sha256 is not None
                self._validate_source_only_delta(
                    source,
                    recovery,
                    expected_source_delta_sha256,
                    phase="during clean-target preparation",
                )
            checks.append("PASS: blocked dirty worktree snapshot remained unchanged after verification")
            log_ref = self._store_logs(materialization, outcomes)
            prepared_recovery = {
                **dict(recovery),
                "target_attempt": target_attempt,
                "target_worktree": str(target),
                "target_branch": target_branch,
                "target_patch_sha256": sha256(patch).hexdigest(),
                "preparation_log_ref": log_ref,
            }
            return RecoveryOutcome(
                "succeeded",
                "captured blocked worktree patch passed declared offline recovery acceptance commands on a clean target",
                {
                    "recovery-snapshot": str(recovery["patch_ref"]),
                    "dependency-materialization": materialization_ref,
                    "test-log": log_ref,
                },
                tuple(checks),
                tuple(str(path) for path in recovery["changed_paths"]),
                prepared_recovery=prepared_recovery,
            )
        except DirtyWorktreeRecoveryError as error:
            return RecoveryOutcome(
                "blocked",
                str(error),
                {"recovery-snapshot": str(recovery["patch_ref"])}
                if isinstance(recovery.get("patch_ref"), str)
                else {},
                (f"BLOCKED: {error}",),
                tuple(str(path) for path in recovery.get("changed_paths", ()) if isinstance(path, str)),
            )

    def _validate_source_only_delta(
        self,
        source: Path,
        recovery: Mapping[str, object],
        expected_source_delta_sha256: str,
        *,
        phase: str,
    ) -> None:
        """Require the current non-ignored source delta to match its preview digest."""

        changed_paths = recovery.get("changed_paths")
        if (
            not isinstance(changed_paths, list)
            or not all(isinstance(path, str) and path for path in changed_paths)
        ):
            raise DirtyWorktreeRecoveryError(
                "source-only recovery receipt has invalid changed_paths"
            )
        expected_paths = tuple(sorted(changed_paths))
        comparison_tree, _, _, _ = self._recovery_input_context(source, recovery)

        def validate_paths(paths: tuple[str, ...]) -> None:
            if paths != expected_paths:
                raise DirtyWorktreeRecoveryError(
                    "source-only recovery source paths drifted " + phase
                )

        current = inspect_recovery_source_delta(
            source,
            comparison_tree,
            validate_paths=validate_paths,
        )
        if current.sha256 != expected_source_delta_sha256:
            raise DirtyWorktreeRecoveryError(
                "source-only recovery source delta drifted " + phase
            )

    def prepare_for_retry(
        self,
        *,
        repository: str,
        source_worktree: str,
        target_worktree: str,
        target_branch: str,
        target_attempt: int,
        recovery: Mapping[str, object],
        source_only: bool = False,
    ) -> RecoveryOutcome:
        """Restore a sealed failed attempt before its normal executor runs.

        Unlike ``prepare``, this deliberately does not execute acceptance
        commands or materialize dependencies: the retry's original executor
        still owns those actions.  It only proves that the old worker patch
        and recorded ancestor input can be reproduced on a fresh target
        without changing the source worktree.
        """

        try:
            source = self._validate_snapshot(
                repository, source_worktree, recovery, source_only=source_only
            )
            target = self._validate_target(
                repository,
                target_worktree,
                target_branch,
                target_attempt,
                recovery,
            )
            comparison_tree = self._restore_recorded_input(target, recovery)
            patch = self._load_patch(recovery)
            self.worktrees.apply_patch(target, self._patch_path(recovery))
            self.mark_untracked_intent_to_add(
                target,
                self._recovery_untracked_paths(recovery),
            )
            if self._git_bytes(target, "diff", "--binary", comparison_tree) != patch:
                raise DirtyWorktreeRecoveryError(
                    "retry target patch does not exactly match the captured failed attempt"
                )
            self._validate_snapshot(
                repository, str(source), recovery, source_only=source_only
            )
            return RecoveryOutcome(
                "succeeded",
                "captured failed-attempt patch was restored on a clean retry target before model dispatch",
                {"recovery-snapshot": str(recovery["patch_ref"])},
                (
                    "PASS: failed source worktree snapshot is unchanged",
                    "PASS: recorded dependency input was reproduced on the retry target",
                    "PASS: captured failed-attempt patch was restored before model dispatch",
                ),
                tuple(str(path) for path in recovery["changed_paths"]),
                prepared_recovery={
                    **dict(recovery),
                    "target_attempt": target_attempt,
                    "target_worktree": str(target),
                    "target_branch": target_branch,
                    "target_patch_sha256": sha256(patch).hexdigest(),
                },
            )
        except DirtyWorktreeRecoveryError as error:
            return RecoveryOutcome(
                "blocked",
                str(error),
                {"recovery-snapshot": str(recovery["patch_ref"])}
                if isinstance(recovery.get("patch_ref"), str)
                else {},
                (f"BLOCKED: {error}",),
                tuple(
                    str(path)
                    for path in recovery.get("changed_paths", ())
                    if isinstance(path, str)
                ),
            )

    def run(
        self,
        *,
        repository: str,
        worktree: str,
        recovery: Mapping[str, object],
        acceptance_commands: tuple[str, ...],
        timeout_seconds: int,
    ) -> RecoveryOutcome:
        """Fail closed for callers that attempt to run inside the dirty source."""

        return RecoveryOutcome(
            "blocked",
            "dirty-worktree recovery requires a fresh target worktree; use prepare",
            {"recovery-snapshot": str(recovery["patch_ref"])}
            if isinstance(recovery.get("patch_ref"), str)
            else {},
            (),
            tuple(str(path) for path in recovery.get("changed_paths", ()) if isinstance(path, str)),
        )


    def _validate_snapshot(
        self,
        repository: str,
        worktree: str,
        recovery: Mapping[str, object],
        *,
        source_only: bool = False,
    ) -> Path:
        required = {
            "source_attempt",
            "source_worktree",
            "source_branch",
            "base_sha",
            "changed_paths",
            "patch_ref",
            "patch_sha256",
        }
        if not required.issubset(recovery):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt is incomplete")
        base_sha = recovery["base_sha"]
        branch = recovery["source_branch"]
        changed_paths = recovery["changed_paths"]
        source_worktree = recovery["source_worktree"]
        source_attempt = recovery["source_attempt"]
        if not isinstance(base_sha, str) or not isinstance(branch, str):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid repository binding")
        if not isinstance(source_worktree, str) or not source_worktree:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid source_worktree")
        if isinstance(source_attempt, bool) or not isinstance(source_attempt, int):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid source_attempt")
        if not isinstance(changed_paths, list) or not all(isinstance(path, str) for path in changed_paths):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid changed_paths")
        path = self._validate_worktree(repository, base_sha, worktree, branch,
                                       checkpoint_sha=recovery.get("source_checkpoint_sha"))
        if source_only:
            try:
                assert_recovery_source_idle(path)
            except RecoveryProcessError as error:
                raise DirtyWorktreeRecoveryError(
                    f"source-only recovery cannot prove the source worktree is idle: {error}"
                ) from error
        try:
            expected_source = Path(source_worktree).expanduser().resolve(strict=True)
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                "blocked recovery receipt source_worktree cannot be resolved"
            ) from error
        if path != expected_source:
            raise DirtyWorktreeRecoveryError("recovery source does not match the captured worktree")
        comparison_tree, _, _, _ = self._recovery_input_context(path, recovery)
        current_paths = tuple(sorted(changed_paths_since_input_tree(path, comparison_tree)))
        if current_paths != tuple(sorted(changed_paths)):
            raise DirtyWorktreeRecoveryError("dirty worktree changed paths drifted after recovery was scheduled")
        for relative_path in current_paths:
            validate_recovery_source_path(path, relative_path)
        ignored_paths = self.ignored_paths(path)
        if ignored_paths and not source_only:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree acquired ignored paths after recovery was scheduled: "
                + summarize_recovery_paths(ignored_paths)
            )
        self._validate_generated_residue_receipt(recovery)
        untracked_paths = self.untracked_paths(path)
        if untracked_paths != self._recovery_untracked_paths(recovery):
            raise DirtyWorktreeRecoveryError(
                "dirty worktree untracked paths drifted after recovery was scheduled"
            )
        patch = self.captured_patch(path, comparison_tree, untracked_paths)
        expected_hash = recovery["patch_sha256"]
        if not isinstance(expected_hash, str) or sha256(patch).hexdigest() != expected_hash:
            raise DirtyWorktreeRecoveryError("dirty worktree patch drifted after recovery was scheduled")
        if self._load_patch(recovery) != patch:
            raise DirtyWorktreeRecoveryError("dirty worktree no longer matches its preserved patch artifact")
        return path

    def _recovery_input_context(
        self,
        worktree: Path,
        recovery: Mapping[str, object],
    ) -> tuple[str, str | None, str | None, str | None]:
        """Return the worker-input tree and optional recorded-input binding."""

        if "source_checkpoint_sha" in recovery:
            checkpoint = recovery["source_checkpoint_sha"]
            if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9a-f]{40}", checkpoint):
                raise DirtyWorktreeRecoveryError("checkpoint requires an exact full commit SHA")
        schema_version = recovery.get("schema_version")
        base_sha = recovery.get("base_sha")
        if not isinstance(base_sha, str) or not base_sha:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid base_sha")
        if schema_version in {1, 4, 7, 8}:
            root_receipt = {
                "schema_version",
                "source_attempt",
                "source_worktree",
                "source_branch",
                "base_sha",
                "changed_paths",
                "patch_ref",
                "patch_sha256",
            }
            if schema_version in {4, 8}:
                root_receipt |= {"generated_residue_paths", "generated_residue_ref"}
            if schema_version in {7, 8}:
                root_receipt.add("untracked_paths")
            if "source_checkpoint_sha" in recovery:
                root_receipt.add("source_checkpoint_sha")
            if set(recovery) != root_receipt:
                raise DirtyWorktreeRecoveryError("blocked root recovery receipt has an invalid shape")
            self._recovery_untracked_paths(recovery)
            return (
                self._git_text(worktree, "rev-parse", f"{base_sha}^{{tree}}"),
                None,
                None,
                None,
            )
        if schema_version not in {2, 3, 5, 6}:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt schema is unsupported")
        required = {
            "schema_version",
            "source_task_id",
            "source_node_id",
            "input_tree_sha",
            "dependency_input_ref",
            "source_attempt",
            "source_worktree",
            "source_branch",
            "base_sha",
            "changed_paths",
            "patch_ref",
            "patch_sha256",
        }
        if schema_version in {3, 6}:
            required.add("untracked_paths")
        if schema_version in {5, 6}:
            required |= {"generated_residue_paths", "generated_residue_ref"}
        if "source_checkpoint_sha" in recovery:
            required.add("source_checkpoint_sha")
        if set(recovery) != required:
            raise DirtyWorktreeRecoveryError("blocked dependency recovery receipt has an invalid shape")
        task_id = recovery["source_task_id"]
        node_id = recovery["source_node_id"]
        input_tree_sha = recovery["input_tree_sha"]
        dependency_input_ref = recovery["dependency_input_ref"]
        if not all(
            isinstance(value, str) and value
            for value in (task_id, node_id, input_tree_sha, dependency_input_ref)
        ):
            raise DirtyWorktreeRecoveryError("blocked dependency recovery receipt is incomplete")
        self._recovery_untracked_paths(recovery)
        return (
            self._git_text(worktree, "rev-parse", "--verify", f"{input_tree_sha}^{{tree}}"),
            task_id,
            node_id,
            dependency_input_ref,
        )

    def _validate_generated_residue_receipt(
        self,
        recovery: Mapping[str, object],
    ) -> None:
        schema_version = recovery.get("schema_version")
        if schema_version not in {4, 5, 6, 8}:
            return
        paths = recovery.get("generated_residue_paths")
        ref = recovery.get("generated_residue_ref")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(is_python_bytecode_residue_path(path) for path in paths)
            or tuple(paths) != tuple(sorted(set(paths)))
            or not isinstance(ref, str)
            or not ref
        ):
            raise DirtyWorktreeRecoveryError(
                "blocked recovery receipt has invalid generated residue evidence"
            )
        try:
            receipt = json.loads(self.artifacts.verify(ref).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise DirtyWorktreeRecoveryError(
                f"generated residue evidence is unavailable: {error}"
            ) from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("kind") != "python-bytecode-generated-residue"
            or receipt.get("expected_paths") != paths
            or not isinstance(receipt.get("observed_files"), list)
            or not isinstance(receipt.get("missing_paths"), list)
        ):
            raise DirtyWorktreeRecoveryError("generated residue evidence is invalid")
        observed_files = receipt["observed_files"]
        missing_paths = receipt["missing_paths"]
        if (
            not all(isinstance(path, str) for path in missing_paths)
            or tuple(missing_paths) != tuple(sorted(set(missing_paths)))
        ):
            raise DirtyWorktreeRecoveryError("generated residue missing-path evidence is invalid")
        observed_paths: list[str] = []
        for entry in observed_files:
            if not isinstance(entry, dict):
                raise DirtyWorktreeRecoveryError("generated residue file evidence is invalid")
            path = entry.get("path")
            digest = entry.get("sha256")
            size = entry.get("bytes")
            content_ref = entry.get("content_ref")
            if (
                not is_python_bytecode_residue_path(path)
                or not isinstance(digest, str)
                or len(digest) != 64
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(content_ref, str)
                or not content_ref
            ):
                raise DirtyWorktreeRecoveryError("generated residue file evidence is invalid")
            try:
                payload = self.artifacts.verify(content_ref).read_bytes()
            except (OSError, ValueError) as error:
                raise DirtyWorktreeRecoveryError(
                    f"generated residue content is unavailable: {error}"
                ) from error
            if len(payload) != size or sha256(payload).hexdigest() != digest:
                raise DirtyWorktreeRecoveryError(
                    "generated residue content does not match its evidence"
                )
            observed_paths.append(path)
        if (
            tuple(observed_paths) != tuple(sorted(set(observed_paths)))
            or set(observed_paths).intersection(missing_paths)
            or tuple(sorted((*observed_paths, *missing_paths))) != tuple(paths)
        ):
            raise DirtyWorktreeRecoveryError(
                "generated residue evidence does not cover its declared paths"
            )

    @staticmethod
    def ignored_paths(worktree: Path) -> tuple[str, ...]:
        """List ignored untracked paths so recovery cannot discard them silently."""

        raw = DirtyWorktreeRecovery._git_bytes(
            worktree,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        )
        return tuple(
            sorted(
                item.decode("utf-8", errors="surrogateescape")
                for item in raw.split(b"\0")
                if item
            )
        )

    def discard_generated_residue(
        self,
        worktree: Path,
        expected_paths: tuple[str, ...],
    ) -> str | None:
        """Archive and remove only receipt-declared Python bytecode residue.

        Git-ignored content remains unsafe by default.  The sole exception is
        a canonical ``__pycache__/*.pyc`` path already named by the failed
        result.  Every present regular file is copied into ArtifactStore and
        hashed before unlinking; symlinks, path escapes, unexpected ignored
        files, and files that change during capture abort recovery.
        """

        expected = tuple(sorted(set(expected_paths)))
        ignored = self.ignored_paths(worktree)
        unexpected = tuple(
            path for path in ignored if not is_python_bytecode_residue_path(path)
        )
        if unexpected:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree contains ignored paths that cannot be recovered safely: "
                + summarize_recovery_paths(unexpected)
            )
        observed = tuple(path for path in ignored if is_python_bytecode_residue_path(path))
        undeclared = tuple(sorted(set(observed) - set(expected)))
        if undeclared:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree contains unreported generated residue: "
                + summarize_recovery_paths(undeclared)
            )
        if not expected and not observed:
            return None

        root = worktree.resolve(strict=True)
        entries: list[dict[str, object]] = []
        snapshots: dict[str, tuple[int, int, int, int, str]] = {}
        for relative_path in observed:
            candidate = root / relative_path
            try:
                metadata = candidate.lstat()
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as error:
                raise DirtyWorktreeRecoveryError(
                    f"generated residue path is unavailable or escapes the worktree: {relative_path}"
                ) from error
            if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise DirtyWorktreeRecoveryError(
                    f"generated residue path must be a regular non-symlink file: {relative_path}"
                )
            try:
                payload = candidate.read_bytes()
                after_read = candidate.lstat()
            except OSError as error:
                raise DirtyWorktreeRecoveryError(
                    f"cannot capture generated residue {relative_path}: {error}"
                ) from error
            identity = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
            if identity != (
                int(after_read.st_dev),
                int(after_read.st_ino),
                int(after_read.st_size),
                int(after_read.st_mtime_ns),
            ):
                raise DirtyWorktreeRecoveryError(
                    f"generated residue changed during capture: {relative_path}"
                )
            digest = sha256(payload).hexdigest()
            content_ref = self.artifacts.put_bytes(payload, "python-bytecode.pyc")
            snapshots[relative_path] = (*identity, digest)
            entries.append(
                {
                    "path": relative_path,
                    "sha256": digest,
                    "bytes": len(payload),
                    "content_ref": content_ref,
                }
            )

        receipt = {
            "schema_version": 1,
            "kind": "python-bytecode-generated-residue",
            "expected_paths": list(expected),
            "observed_files": entries,
            "missing_paths": list(sorted(set(expected) - set(observed))),
        }
        receipt_ref = self.artifacts.put_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "generated-residue.json",
        )
        for relative_path in observed:
            candidate = root / relative_path
            try:
                metadata = candidate.lstat()
                payload = candidate.read_bytes()
            except OSError as error:
                raise DirtyWorktreeRecoveryError(
                    f"generated residue changed before removal: {relative_path}: {error}"
                ) from error
            expected_identity = snapshots[relative_path]
            current_identity = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
                sha256(payload).hexdigest(),
            )
            if candidate.is_symlink() or current_identity != expected_identity:
                raise DirtyWorktreeRecoveryError(
                    f"generated residue changed before removal: {relative_path}"
                )
            candidate.unlink()
            cache_directory = candidate.parent
            if cache_directory.name == "__pycache__":
                try:
                    cache_directory.rmdir()
                except OSError:
                    pass
        remaining = self.ignored_paths(worktree)
        if remaining:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree acquired ignored paths during generated-residue capture: "
                + summarize_recovery_paths(remaining)
            )
        return receipt_ref

    @staticmethod
    def untracked_paths(worktree: Path) -> tuple[str, ...]:
        raw = DirtyWorktreeRecovery._git_bytes(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        paths = tuple(sorted(item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item))
        for relative_path in paths:
            candidate = worktree / relative_path
            validate_recovery_source_path(worktree, relative_path)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise DirtyWorktreeRecoveryError(
                    f"cannot preserve untracked recovery file {relative_path!r}: {error}"
                ) from error
            if not resolved.is_relative_to(worktree.resolve()) or candidate.is_symlink() or not candidate.is_file():
                raise DirtyWorktreeRecoveryError(
                    f"untracked recovery path must be a regular file inside its worktree: {relative_path!r}"
                )
        return paths

    @staticmethod
    def captured_patch(
        worktree: Path,
        comparison_tree: str,
        untracked_paths: tuple[str, ...] = (),
    ) -> bytes:
        tracked = DirtyWorktreeRecovery._git_bytes(worktree, "diff", "--binary", comparison_tree)
        if not untracked_paths:
            return tracked
        actual_paths = DirtyWorktreeRecovery.untracked_paths(worktree)
        if actual_paths != tuple(untracked_paths):
            raise DirtyWorktreeRecoveryError("untracked recovery paths changed while capturing patch")
        tracked_paths = tuple(
            sorted(
                line
                for line in DirtyWorktreeRecovery._git_bytes(
                    worktree, "diff", "--name-only", "--no-renames", comparison_tree, "--"
                )
                .decode(errors="surrogateescape")
                .splitlines()
                if line
            )
        )
        additions: list[bytes] = []
        untracked = set(untracked_paths)
        for relative_path in sorted((*tracked_paths, *untracked_paths)):
            if relative_path not in untracked:
                additions.append(
                    DirtyWorktreeRecovery._git_bytes(
                        worktree, "diff", "--binary", comparison_tree, "--", relative_path
                    )
                )
                continue
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    "/dev/null",
                    relative_path,
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode not in {0, 1} or not result.stdout:
                raise DirtyWorktreeRecoveryError(
                    result.stderr.decode(errors="replace").strip()
                    or f"cannot capture untracked recovery file {relative_path!r}"
                )
            additions.append(bytes(result.stdout))
        return b"".join(additions)

    @staticmethod
    def mark_untracked_intent_to_add(worktree: Path, untracked_paths: tuple[str, ...]) -> None:
        if not untracked_paths:
            return
        current_paths = DirtyWorktreeRecovery.untracked_paths(worktree)
        # `git apply --3way` may already stage a no-index new-file patch. In
        # that case there is nothing left to mark; the exact combined patch
        # comparison immediately after this call still proves the target.
        if not current_paths:
            return
        if current_paths != tuple(untracked_paths):
            raise DirtyWorktreeRecoveryError(
                "recovery target untracked paths do not match the preserved source paths"
            )
        result = subprocess.run(
            ["git", "-C", str(worktree), "add", "--intent-to-add", "--", *untracked_paths],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise DirtyWorktreeRecoveryError(
                result.stderr.strip() or result.stdout.strip() or "cannot record recovered untracked paths"
            )

    @staticmethod
    def _recovery_untracked_paths(recovery: Mapping[str, object]) -> tuple[str, ...]:
        schema_version = recovery.get("schema_version")
        if schema_version in {1, 2, 4, 5}:
            return ()
        if schema_version not in {3, 6, 7, 8}:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt schema is unsupported")
        paths = recovery.get("untracked_paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path for path in paths)
            or tuple(paths) != tuple(sorted(set(paths)))
        ):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid untracked_paths")
        return tuple(paths)

    def _restore_recorded_input(
        self,
        target: Path,
        recovery: Mapping[str, object],
    ) -> str:
        comparison_tree, task_id, node_id, dependency_input_ref = self._recovery_input_context(
            target,
            recovery,
        )
        if dependency_input_ref is None:
            if self._git_text(target, "write-tree") != comparison_tree:
                raise DirtyWorktreeRecoveryError("clean recovery target does not match contract input tree")
            return comparison_tree
        base_sha = recovery["base_sha"]
        assert isinstance(base_sha, str)
        try:
            restored = apply_recorded_dependency_input(
                self.artifacts,
                self.worktrees,
                ref=dependency_input_ref,
                task_id=str(task_id),
                node_id=str(node_id),
                base_sha=base_sha,
                worktree=target,
            )
        except DependencyInputError as error:
            raise DirtyWorktreeRecoveryError(
                f"cannot reproduce recorded dependency input: {error}"
            ) from error
        if restored.input_tree_sha != comparison_tree:
            raise DirtyWorktreeRecoveryError(
                "recorded dependency input tree differs from the recovery receipt"
            )
        return comparison_tree

    def _validate_target(
        self,
        repository: str,
        worktree: str,
        branch: str,
        attempt: int,
        recovery: Mapping[str, object],
    ) -> Path:
        source_attempt = recovery.get("source_attempt")
        source_worktree = recovery.get("source_worktree")
        base_sha = recovery.get("base_sha")
        if isinstance(source_attempt, bool) or not isinstance(source_attempt, int):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid source_attempt")
        if not isinstance(source_worktree, str) or not source_worktree:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid source_worktree")
        if not isinstance(base_sha, str) or not base_sha:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid base_sha")
        if attempt != source_attempt + 1:
            raise DirtyWorktreeRecoveryError(
                "recovery target attempt must immediately follow the blocked attempt"
            )
        target = self._validate_worktree(repository, base_sha, worktree, branch)
        try:
            source = Path(source_worktree).expanduser().resolve(strict=True)
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                "blocked recovery receipt source_worktree cannot be resolved"
            ) from error
        if target == source:
            raise DirtyWorktreeRecoveryError(
                "recovery target must be a fresh worktree, not the dirty source"
            )
        status = self._git_bytes(
            target,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
        )
        if status:
            raise DirtyWorktreeRecoveryError(
                "recovery target must be clean before the captured patch is applied"
            )
        return target

    def _patch_path(self, recovery: Mapping[str, object]) -> Path:
        patch_ref = recovery.get("patch_ref")
        if not isinstance(patch_ref, str):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid patch_ref")
        try:
            return self.artifacts.verify(patch_ref)
        except (OSError, ValueError) as error:
            raise DirtyWorktreeRecoveryError(
                f"blocked recovery patch artifact is unavailable: {error}"
            ) from error

    def _load_patch(self, recovery: Mapping[str, object]) -> bytes:
        expected_hash = recovery.get("patch_sha256")
        if not isinstance(expected_hash, str):
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid patch_sha256")
        patch = self._patch_path(recovery).read_bytes()
        if sha256(patch).hexdigest() != expected_hash:
            raise DirtyWorktreeRecoveryError(
                "blocked recovery patch artifact hash does not match receipt"
            )
        return patch


    def _validate_worktree(self, repository: str, base_sha: str, worktree: str, branch: str,
                           *, checkpoint_sha: object = None) -> Path:
        repo = Path(repository).expanduser().resolve(strict=True)
        path = Path(worktree).expanduser().resolve(strict=True)
        root = self.worktrees.root.expanduser().resolve(strict=False)
        if not path.is_relative_to(root):
            raise DirtyWorktreeRecoveryError("recovery worktree is outside the Workbench worktree root")
        if self._git_text(path, "rev-parse", "--show-toplevel") != str(path):
            raise DirtyWorktreeRecoveryError("recovery path is not a standalone Git worktree")
        if self._git_text(
            path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ) != self._git_text(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ):
            raise DirtyWorktreeRecoveryError("recovery source belongs to another repository")
        base = self._git_text(repo, "rev-parse", f"{base_sha}^{{commit}}")
        expected_head = base
        if checkpoint_sha is not None:
            if not isinstance(checkpoint_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", checkpoint_sha):
                raise DirtyWorktreeRecoveryError("checkpoint requires an exact full commit SHA")
            if self._git_text(path, "merge-base", base, checkpoint_sha) != base:
                raise DirtyWorktreeRecoveryError("checkpoint does not descend from the contract base")
            expected_head = checkpoint_sha
        if self._git_text(path, "rev-parse", "HEAD") != expected_head:
            raise DirtyWorktreeRecoveryError("recovery worktree no longer matches its contract base")
        if self._git_text(path, "branch", "--show-current") != branch:
            raise DirtyWorktreeRecoveryError("recovery worktree no longer matches its allocated branch")
        return path

    @staticmethod
    def _parse_command(
        source: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
        try:
            declared = tuple(shlex.split(source))
        except ValueError as error:
            raise DirtyWorktreeRecoveryError(f"invalid acceptance command: {error}") from error
        if not declared or any(
            token in {"|", "||", "&&", ";", ">", "<"}
            for token in declared
        ):
            raise DirtyWorktreeRecoveryError("recovery acceptance command must be an argv-only command")
        environment: list[tuple[str, str]] = []
        command_start = 0
        for token in declared:
            if "=" not in token:
                break
            name, value = token.split("=", 1)
            if (
                not name
                or not (name[0].isalpha() or name[0] == "_")
                or not all(character.isalnum() or character == "_" for character in name)
            ):
                raise DirtyWorktreeRecoveryError(
                    f"invalid recovery acceptance environment assignment: {token}"
                )
            if name not in _RECOVERY_ACCEPTANCE_ENVIRONMENT:
                raise DirtyWorktreeRecoveryError(
                    f"recovery acceptance environment variable {name} is not permitted"
                )
            if any(existing == name for existing, _ in environment):
                raise DirtyWorktreeRecoveryError(
                    f"recovery acceptance environment variable {name} is duplicated"
                )
            environment.append((name, value))
            command_start += 1
        command = declared[command_start:]
        if not command:
            raise DirtyWorktreeRecoveryError(
                "recovery acceptance command requires an executable after environment assignments"
            )
        return declared, command, tuple(environment)

    @staticmethod
    def _acceptance_evidence_inputs(
        *,
        comparison_tree: str,
        recovery: Mapping[str, object],
        materialization: Mapping[str, object],
    ) -> dict[str, object]:
        """Return the static, already-known inputs for one acceptance command.

        This receipt is evidence for a command that still executes on every
        recovery attempt.  It neither authorizes a result cache nor makes a
        fingerprint reusable across another worktree.
        """

        incomplete: list[str] = []

        def required_string(container: Mapping[str, object], name: str, label: str) -> str | None:
            value = container.get(name)
            if isinstance(value, str) and value:
                return value
            incomplete.append(label)
            return None

        base_sha = required_string(recovery, "base_sha", "recovery base_sha")
        patch_sha256 = required_string(recovery, "patch_sha256", "recovery patch_sha256")
        if not isinstance(comparison_tree, str) or not comparison_tree:
            incomplete.append("comparison tree")
        dependency_input_ref = recovery.get("dependency_input_ref")
        if dependency_input_ref is not None and not isinstance(dependency_input_ref, str):
            incomplete.append("dependency input ref")
            dependency_input_ref = None

        materialization_kind = materialization.get("kind")
        if not isinstance(materialization_kind, str) or not materialization_kind:
            incomplete.append("dependency materialization kind")
            materialization_kind = None
        materialization_inputs: dict[str, object] = {"kind": materialization_kind}
        if materialization_kind == "pnpm-offline-materialization":
            lockfile_sha256 = required_string(
                materialization, "lockfile_sha256", "pnpm lockfile digest"
            )
            template = materialization.get("template")
            if not isinstance(template, Mapping):
                incomplete.append("pnpm template receipt")
                template_inputs: dict[str, object] | None = None
            else:
                template_inputs = {
                    key: template[key]
                    for key in ("state", "key")
                    if key in template
                }
                if not isinstance(template_inputs.get("state"), str):
                    incomplete.append("pnpm template state")
                # A disabled template legitimately has no key.  A cache hit,
                # reuse, or seed must bind the actual template identity.
                if template_inputs.get("state") in {"hit", "reuse", "seeded"} and not isinstance(
                    template_inputs.get("key"), str
                ):
                    incomplete.append("pnpm template key")
            materialization_inputs["lockfile_sha256"] = lockfile_sha256
            materialization_inputs["template"] = template_inputs
        else:
            # A non-Node target has no lockfile/template input.  Its complete
            # materialization receipt hash still records that explicit state.
            materialization_inputs["lockfile_sha256"] = None
            materialization_inputs["template"] = None

        # Lock acquisition timing and private cache paths describe the local
        # materializer's mechanics, not an acceptance input.  Bind the pnpm
        # receipt's stable command/result identities instead, so identical
        # inputs do not receive a new fingerprint merely because a lock waited.
        command_receipts = materialization.get("commands")
        normalized_commands: list[dict[str, object]] | None
        if materialization_kind == "pnpm-offline-materialization":
            if not isinstance(command_receipts, list):
                normalized_commands = None
            else:
                normalized_commands = []
                for command_receipt in command_receipts:
                    if not isinstance(command_receipt, Mapping):
                        normalized_commands = None
                        break
                    command_value = command_receipt.get("command")
                    exit_code = command_receipt.get("exit_code")
                    stdout = command_receipt.get("stdout")
                    stderr = command_receipt.get("stderr")
                    if (
                        not isinstance(command_value, list)
                        or not all(isinstance(value, str) for value in command_value)
                        or isinstance(exit_code, bool)
                        or not isinstance(exit_code, int)
                        or not isinstance(stdout, str)
                        or not isinstance(stderr, str)
                    ):
                        normalized_commands = None
                        break
                    normalized_commands.append(
                        {
                            "command_sha256": sha256(
                                _canonical_evidence_json(command_value)
                            ).hexdigest(),
                            "exit_code": exit_code,
                            "stdout_sha256": sha256(stdout.encode("utf-8")).hexdigest(),
                            "stderr_sha256": sha256(stderr.encode("utf-8")).hexdigest(),
                        }
                    )
            if normalized_commands is None:
                incomplete.append("pnpm materialization receipt")
        else:
            normalized_commands = []
        pnpm_receipt = {
            "kind": materialization_kind,
            "package_manager": materialization.get("package_manager"),
            "pnpm_version": materialization.get("pnpm_version"),
            "materialization_timeout_seconds": materialization.get(
                "materialization_timeout_seconds"
            ),
            "template": materialization_inputs["template"],
            "commands": normalized_commands,
        }
        try:
            materialization_inputs["pnpm_receipt_sha256"] = sha256(
                _canonical_evidence_json(pnpm_receipt)
            ).hexdigest()
        except (TypeError, ValueError, UnicodeError):
            materialization_inputs["pnpm_receipt_sha256"] = None
            incomplete.append("pnpm materialization receipt")

        return {
            "schema_version": 1,
            "recovery": {
                "comparison_tree": comparison_tree if isinstance(comparison_tree, str) else None,
                "base_sha": base_sha,
                "patch_sha256": patch_sha256,
                "dependency_input_ref": dependency_input_ref,
            },
            "materialization": materialization_inputs,
            "coverage": _recovery_evidence_coverage(incomplete),
        }

    @staticmethod
    def _command_evidence_inputs(
        *,
        static_inputs: Mapping[str, object],
        declared_command: tuple[str, ...],
        command: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        shim_directory: Path,
        prior_evidence_fingerprints: tuple[str, ...],
    ) -> dict[str, object]:
        """Bind one direct child invocation to static recovery inputs.

        The evidence records executable metadata rather than recursively
        hashing a runtime or dependency graph.  A missing executable identity
        is visible as incomplete coverage, never treated as equivalent input.
        """

        evidence = _json_evidence_copy(static_inputs)
        incomplete: list[str] = []
        if evidence is None:
            evidence = {"schema_version": 1, "static_inputs": "unavailable"}
            incomplete.append("declared recovery inputs")
        existing_coverage = evidence.get("coverage")
        if not isinstance(existing_coverage, Mapping):
            incomplete.append("declared recovery input coverage")
        else:
            existing_reasons = existing_coverage.get("incomplete_reasons")
            if existing_coverage.get("complete") is not True:
                incomplete.append("declared recovery inputs")
            if isinstance(existing_reasons, list):
                incomplete.extend(
                    reason for reason in existing_reasons if isinstance(reason, str)
                )

        try:
            actual_cwd = cwd.resolve(strict=True)
        except OSError:
            actual_cwd = cwd.resolve(strict=False)
            incomplete.append("command cwd")
        node_identity = _runtime_file_identity(
            _resolve_child_executable("node", actual_cwd, environment), actual_cwd
        )
        command_identity = _runtime_file_identity(
            _effective_command_entry(command, actual_cwd, environment, shim_directory),
            actual_cwd,
        )
        command_name = Path(command[0]).name
        node_script_path, node_script_expected = _direct_node_script_entry(command, actual_cwd)
        node_script_identity = (
            _runtime_file_identity(node_script_path, actual_cwd)
            if node_script_expected
            else {"status": "not-applicable"}
        )
        if command_identity.get("status") != "resolved":
            incomplete.append("command entry")
        if command_identity.get("launcher_hash_status") == "unavailable":
            incomplete.append("command launcher hash")
        node_required = command_name in {"node", "pnpm"}
        if node_required and node_identity.get("status") != "resolved":
            incomplete.append("node runtime")
        if node_required and node_identity.get("launcher_hash_status") == "unavailable":
            incomplete.append("node launcher hash")
        if node_script_expected and node_script_identity.get("status") != "resolved":
            incomplete.append("node script entry")
        if node_script_expected and node_script_identity.get("launcher_hash_status") == "unavailable":
            incomplete.append("node script hash")
        if any(
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            for fingerprint in prior_evidence_fingerprints
        ):
            incomplete.append("prior successful command fingerprint")

        evidence["command"] = {
            "declared_argv": list(declared_command),
            "effective_argv": list(command),
        }
        evidence["execution_scope"] = {
            "cwd": str(actual_cwd),
            "cross_worktree_reuse": "not-supported",
            "cache": "none",
        }
        evidence["environment"] = _environment_evidence(environment, shim_directory)
        evidence["runtime"] = {
            "node": node_identity,
            "command_entry": command_identity,
            "node_script_entry": node_script_identity,
        }
        evidence["prior_successful_command_fingerprints"] = list(prior_evidence_fingerprints)
        evidence["coverage"] = _recovery_evidence_coverage(incomplete)
        return evidence

    def _run_command(
        self,
        declared_command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        *,
        executable: tuple[str, ...] | None = None,
        environment_overrides: tuple[tuple[str, str], ...] = (),
        evidence_inputs: Mapping[str, object] | None = None,
        prior_evidence_fingerprints: tuple[str, ...] = (),
    ) -> CommandOutcome:
        command = executable or declared_command
        command_evidence: dict[str, object] | None = None
        evidence_fingerprint: str | None = None
        try:
            # Recovery first materializes an independent worktree-local linker.
            # Its acceptance command must use the same process-local pnpm shim
            # as a Codex worker; otherwise `pnpm exec` re-selects the user's
            # global pnpm and launches an unrelated `pnpm install`.
            with tempfile.TemporaryDirectory(prefix="codex-workbench-recovery-pnpm-shim-") as shim_directory:
                environment = codex_subscription_environment(
                    pnpm_shim_directory=Path(shim_directory)
                )
                environment.update({
                    "CI": "true",
                    "NO_UPDATE_NOTIFIER": "1",
                    "npm_config_offline": "true",
                })
                environment.update(environment_overrides)
                if evidence_inputs is not None:
                    command_evidence = self._command_evidence_inputs(
                        static_inputs=evidence_inputs,
                        declared_command=declared_command,
                        command=command,
                        cwd=cwd,
                        environment=environment,
                        shim_directory=Path(shim_directory),
                        prior_evidence_fingerprints=prior_evidence_fingerprints,
                    )
                    evidence_fingerprint = sha256(
                        _canonical_evidence_json(command_evidence)
                    ).hexdigest()
                completed = self.runner(
                    list(command),
                    cwd=cwd,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DirtyWorktreeRecoveryError(
                "cannot run recovery acceptance command "
                f"{' '.join(declared_command)}: {error}"
            ) from error
        return CommandOutcome(
            declared_command,
            int(completed.returncode),
            _bounded(completed.stdout or ""),
            _bounded(completed.stderr or ""),
            command_evidence,
            evidence_fingerprint,
        )

    def _store_logs(self, materialization: Mapping[str, object], outcomes: list[CommandOutcome]) -> str:
        return self.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "materialization": materialization,
                    "acceptance_commands": [outcome.to_dict() for outcome in outcomes],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "blocked-worktree-recovery.json",
        )

    @staticmethod
    def _git_text(path: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise DirtyWorktreeRecoveryError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    @staticmethod
    def _git_bytes(path: Path, *arguments: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise DirtyWorktreeRecoveryError(
                result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            )
        return result.stdout
