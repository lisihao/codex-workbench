"""Narrow, operator-authorized sandbox execution for three fixed DSH checks.

This module does not schedule work, inspect task state, or invoke a model. It
turns one approved validation identifier into exact argv and filesystem grants,
then records the bounded result of that argv in a fresh private directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping

from .artifacts import ArtifactStore
from .config import WorkbenchConfig


_CHECK_IPC = "dsh-b-ipc-v1"
_CHECK_PAIRING = "dsh-b-pairing-check-v1"
_CHECK_PAIRING_WRITE = "dsh-b-pairing-write-v1"
_CHECK_IDS = frozenset({_CHECK_IPC, _CHECK_PAIRING, _CHECK_PAIRING_WRITE})
_MAX_TIMEOUT_SECONDS = 60
_OUTPUT_LIMIT_BYTES = 128 * 1024
_SCRATCH_PLACEHOLDER = "<private-scratch>"
# macOS resolves /tmp to /private/tmp; Linux CI uses /tmp directly.
_PRIVATE_TEMP_ROOT = Path("/tmp").resolve()
_GIT_BINARY = "/usr/bin/git"

_README_ANCHORS = (
    "packages/client/connection/README.md",
    "packages/core/system-prompt/README.md",
    "packages/physical-operator/resident-operator-local/README.md",
    "packages/physical-operator/resident-operator/README.md",
    "packages/physical-operator/tool-physical-operator/README.md",
)

_IPC_VITEST_CASES = (
    (
        "packages/orchestration/orchestration-local/tests/prime-rlm-e2e.spec.ts",
        "keeps Standard direct, lets Auto choose, and runs explicit RLM through two async children, family messages, a goal, and a boundary refinement",
    ),
    (
        "packages/physical-operator/tool-physical-operator/tests/routing.spec.ts",
        "serves model tools when DSH_HOME exceeds Unix socket limits",
    ),
    (
        "packages/physical-operator/resident-operator-local/tests/client.spec.ts",
        "performs the handshake and business method on the same transport",
    ),
    (
        "packages/physical-operator/resident-operator-local/tests/daemon.spec.ts",
        "starts and accepts clients when the durable root exceeds Unix socket limits",
    ),
    (
        "packages/physical-operator/resident-operator-local/tests/codex-transport.spec.ts",
        "performs a WebSocket upgrade over Unix and maps JSON frames to NDJSON lines",
    ),
    (
        "packages/physical-operator/resident-operator-local/tests/drivers.spec.ts",
        "serves typescript_repl through the Agent SDK MCP adapter with a stable Receipt identity",
    ),
)


class ControlledValidationError(ValueError):
    """A requested validation cannot be proven to be within its fixed policy."""


@dataclass(frozen=True)
class ExecutableIdentity:
    """The small stat identity that pins one already-resolved executable."""

    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int

    def to_dict(self) -> dict[str, int]:
        """Return JSON-safe executable metadata without reading file contents."""

        return {
            "st_dev": self.st_dev,
            "st_ino": self.st_ino,
            "st_size": self.st_size,
            "st_mtime_ns": self.st_mtime_ns,
        }


@dataclass(frozen=True)
class ValidationRuntime:
    """The Authority-pinned executables used by a controlled validation."""

    codex_binary: Path
    pnpm_binary: Path
    node_binary: Path
    codex_identity: ExecutableIdentity = field(init=False, repr=False)
    pnpm_identity: ExecutableIdentity = field(init=False, repr=False)
    node_identity: ExecutableIdentity = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for field_name, identity_name in (
            ("codex_binary", "codex_identity"),
            ("pnpm_binary", "pnpm_identity"),
            ("node_binary", "node_identity"),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ControlledValidationError(
                    f"validation runtime {field_name} must be an absolute path"
                )
            resolved = _runtime_executable(value, f"validation runtime {field_name}")
            object.__setattr__(self, field_name, resolved)
            object.__setattr__(
                self,
                identity_name,
                _executable_identity(resolved, f"validation runtime {field_name}"),
            )

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe pinned executable identities."""

        return {
            "codex_binary": str(self.codex_binary),
            "pnpm_binary": str(self.pnpm_binary),
            "node_binary": str(self.node_binary),
            "executable_identity": {
                "codex": self.codex_identity.to_dict(),
                "pnpm": self.pnpm_identity.to_dict(),
                "node": self.node_identity.to_dict(),
            },
        }


@dataclass(frozen=True)
class ValidationCommand:
    """One exact child argv in a fixed validation plan."""

    purpose: str
    argv: tuple[str, ...]
    expected_test_title: str | None = None
    report_file: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe command evidence."""

        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "expected_test_title": self.expected_test_title,
            "report_file": self.report_file,
        }


@dataclass(frozen=True)
class ReadmeBinding:
    """The selected bilingual README pair and its current content identities."""

    anchor: str
    translated_path: str
    sidecar_path: str
    source_blob_sha1: str
    translated_blob_sha1: str
    sidecar_sha256: str

    def to_dict(self) -> dict[str, str]:
        """Return JSON-safe selected-pair identities."""

        return {
            "anchor": self.anchor,
            "translated_path": self.translated_path,
            "sidecar_path": self.sidecar_path,
            "source_blob_sha1": self.source_blob_sha1,
            "translated_blob_sha1": self.translated_blob_sha1,
            "sidecar_sha256": self.sidecar_sha256,
        }


@dataclass(frozen=True)
class ValidationPlan:
    """A static whitelist plan with no per-run private directory identity."""

    worktree: Path
    check_id: str
    runtime: ValidationRuntime
    commands: tuple[ValidationCommand, ...]
    readme_bindings: tuple[ReadmeBinding, ...]
    write_paths: tuple[Path, ...]
    grants: tuple[Path, ...]
    preparation_directories: tuple[Path, ...]
    git_common_dir: Path | None
    allow_unix_socket: bool
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        """Return the static JSON-safe plan used for preview and journaling."""

        return {
            "schema_version": 1,
            "worktree": str(self.worktree),
            "check_id": self.check_id,
            "runtime": self.runtime.to_dict(),
            "commands": [command.to_dict() for command in self.commands],
            "readme_bindings": [binding.to_dict() for binding in self.readme_bindings],
            "write_paths": [str(path) for path in self.write_paths],
            "grants": [str(path) for path in self.grants],
            "preparation_directories": [str(path) for path in self.preparation_directories],
            "git_common_dir": str(self.git_common_dir) if self.git_common_dir else None,
            "sandbox": {
                "private_scratch": _SCRATCH_PLACEHOLDER,
                "allow_unix_socket": self.allow_unix_socket,
                "network": False,
            },
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ValidationCommandReceipt:
    """One bounded sandbox child result retained as artifact references."""

    purpose: str
    argv: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    stdout_ref: str
    stderr_ref: str
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None = None
    test_assertion: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe child execution evidence."""

        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "error": self.error,
            "test_assertion": dict(self.test_assertion) if self.test_assertion else None,
        }


@dataclass(frozen=True)
class ValidationResult:
    """The complete bounded receipt for one controlled validation plan."""

    plan_fingerprint: str
    check_id: str
    ok: bool
    duration_ms: int
    commands: tuple[ValidationCommandReceipt, ...]
    controlled_environment: dict[str, str]
    grants: tuple[str, ...]
    preflight_checks: tuple[dict[str, object], ...]
    postflight_checks: tuple[dict[str, object], ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe evidence without a live scratch path."""

        return {
            "schema_version": 1,
            "plan_fingerprint": self.plan_fingerprint,
            "check_id": self.check_id,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "commands": [command.to_dict() for command in self.commands],
            "controlled_environment": dict(self.controlled_environment),
            "grants": list(self.grants),
            "preflight_checks": [dict(check) for check in self.preflight_checks],
            "postflight_checks": [dict(check) for check in self.postflight_checks],
            "error": self.error,
        }


def resolve_runtime(config: WorkbenchConfig) -> ValidationRuntime:
    """Resolve only Authority-installed executable paths, never the user PATH."""

    if config.deployment_role != "authority":
        raise ControlledValidationError("controlled validation requires an Authority config")
    try:
        raw = json.loads(config.install_manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ControlledValidationError("Authority install manifest is absent") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ControlledValidationError("Authority install manifest is unreadable") from error
    if not isinstance(raw, Mapping):
        raise ControlledValidationError("Authority install manifest must be an object")
    pnpm_runtime = raw.get("pnpm_recovery_runtime")
    if not isinstance(pnpm_runtime, Mapping):
        raise ControlledValidationError("Authority pnpm recovery runtime is absent")
    codex_value = raw.get("codex_binary")
    if codex_value is None and isinstance(raw.get("runtime"), Mapping):
        codex_value = raw["runtime"].get("codex_binary")
    codex = (
        config.state_root / "runtime" / "codex"
        if codex_value is None
        else _manifest_executable(codex_value, "Authority Codex binary")
    )
    return ValidationRuntime(
        codex_binary=codex,
        pnpm_binary=_manifest_executable(
            pnpm_runtime.get("binary"), "Authority pinned pnpm launcher"
        ),
        node_binary=_manifest_executable(
            pnpm_runtime.get("node_binary"), "Authority pinned Node executable"
        ),
    )


def plan_validation(
    worktree: Path,
    check_id: str,
    runtime: ValidationRuntime,
) -> ValidationPlan:
    """Build one static whitelist plan for a selected source worktree."""

    if check_id not in _CHECK_IDS:
        raise ControlledValidationError(f"unsupported controlled validation check: {check_id}")
    root = _worktree_root(worktree)
    commands = _commands_for(check_id, runtime)
    if check_id == _CHECK_IPC:
        for path, _title in _IPC_VITEST_CASES:
            _safe_regular_file(root, path)
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=(), write_paths=(), grants=(), preparation_directories=(),
            git_common_dir=None, allow_unix_socket=True,
        )
    bindings = _readme_bindings(root)
    if check_id == _CHECK_PAIRING:
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=bindings, write_paths=(), grants=(), preparation_directories=(),
            git_common_dir=None, allow_unix_socket=True,
        )
    common = _git_common_dir(root)
    write_paths, grants, preparation_directories = _pairing_write_grants(root, common, bindings)
    return _finish_plan(
        worktree=root, check_id=check_id, runtime=runtime, commands=commands,
        readme_bindings=bindings, write_paths=write_paths, grants=grants,
        preparation_directories=preparation_directories, git_common_dir=common,
        allow_unix_socket=True,
    )


def _manifest_executable(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ControlledValidationError(f"{label} is absent from the install manifest")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ControlledValidationError(f"{label} must be an absolute path")
    return candidate


def _runtime_executable(value: Path, label: str) -> Path:
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlledValidationError(f"{label} is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ControlledValidationError(f"{label} is not executable")
    return resolved


def _executable_identity(value: Path, label: str) -> ExecutableIdentity:
    try:
        metadata = value.stat()
    except OSError as error:
        raise ControlledValidationError(f"{label} cannot be statted") from error
    return ExecutableIdentity(
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_size=metadata.st_size,
        st_mtime_ns=metadata.st_mtime_ns,
    )


def _assert_runtime_current(runtime: ValidationRuntime) -> None:
    for field_name, identity_name in (
        ("codex_binary", "codex_identity"),
        ("pnpm_binary", "pnpm_identity"),
        ("node_binary", "node_identity"),
    ):
        path = getattr(runtime, field_name)
        expected = getattr(runtime, identity_name)
        label = f"validation runtime {field_name}"
        if _runtime_executable(path, label) != path:
            raise ControlledValidationError(f"{label} changed after validation preview")
        if _executable_identity(path, label) != expected:
            raise ControlledValidationError(f"{label} changed after validation preview")


def _worktree_root(worktree: Path) -> Path:
    if not isinstance(worktree, Path):
        raise ControlledValidationError("validation worktree must be a path")
    try:
        root = worktree.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlledValidationError("validation worktree is unavailable") from error
    if not root.is_dir():
        raise ControlledValidationError("validation worktree must be a directory")
    return root


def _commands_for(check_id: str, runtime: ValidationRuntime) -> tuple[ValidationCommand, ...]:
    pnpm = str(runtime.pnpm_binary)
    if check_id == _CHECK_IPC:
        return tuple(
            ValidationCommand(
                "dsh-b-ipc-v1",
                (
                    pnpm,
                    "exec",
                    "vitest",
                    "run",
                    path,
                    "-t",
                    title,
                    "--reporter=json",
                    f"--outputFile={_SCRATCH_PLACEHOLDER}/case-{index}.json",
                    "--no-cache", "--configLoader=runner",
                ),
                expected_test_title=title,
                report_file=f"{_SCRATCH_PLACEHOLDER}/case-{index}.json",
            )
            for index, (path, title) in enumerate(_IPC_VITEST_CASES, start=1)
        )
    check = (pnpm, "run", "verify-translation-pairing", *_README_ANCHORS)
    if check_id == _CHECK_PAIRING:
        return (ValidationCommand("dsh-b-pairing-check-v1", check),)
    if check_id == _CHECK_PAIRING_WRITE:
        return (
            ValidationCommand(
                "dsh-b-pairing-write-v1",
                (pnpm, "run", "verify-translation-pairing", "--write", *_README_ANCHORS),
            ),
            ValidationCommand("dsh-b-pairing-check-v1", check),
        )
    raise ControlledValidationError(f"unsupported controlled validation check: {check_id}")


def _finish_plan(
    *, worktree: Path, check_id: str, runtime: ValidationRuntime,
    commands: tuple[ValidationCommand, ...], readme_bindings: tuple[ReadmeBinding, ...],
    write_paths: tuple[Path, ...], grants: tuple[Path, ...],
    preparation_directories: tuple[Path, ...], git_common_dir: Path | None,
    allow_unix_socket: bool,
) -> ValidationPlan:
    partial = {
        "worktree": str(worktree), "check_id": check_id, "runtime": runtime.to_dict(),
        "commands": [command.to_dict() for command in commands],
        "readme_bindings": [binding.to_dict() for binding in readme_bindings],
        "write_paths": [str(path) for path in write_paths],
        "grants": [str(path) for path in grants],
        "preparation_directories": [str(path) for path in preparation_directories],
        "git_common_dir": str(git_common_dir) if git_common_dir else None,
        "sandbox": {"private_scratch": _SCRATCH_PLACEHOLDER,
                    "allow_unix_socket": allow_unix_socket, "network": False},
    }
    fingerprint = sha256(json.dumps(
        partial, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return ValidationPlan(
        worktree=worktree, check_id=check_id, runtime=runtime, commands=commands,
        readme_bindings=readme_bindings, write_paths=write_paths, grants=grants,
        preparation_directories=preparation_directories, git_common_dir=git_common_dir,
        allow_unix_socket=allow_unix_socket, fingerprint=fingerprint,
    )


def _readme_bindings(worktree: Path) -> tuple[ReadmeBinding, ...]:
    bindings: list[ReadmeBinding] = []
    for anchor in _README_ANCHORS:
        translated = anchor.removesuffix(".md") + ".zh.md"
        sidecar = anchor.removesuffix(".md") + ".i18n.yaml"
        _safe_regular_file(worktree, anchor)
        _safe_regular_file(worktree, translated)
        sidecar_path = _safe_regular_file(worktree, sidecar)
        bindings.append(ReadmeBinding(
            anchor=anchor, translated_path=translated, sidecar_path=sidecar,
            source_blob_sha1=_git_blob_id(worktree, anchor),
            translated_blob_sha1=_git_blob_id(worktree, translated),
            sidecar_sha256=sha256(sidecar_path.read_bytes()).hexdigest(),
        ))
    return tuple(bindings)


def _pairing_write_grants(
    worktree: Path, common: Path, bindings: tuple[ReadmeBinding, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    object_dirs: set[Path] = set()
    refs: set[Path] = set()
    locks: set[Path] = set()
    sidecars: set[Path] = set()
    snapshot_parent = common / "refs" / "dsh" / "translation-pairing" / "snapshots"
    for binding in bindings:
        sidecars.add(_safe_regular_file(worktree, binding.sidecar_path))
        for blob in (binding.source_blob_sha1, binding.translated_blob_sha1):
            object_dirs.add(common / "objects" / blob[:2])
            reference = snapshot_parent / blob
            refs.add(reference)
            locks.add(snapshot_parent / (blob + ".lock"))
    for path in (*object_dirs, *refs, *locks):
        _safe_grant_path(common, path)
    return (
        tuple(sorted((*sidecars, *refs, *locks), key=str)),
        tuple(sorted((*sidecars, *object_dirs, *refs, *locks), key=str)),
        tuple(sorted((*object_dirs, snapshot_parent), key=str)),
    )


def _git_common_dir(worktree: Path) -> Path:
    value = _git_text(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    try:
        common = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlledValidationError("Git common directory is unavailable") from error
    if not common.is_dir():
        raise ControlledValidationError("Git common directory is not a directory")
    return common


def _git_blob_id(worktree: Path, relative: str) -> str:
    value = _git_text(worktree, "hash-object", "--", relative)
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ControlledValidationError(f"Git blob identity is invalid for {relative}")
    return value


def _git_text(worktree: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [_GIT_BINARY, "-C", str(worktree), *arguments], capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlledValidationError("Git validation preflight could not run") from error
    if completed.returncode != 0:
        raise ControlledValidationError((completed.stderr or completed.stdout or "Git command failed").strip())
    return completed.stdout.strip()


def _safe_regular_file(root: Path, relative: str) -> Path:
    path = _safe_relative_path(root, relative)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ControlledValidationError(f"validation input is unavailable: {relative}") from error
    if path.is_symlink() or not path.is_file() or metadata.st_size < 0:
        raise ControlledValidationError(f"validation input must be a regular non-symlink file: {relative}")
    return path


def _safe_grant_path(root: Path, path: Path) -> None:
    try:
        root_path = root.resolve(strict=True)
        candidate = path.absolute()
        relative = candidate.relative_to(root_path)
    except (OSError, RuntimeError, ValueError) as error:
        raise ControlledValidationError("controlled validation grant escapes Git common directory") from error
    cursor = root_path
    for component in relative.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise ControlledValidationError("controlled validation grant cannot be inspected") from error
        if cursor.is_symlink():
            raise ControlledValidationError("controlled validation grant traverses a symlink")
        if cursor != candidate and not cursor.is_dir():
            raise ControlledValidationError("controlled validation grant has a non-directory parent")
        if metadata.st_size < 0:
            raise ControlledValidationError("controlled validation grant metadata is invalid")


def _path_is_within(root: Path, candidate: Path) -> bool:
    """Check lexical containment without resolving a potentially hostile leaf."""

    try:
        candidate.absolute().relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _safe_relative_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ControlledValidationError("validation path is invalid")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or str(parsed) != relative or ".." in parsed.parts:
        raise ControlledValidationError("validation path is invalid")
    root_path = root.resolve(strict=True)
    cursor = root_path
    for component in parsed.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise ControlledValidationError("validation path cannot be inspected") from error
        if cursor.is_symlink():
            raise ControlledValidationError("validation path traverses a symlink")
        if cursor != root_path / relative and not cursor.is_dir():
            raise ControlledValidationError("validation path has a non-directory parent")
        if metadata.st_size < 0:
            raise ControlledValidationError("validation path metadata is invalid")
    try:
        cursor.resolve(strict=False).relative_to(root_path)
    except (OSError, ValueError) as error:
        raise ControlledValidationError("validation path escapes the worktree") from error
    return cursor



def run_validation(
    plan: ValidationPlan,
    artifacts: ArtifactStore,
    timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
) -> ValidationResult:
    """Run an immutable validation plan in a fresh restricted Codex sandbox.

    Each child starts in a new session. On timeout this runner signals only
    that child process group; it never signals an inherited coordinator group.
    """

    _validate_timeout(timeout_seconds)
    started = time.monotonic()
    receipts: list[ValidationCommandReceipt] = []
    scratch: Path | None = None
    controlled_environment: dict[str, str] = {}
    preflight: tuple[dict[str, object], ...] = ()
    postflight: tuple[dict[str, object], ...] = ()
    error: str | None = None
    try:
        _assert_runtime_current(plan.runtime)
        _assert_immutable_plan(plan)
        preflight = _input_checks(plan, allow_sidecar_change=False)
        _prepare_grant_directories(plan)
        deadline = started + timeout_seconds
        if time.monotonic() >= deadline:
            raise ControlledValidationError("controlled validation reached its total timeout before execution")
        scratch = Path(tempfile.mkdtemp(prefix="wb-v.", dir=str(_PRIVATE_TEMP_ROOT))).resolve()
        _ensure_private_scratch(scratch)
        environment = _controlled_environment(scratch, plan.runtime)
        controlled_environment = _redact_scratch_environment(environment, scratch)
        for command in plan.commands:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                receipts.append(_deadline_receipt(command, artifacts))
                break
            receipt = _run_command(
                plan,
                command,
                scratch=scratch,
                environment=environment,
                artifacts=artifacts,
                deadline=deadline,
            )
            receipts.append(receipt)
            if receipt.exit_code != 0 or receipt.timed_out or receipt.error is not None:
                break
        if len(receipts) != len(plan.commands):
            for skipped in plan.commands[len(receipts):]:
                receipts.append(_skipped_receipt(skipped, artifacts))
        if time.monotonic() >= deadline:
            raise ControlledValidationError("controlled validation reached its total timeout")
        postflight = _input_checks(
            plan,
            allow_sidecar_change=plan.check_id == _CHECK_PAIRING_WRITE,
        )
    except (ControlledValidationError, OSError, subprocess.SubprocessError) as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        if scratch is not None:
            try:
                _cleanup_private_scratch(scratch)
            except ControlledValidationError as caught:
                error = error or f"{type(caught).__name__}: {caught}"
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    ok = (
        error is None
        and len(receipts) == len(plan.commands)
        and all(
            receipt.exit_code == 0
            and not receipt.timed_out
            and receipt.error is None
            for receipt in receipts
        )
        and bool(postflight or not plan.readme_bindings)
    )
    return ValidationResult(
        plan_fingerprint=plan.fingerprint,
        check_id=plan.check_id,
        ok=ok,
        duration_ms=duration_ms,
        commands=tuple(receipts),
        controlled_environment=controlled_environment,
        grants=tuple(str(path) for path in plan.grants),
        preflight_checks=preflight,
        postflight_checks=postflight,
        error=error,
    )


def _assert_immutable_plan(plan: ValidationPlan) -> None:
    if not isinstance(plan, ValidationPlan):
        raise ControlledValidationError("controlled validation plan is invalid")
    expected = plan_validation(plan.worktree, plan.check_id, plan.runtime)
    if expected.to_dict() != plan.to_dict():
        raise ControlledValidationError("controlled validation plan changed after preview")


def _input_checks(
    plan: ValidationPlan,
    *,
    allow_sidecar_change: bool,
) -> tuple[dict[str, object], ...]:
    checks: list[dict[str, object]] = []
    if plan.check_id == _CHECK_IPC:
        for path, _title in _IPC_VITEST_CASES:
            checked = _safe_regular_file(plan.worktree, path)
            checks.append({"kind": "ipc_test", "path": str(checked)})
    for binding in plan.readme_bindings:
        source = _safe_regular_file(plan.worktree, binding.anchor)
        translated = _safe_regular_file(plan.worktree, binding.translated_path)
        sidecar = _safe_regular_file(plan.worktree, binding.sidecar_path)
        source_blob = _git_blob_id(plan.worktree, binding.anchor)
        translated_blob = _git_blob_id(plan.worktree, binding.translated_path)
        sidecar_hash = sha256(sidecar.read_bytes()).hexdigest()
        if source_blob != binding.source_blob_sha1 or translated_blob != binding.translated_blob_sha1:
            raise ControlledValidationError("selected README source changed after preview")
        if not allow_sidecar_change and sidecar_hash != binding.sidecar_sha256:
            raise ControlledValidationError("selected README sidecar changed after preview")
        checks.append(
            {
                "anchor": binding.anchor,
                "source_blob_sha1": source_blob,
                "translated_blob_sha1": translated_blob,
                "sidecar_sha256": sidecar_hash,
                "sidecar_changed": sidecar_hash != binding.sidecar_sha256,
                "source_path": str(source),
                "translated_path": str(translated),
            }
        )
    if plan.git_common_dir is not None:
        sidecars = {
            _safe_regular_file(plan.worktree, binding.sidecar_path)
            for binding in plan.readme_bindings
        }
        for grant in plan.grants:
            if _path_is_within(plan.git_common_dir, grant):
                _safe_grant_path(plan.git_common_dir, grant)
                checks.append({"kind": "git_grant", "path": str(grant)})
            elif grant not in sidecars:
                raise ControlledValidationError("controlled validation has an unrecognized write grant")
    return tuple(checks)


def _prepare_grant_directories(plan: ValidationPlan) -> None:
    if plan.git_common_dir is None:
        return
    for directory in plan.preparation_directories:
        _safe_grant_path(plan.git_common_dir, directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_grant_path(plan.git_common_dir, directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ControlledValidationError("controlled validation grant directory is unsafe")


def _ensure_private_scratch(scratch: Path) -> None:
    scratch.chmod(0o700)
    if scratch.is_symlink() or not scratch.is_dir():
        raise ControlledValidationError("controlled validation scratch directory is unsafe")
    for child in ("home", "dsh", "npm-cache", "cache"):
        target = scratch / child
        target.mkdir(mode=0o700)
        target.chmod(0o700)


def _controlled_environment(scratch: Path, runtime: ValidationRuntime) -> dict[str, str]:
    return {
        "PATH": str(runtime.node_binary.parent) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "DSH_HOME": str(scratch / "dsh"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "npm_config_cache": str(scratch / "npm-cache"),
        "CI": "true",
        "NO_UPDATE_NOTIFIER": "1",
    }


def _redact_scratch_environment(environment: Mapping[str, str], scratch: Path) -> dict[str, str]:
    root = str(scratch)
    return {key: value.replace(root, _SCRATCH_PLACEHOLDER) for key, value in environment.items()}


def _sandbox_argv(
    plan: ValidationPlan,
    command: ValidationCommand,
    scratch: Path,
) -> list[str]:
    filesystem: dict[str, str] = {
        ":root": "read",
        ":tmpdir": "read",
        ":slash_tmp": "read",
        str(scratch): "write",
        **{str(path): "write" for path in plan.grants},
    }
    entries = ",".join(
        json.dumps(path) + "=" + json.dumps(access)
        for path, access in filesystem.items()
    )
    profile = (
        "permissions.wb-controlled-validation={filesystem={"
        + entries
        + "},network={enabled=false}}"
    )
    argv = [
        str(plan.runtime.codex_binary),
        "sandbox",
        "-P",
        "wb-controlled-validation",
        "-c",
        profile,
        "-C",
        str(plan.worktree),
    ]
    if plan.allow_unix_socket:
        argv.extend(("--allow-unix-socket", str(scratch)))
    argv.extend(
        ("--", *(part.replace(_SCRATCH_PLACEHOLDER, str(scratch)) for part in command.argv))
    )
    return argv


def _run_command(
    plan: ValidationPlan,
    command: ValidationCommand,
    *,
    scratch: Path,
    environment: dict[str, str],
    artifacts: ArtifactStore,
    deadline: float,
) -> ValidationCommandReceipt:
    started = time.monotonic()
    stdout = _BoundedCapture(_OUTPUT_LIMIT_BYTES)
    stderr = _BoundedCapture(_OUTPUT_LIMIT_BYTES)
    error: str | None = None
    timed_out = False
    exit_code: int | None = None
    test_assertion: dict[str, object] | None = None
    process: Any | None = None
    readers: tuple[threading.Thread, ...] = ()
    try:
        process = subprocess.Popen(
            _sandbox_argv(plan, command, scratch),
            cwd=str(plan.worktree),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        readers = _start_readers(process, stdout, stderr)
        try:
            exit_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_own_process_group(process)
            remaining_seconds = max(0.0, deadline - time.monotonic())
            try:
                exit_code = process.wait(timeout=remaining_seconds)
            except subprocess.TimeoutExpired:
                _kill_own_process_group(process)
                exit_code = process.wait(timeout=1)
        if error is None and exit_code == 0 and command.expected_test_title is not None:
            try:
                test_assertion = _vitest_assertion(scratch, command)
            except ControlledValidationError as caught:
                error = str(caught)
    except (OSError, subprocess.SubprocessError) as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        if process is not None:
            if exit_code is None:
                _terminate_own_process_group(process)
                try:
                    exit_code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _kill_own_process_group(process)
            if not _join_readers(readers, timeout_seconds=2):
                _terminate_own_process_group(process)
                _join_readers(readers, timeout_seconds=2)
            if any(reader.is_alive() for reader in readers):
                _kill_own_process_group(process)
                _join_readers(readers, timeout_seconds=2)
            _close_process_pipes(process)
            _join_readers(readers, timeout_seconds=1)
            if any(reader.is_alive() for reader in readers):
                error = error or "sandbox output reader did not finish"
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return ValidationCommandReceipt(
        purpose=command.purpose,
        argv=command.argv,
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        stdout_ref=artifacts.put_bytes(stdout.render(), "controlled-validation.stdout.log"),
        stderr_ref=artifacts.put_bytes(stderr.render(), "controlled-validation.stderr.log"),
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        error=error,
        test_assertion=test_assertion,
    )


def _start_readers(
    process: Any,
    stdout: "_BoundedCapture",
    stderr: "_BoundedCapture",
) -> tuple[threading.Thread, threading.Thread]:
    if process.stdout is None or process.stderr is None:
        raise ControlledValidationError("sandbox child did not expose output pipes")
    readers = (
        threading.Thread(target=stdout.read_from, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.read_from, args=(process.stderr,), daemon=True),
    )
    for reader in readers:
        reader.start()
    return readers


def _join_readers(readers: tuple[threading.Thread, ...], *, timeout_seconds: float) -> bool:
    for reader in readers:
        reader.join(timeout=timeout_seconds)
    return not any(reader.is_alive() for reader in readers)


def _close_process_pipes(process: Any) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            continue


def _terminate_own_process_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _kill_own_process_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _skipped_receipt(command: ValidationCommand, artifacts: ArtifactStore) -> ValidationCommandReceipt:
    message = "not run because an earlier controlled validation command failed\n"
    return ValidationCommandReceipt(
        purpose=command.purpose,
        argv=command.argv,
        exit_code=None,
        duration_ms=0,
        timed_out=False,
        stdout_ref=artifacts.put_text("", "controlled-validation.stdout.log"),
        stderr_ref=artifacts.put_text(message, "controlled-validation.stderr.log"),
        stdout_truncated=False,
        stderr_truncated=False,
        error="skipped after earlier command failure",
    )


def _deadline_receipt(command: ValidationCommand, artifacts: ArtifactStore) -> ValidationCommandReceipt:
    message = "not run because the controlled validation total timeout elapsed\n"
    return ValidationCommandReceipt(
        purpose=command.purpose,
        argv=command.argv,
        exit_code=None,
        duration_ms=0,
        timed_out=True,
        stdout_ref=artifacts.put_text("", "controlled-validation.stdout.log"),
        stderr_ref=artifacts.put_text(message, "controlled-validation.stderr.log"),
        stdout_truncated=False,
        stderr_truncated=False,
        error="controlled validation total timeout elapsed",
    )


def _vitest_assertion(scratch: Path, command: ValidationCommand) -> dict[str, object]:
    """Require the exact planned Vitest title to appear as a passed assertion."""

    if command.expected_test_title is None or command.report_file is None:
        raise ControlledValidationError("Vitest validation command has incomplete assertion metadata")
    report_name = command.report_file.replace(_SCRATCH_PLACEHOLDER + "/", "", 1)
    if not report_name or "/" in report_name or "\\" in report_name:
        raise ControlledValidationError("Vitest validation report path is invalid")
    report = scratch / report_name
    try:
        metadata = report.lstat()
        payload = report.read_text(encoding="utf-8")
    except OSError as caught:
        raise ControlledValidationError("Vitest JSON report is absent") from caught
    if report.is_symlink() or not report.is_file() or metadata.st_size < 0:
        raise ControlledValidationError("Vitest JSON report is unsafe")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as caught:
        raise ControlledValidationError("Vitest JSON report is invalid") from caught
    suites = value.get("testResults") if isinstance(value, Mapping) else None
    if not isinstance(suites, list):
        raise ControlledValidationError("Vitest JSON report has no test results")
    matches: list[str] = []
    for suite in suites:
        assertions = suite.get("assertionResults") if isinstance(suite, Mapping) else None
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                continue
            if assertion.get("title") == command.expected_test_title:
                status = assertion.get("status")
                matches.append(status if isinstance(status, str) else "<invalid>")
    if not matches:
        raise ControlledValidationError("Vitest JSON report did not match the planned test title")
    if any(status != "passed" for status in matches):
        raise ControlledValidationError(
            "Vitest JSON report did not pass the planned test title: " + ", ".join(matches)
        )
    return {
        "expected_title": command.expected_test_title,
        "matched_count": len(matches),
        "statuses": matches,
    }


def _validate_timeout(timeout_seconds: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise ControlledValidationError(
            f"controlled validation timeout must be an integer from 1 to {_MAX_TIMEOUT_SECONDS}"
        )


def _cleanup_private_scratch(scratch: Path) -> None:
    if (scratch.parent != _PRIVATE_TEMP_ROOT or not scratch.name.startswith("wb-v.")
            or scratch.is_symlink()):
        raise ControlledValidationError("controlled validation scratch escaped its private temporary root")
    shutil.rmtree(scratch)


class _BoundedCapture:
    """Drain a child pipe while retaining only a bounded audit payload."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def read_from(self, stream: Any) -> None:
        while True:
            try:
                chunk = stream.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode()
            self.total += len(chunk)
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def render(self) -> bytes:
        if not self.truncated:
            return bytes(self.data)
        return bytes(self.data) + (
            f"\n[controlled validation output truncated after {self.limit} bytes; "
            f"observed {self.total} bytes]\n"
        ).encode()
