"""Narrow, operator-authorized sandbox execution for three fixed DSH checks.

This module does not schedule work, inspect task state, or invoke a model. It
turns one approved validation identifier into exact argv and filesystem grants,
then records the bounded result of that argv in a fresh private directory.
"""

from __future__ import annotations

import base64
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
from .d_integration_metadata import (
    AgentNoteMetadata,
    DIntegrationMetadataError,
    DMetadataRequest,
    PairingMetadata,
    normalize_metadata_request,
    note_paths,
    pairing_paths,
)
from .d_integration_profile import (
    NOTE_WRITE_ID,
    PAIRING_WRITE_ID,
    REQUIRED_PACKAGE_MARKERS,
    SCOPE_PROFILE_ID,
)


_CHECK_IPC = "dsh-b-ipc-v1"
_CHECK_PAIRING = "dsh-b-pairing-check-v1"
_CHECK_PAIRING_WRITE = "dsh-b-pairing-write-v1"
_CHECK_D_PAIRING_WRITE = PAIRING_WRITE_ID
_CHECK_D_NOTE_WRITE = NOTE_WRITE_ID
_CHECK_IDS = frozenset({
    _CHECK_IPC,
    _CHECK_PAIRING,
    _CHECK_PAIRING_WRITE,
    _CHECK_D_PAIRING_WRITE,
    _CHECK_D_NOTE_WRITE,
})
_MAX_TIMEOUT_SECONDS = 60
_OUTPUT_LIMIT_BYTES = 128 * 1024
_SCRATCH_PLACEHOLDER = "<private-scratch>"
# macOS resolves /tmp to /private/tmp; Linux CI uses /tmp directly.
_PRIVATE_TEMP_ROOT = Path("/tmp").resolve()
_GIT_BINARY = "/usr/bin/git"
_NOTE_WRITE_PROGRAM_NAME = "d-agent-note-write.cjs"
_NOTE_WRITE_INPUT_NAME = "d-agent-note-write.json"
_NOTE_WRITE_PROGRAM = r"""
"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const O_NOFOLLOW = fs.constants.O_NOFOLLOW;

function fail(message) {
  throw new Error("D Agent Note writer: " + message);
}

function requireDirectoryChain(target) {
  const absolute = path.resolve(target);
  if (absolute !== target) fail("target path is not canonical");
  const parent = path.dirname(absolute);
  const root = path.parse(absolute).root;
  let cursor = root;
  for (const component of parent.slice(root.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, component);
    const stat = fs.lstatSync(cursor);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      fail("target parent is not a regular directory");
    }
  }
}

function readExisting(target) {
  let descriptor;
  try {
    descriptor = fs.openSync(target, fs.constants.O_RDONLY | O_NOFOLLOW);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile()) fail("existing target is not a regular file");
    return fs.readFileSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function currentTarget(target, expectedSha256) {
  requireDirectoryChain(target);
  const existing = readExisting(target);
  const actualSha256 = existing === null ? null : crypto.createHash("sha256").update(existing).digest("hex");
  if (actualSha256 !== expectedSha256) fail("target changed after preview");
  return existing;
}

function writeExact(target, expected, expectedSha256) {
  const existing = currentTarget(target, expectedSha256);
  if (existing !== null && existing.equals(expected)) return "unchanged";
  if (existing !== null) {
    const descriptor = fs.openSync(target, fs.constants.O_WRONLY | O_NOFOLLOW);
    try {
      if (!fs.fstatSync(descriptor).isFile()) fail("existing target is not a regular file");
      fs.ftruncateSync(descriptor, 0);
      fs.writeFileSync(descriptor, expected);
      fs.fsyncSync(descriptor);
    } finally {
      fs.closeSync(descriptor);
    }
    const written = readExisting(target);
    if (written === null || !written.equals(expected)) fail("updated target bytes changed");
    return "updated";
  }
  const descriptor = fs.openSync(
    target,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | O_NOFOLLOW,
    0o600,
  );
  try {
    fs.writeFileSync(descriptor, expected);
    fs.fsyncSync(descriptor);
    if (!fs.fstatSync(descriptor).isFile()) fail("created target is not a regular file");
  } finally {
    fs.closeSync(descriptor);
  }
  const written = readExisting(target);
  if (written === null || !written.equals(expected)) fail("created target bytes changed");
  return "created";
}

function decodeTarget(value) {
  if (!value || typeof value.path !== "string" || typeof value.contents_base64 !== "string"
      || !(value.expected_sha256 === null
        || (typeof value.expected_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.expected_sha256)))) {
    fail("input target is invalid");
  }
  const contents = Buffer.from(value.contents_base64, "base64");
  if (contents.toString("base64") !== value.contents_base64) fail("input contents are not canonical base64");
  return { path: value.path, contents, expectedSha256: value.expected_sha256 };
}

if (typeof O_NOFOLLOW !== "number") fail("O_NOFOLLOW is unavailable");
if (process.argv.length !== 3) fail("expected exactly one private JSON input path");
const document = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
if (!document || Object.keys(document).length !== 1 || !Array.isArray(document.targets) || document.targets.length !== 2) {
  fail("private JSON input is invalid");
}
const targets = document.targets.map(decodeTarget);
if (new Set(targets.map((target) => target.path)).size !== targets.length) fail("input targets are not distinct");
targets.forEach((target) => currentTarget(target.path, target.expectedSha256));
const results = targets.map((target) => ({
  path: target.path,
  result: writeExact(target.path, target.contents, target.expectedSha256),
}));
process.stdout.write(JSON.stringify({ ok: true, results }) + "\n");
""".lstrip()
_NOTE_WRITE_PROGRAM_SHA256 = sha256(_NOTE_WRITE_PROGRAM.encode()).hexdigest()

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
    entrypoint_sha256: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe command evidence."""

        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "expected_test_title": self.expected_test_title,
            "report_file": self.report_file,
            "entrypoint_sha256": dict(self.entrypoint_sha256),
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
class DProfileMarkerBinding:
    """One DSH package identity that makes the D profile source-specific."""

    path: str
    expected_package_name: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        """Return the source marker identity bound into a D plan."""

        return {
            "path": self.path,
            "expected_package_name": self.expected_package_name,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DPairingBinding:
    """One selected D English/Chinese pair and its possibly absent sidecar."""

    anchor: str
    translated_path: str
    sidecar_path: str
    source_blob_sha1: str
    translated_blob_sha1: str
    source_sha256: str
    translated_sha256: str
    sidecar_exists: bool
    sidecar_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        """Return exact selected input and sidecar identities without source text."""

        return {
            "anchor": self.anchor,
            "translated_path": self.translated_path,
            "sidecar_path": self.sidecar_path,
            "source_blob_sha1": self.source_blob_sha1,
            "translated_blob_sha1": self.translated_blob_sha1,
            "source_sha256": self.source_sha256,
            "translated_sha256": self.translated_sha256,
            "sidecar_exists": self.sidecar_exists,
            "sidecar_sha256": self.sidecar_sha256,
        }


@dataclass(frozen=True)
class DAgentNoteBinding:
    """The two exact Agent Note outputs and their preview-time existence."""

    anchor: str
    translated_path: str
    english_exists: bool
    chinese_exists: bool
    english_sha256: str | None
    chinese_sha256: str | None
    intended_english_sha256: str
    intended_chinese_sha256: str
    private_input_sha256: str

    def to_dict(self) -> dict[str, object]:
        """Return output existence and hashes without retaining note contents."""

        return {
            "anchor": self.anchor,
            "translated_path": self.translated_path,
            "english_exists": self.english_exists,
            "chinese_exists": self.chinese_exists,
            "english_sha256": self.english_sha256,
            "chinese_sha256": self.chinese_sha256,
            "intended_english_sha256": self.intended_english_sha256,
            "intended_chinese_sha256": self.intended_chinese_sha256,
            "private_input_sha256": self.private_input_sha256,
        }


@dataclass(frozen=True)
class DMetadataPlan:
    """D-only source bindings and opaque write data for one immutable plan."""

    request: DMetadataRequest
    profile_markers: tuple[DProfileMarkerBinding, ...]
    pairing_bindings: tuple[DPairingBinding, ...] = ()
    agent_note: DAgentNoteBinding | None = None
    note_program_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a journal-safe D metadata record bound by the plan fingerprint."""

        record: dict[str, object] = {
            "profile_id": SCOPE_PROFILE_ID,
            "request": self.request.to_dict(),
            "profile_markers": [marker.to_dict() for marker in self.profile_markers],
            "pairing_bindings": [binding.to_dict() for binding in self.pairing_bindings],
            "agent_note": self.agent_note.to_dict() if self.agent_note else None,
            "note_program_sha256": self.note_program_sha256,
        }
        return record


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
    metadata: DMetadataPlan | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the static JSON-safe plan used for preview and journaling."""

        result: dict[str, object] = {
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
        if self.metadata is not None:
            result["metadata"] = self.metadata.to_dict()
        return result


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
    *,
    metadata: DMetadataRequest | None = None,
) -> ValidationPlan:
    """Build one static whitelist plan for a selected source worktree."""

    if check_id not in _CHECK_IDS:
        raise ControlledValidationError(f"unsupported controlled validation check: {check_id}")
    root = _worktree_root(worktree)
    try:
        normalized_metadata = normalize_metadata_request(check_id, metadata)
    except DIntegrationMetadataError as error:
        raise ControlledValidationError(str(error)) from error
    d_metadata = _d_metadata_plan(root, normalized_metadata) if normalized_metadata else None
    commands = _commands_for(root, check_id, runtime, d_metadata)
    if check_id == _CHECK_IPC:
        for path, _title in _IPC_VITEST_CASES:
            _safe_regular_file(root, path)
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=(), write_paths=(), grants=(), preparation_directories=(),
            git_common_dir=None, allow_unix_socket=True, metadata=None,
        )
    if check_id == _CHECK_PAIRING:
        bindings = _readme_bindings(root)
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=bindings, write_paths=(), grants=(), preparation_directories=(),
            git_common_dir=None, allow_unix_socket=True, metadata=None,
        )
    if check_id == _CHECK_PAIRING_WRITE:
        bindings = _readme_bindings(root)
        common = _git_common_dir(root)
        write_paths, grants, preparation_directories = _pairing_write_grants(root, common, bindings)
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=bindings, write_paths=write_paths, grants=grants,
            preparation_directories=preparation_directories, git_common_dir=common,
            allow_unix_socket=True, metadata=None,
        )
    if d_metadata is None:
        raise ControlledValidationError("D validation metadata is unavailable")
    if check_id == _CHECK_D_PAIRING_WRITE:
        common = _git_common_dir(root)
        write_paths, grants, preparation_directories = _d_pairing_write_grants(
            root, common, d_metadata.pairing_bindings
        )
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=(), write_paths=write_paths, grants=grants,
            preparation_directories=preparation_directories, git_common_dir=common,
            allow_unix_socket=False, metadata=d_metadata,
        )
    if check_id == _CHECK_D_NOTE_WRITE:
        if d_metadata.agent_note is None:
            raise ControlledValidationError("D Agent Note plan has no note binding")
        write_paths = tuple(
            _safe_relative_path(root, path)
            for path in (d_metadata.agent_note.anchor, d_metadata.agent_note.translated_path)
        )
        return _finish_plan(
            worktree=root, check_id=check_id, runtime=runtime, commands=commands,
            readme_bindings=(), write_paths=write_paths, grants=write_paths,
            preparation_directories=(), git_common_dir=None, allow_unix_socket=False,
            metadata=d_metadata,
        )
    raise ControlledValidationError(f"unsupported controlled validation check: {check_id}")


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


def _installed_entrypoint(worktree: Path, relative: str) -> tuple[Path, tuple[str, str]]:
    """Bind a fixed installed JS entry, including pnpm dependency symlinks."""
    try:
        path = (worktree / relative).resolve(strict=True)
        if not path.is_file():
            raise ControlledValidationError(f"validation entrypoint is not a file: {relative}")
        digest = sha256(path.read_bytes()).hexdigest()
    except (OSError, RuntimeError) as error:
        raise ControlledValidationError(f"validation entrypoint is unavailable: {relative}") from error
    return path, (str(path), digest)


def _commands_for(
    worktree: Path,
    check_id: str,
    runtime: ValidationRuntime,
    metadata: DMetadataPlan | None,
) -> tuple[ValidationCommand, ...]:
    node = str(runtime.node_binary)
    if check_id == _CHECK_IPC:
        vitest, vitest_identity = _installed_entrypoint(worktree, "node_modules/vitest/vitest.mjs")
        return tuple(
            ValidationCommand(
                "dsh-b-ipc-v1",
                (
                    node,
                    str(vitest),
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
                entrypoint_sha256=(vitest_identity,),
            )
            for index, (path, title) in enumerate(_IPC_VITEST_CASES, start=1)
        )
    if check_id == _CHECK_D_NOTE_WRITE:
        if metadata is None or metadata.agent_note is None or metadata.note_program_sha256 is None:
            raise ControlledValidationError("D Agent Note command has no immutable program binding")
        return (
            ValidationCommand(
                _CHECK_D_NOTE_WRITE,
                (
                    node,
                    f"{_SCRATCH_PLACEHOLDER}/{_NOTE_WRITE_PROGRAM_NAME}",
                    f"{_SCRATCH_PLACEHOLDER}/{_NOTE_WRITE_INPUT_NAME}",
                ),
                entrypoint_sha256=((
                    f"{_SCRATCH_PLACEHOLDER}/{_NOTE_WRITE_PROGRAM_NAME}",
                    metadata.note_program_sha256,
                ),),
            ),
        )
    loader, loader_identity = _installed_entrypoint(worktree, "node_modules/tsx/dist/esm/index.mjs")
    script = _safe_regular_file(worktree, "scripts/verify-translation-pairing.ts")
    script_identity = (str(script), sha256(script.read_bytes()).hexdigest())
    entrypoints = (loader_identity, script_identity)
    prefix = (node, "--import", loader.as_uri(), str(script))
    check = (*prefix, *_README_ANCHORS)
    if check_id == _CHECK_PAIRING:
        return (ValidationCommand("dsh-b-pairing-check-v1", check, entrypoint_sha256=entrypoints),)
    if check_id == _CHECK_PAIRING_WRITE:
        return (
            ValidationCommand(
                "dsh-b-pairing-write-v1",
                (*prefix, "--write", *_README_ANCHORS),
                entrypoint_sha256=entrypoints,
            ),
            ValidationCommand("dsh-b-pairing-check-v1", check, entrypoint_sha256=entrypoints),
        )
    if check_id == _CHECK_D_PAIRING_WRITE:
        if metadata is None or not metadata.pairing_bindings:
            raise ControlledValidationError("D pairing command has no selected bindings")
        anchors = tuple(binding.anchor for binding in metadata.pairing_bindings)
        return (
            ValidationCommand(
                _CHECK_D_PAIRING_WRITE,
                (*prefix, "--write", *anchors),
                entrypoint_sha256=entrypoints,
            ),
            ValidationCommand(
                "dsh-d-pairing-check-v1",
                (*prefix, *anchors),
                entrypoint_sha256=entrypoints,
            ),
        )
    raise ControlledValidationError(f"unsupported controlled validation check: {check_id}")


def _finish_plan(
    *, worktree: Path, check_id: str, runtime: ValidationRuntime,
    commands: tuple[ValidationCommand, ...], readme_bindings: tuple[ReadmeBinding, ...],
    write_paths: tuple[Path, ...], grants: tuple[Path, ...],
    preparation_directories: tuple[Path, ...], git_common_dir: Path | None,
    allow_unix_socket: bool, metadata: DMetadataPlan | None,
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
    if metadata is not None:
        partial["metadata"] = metadata.to_dict()
    fingerprint = sha256(json.dumps(
        partial, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return ValidationPlan(
        worktree=worktree, check_id=check_id, runtime=runtime, commands=commands,
        readme_bindings=readme_bindings, write_paths=write_paths, grants=grants,
        preparation_directories=preparation_directories, git_common_dir=git_common_dir,
        allow_unix_socket=allow_unix_socket, fingerprint=fingerprint, metadata=metadata,
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


def _d_metadata_plan(worktree: Path, request: DMetadataRequest) -> DMetadataPlan:
    """Bind D-only source markers plus selected inputs before any child starts."""

    markers = _d_profile_markers(worktree)
    if isinstance(request, PairingMetadata):
        return DMetadataPlan(
            request=request,
            profile_markers=markers,
            pairing_bindings=_d_pairing_bindings(worktree, request),
        )
    if isinstance(request, AgentNoteMetadata):
        return DMetadataPlan(
            request=request,
            profile_markers=markers,
            agent_note=_d_agent_note_binding(worktree, request),
            note_program_sha256=_NOTE_WRITE_PROGRAM_SHA256,
        )
    raise ControlledValidationError("D validation metadata has an unsupported request type")


def _d_profile_markers(worktree: Path) -> tuple[DProfileMarkerBinding, ...]:
    """Require the exact DSH packages that identify this closed D profile."""

    markers: list[DProfileMarkerBinding] = []
    for relative, package_name in sorted(REQUIRED_PACKAGE_MARKERS.items()):
        _path, payload = _safe_regular_bytes(worktree, relative)
        try:
            package = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlledValidationError(f"D profile package marker is invalid: {relative}") from error
        if not isinstance(package, Mapping) or package.get("name") != package_name:
            raise ControlledValidationError(f"D profile package marker does not match: {relative}")
        markers.append(DProfileMarkerBinding(
            path=relative,
            expected_package_name=package_name,
            sha256=sha256(payload).hexdigest(),
        ))
    return tuple(markers)


def _d_pairing_bindings(
    worktree: Path,
    request: PairingMetadata,
) -> tuple[DPairingBinding, ...]:
    """Bind all explicitly selected D pairs, including absent sidecars."""

    bindings: list[DPairingBinding] = []
    for anchor in request.anchors:
        try:
            source_relative, translated_relative, sidecar_relative = pairing_paths(anchor)
        except DIntegrationMetadataError as error:
            raise ControlledValidationError(str(error)) from error
        _source_path, source = _safe_regular_bytes(worktree, source_relative)
        _translated_path, translated = _safe_regular_bytes(worktree, translated_relative)
        _sidecar_path, sidecar_exists, sidecar = _safe_optional_regular_bytes(
            worktree, sidecar_relative
        )
        bindings.append(DPairingBinding(
            anchor=source_relative,
            translated_path=translated_relative,
            sidecar_path=sidecar_relative,
            source_blob_sha1=_git_blob_id(worktree, source_relative),
            translated_blob_sha1=_git_blob_id(worktree, translated_relative),
            source_sha256=sha256(source).hexdigest(),
            translated_sha256=sha256(translated).hexdigest(),
            sidecar_exists=sidecar_exists,
            sidecar_sha256=sha256(sidecar).hexdigest() if sidecar is not None else None,
        ))
    return tuple(bindings)


def _d_agent_note_binding(worktree: Path, request: AgentNoteMetadata) -> DAgentNoteBinding:
    """Bind absent or explicitly hash-matched Agent Note leaves before writing."""

    try:
        english_relative, chinese_relative = note_paths(request.anchor)
    except DIntegrationMetadataError as error:
        raise ControlledValidationError(str(error)) from error
    english_path, english_exists, english = _safe_creation_leaf(worktree, english_relative)
    chinese_path, chinese_exists, chinese = _safe_creation_leaf(worktree, chinese_relative)
    english_sha256 = sha256(english).hexdigest() if english is not None else None
    chinese_sha256 = sha256(chinese).hexdigest() if chinese is not None else None
    if request.expected_english_sha256 is None:
        if english is not None and english != request.english:
            raise ControlledValidationError("D Agent Note English target already has different bytes")
        if chinese is not None and chinese != request.chinese:
            raise ControlledValidationError("D Agent Note Chinese target already has different bytes")
    else:
        if english_sha256 != request.expected_english_sha256:
            raise ControlledValidationError("D Agent Note English target does not match expected current SHA-256")
        if chinese_sha256 != request.expected_chinese_sha256:
            raise ControlledValidationError("D Agent Note Chinese target does not match expected current SHA-256")
    private_input = _note_private_input_bytes(
        english_path, chinese_path, request.english, request.chinese,
        english_sha256, chinese_sha256,
    )
    return DAgentNoteBinding(
        anchor=english_relative,
        translated_path=chinese_relative,
        english_exists=english_exists,
        chinese_exists=chinese_exists,
        english_sha256=english_sha256,
        chinese_sha256=chinese_sha256,
        intended_english_sha256=sha256(request.english).hexdigest(),
        intended_chinese_sha256=sha256(request.chinese).hexdigest(),
        private_input_sha256=sha256(private_input).hexdigest(),
    )


def _safe_regular_bytes(root: Path, relative: str) -> tuple[Path, bytes]:
    path = _safe_regular_file(root, relative)
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise ControlledValidationError(f"validation input cannot be read: {relative}") from error


def _safe_optional_regular_bytes(root: Path, relative: str) -> tuple[Path, bool, bytes | None]:
    """Read an existing sidecar or bind its absence without creating it."""

    path = _safe_relative_path(root, relative)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return path, False, None
    except OSError as error:
        raise ControlledValidationError(f"validation input cannot be inspected: {relative}") from error
    if path.is_symlink() or not path.is_file() or metadata.st_size < 0:
        raise ControlledValidationError(
            f"validation input must be a regular non-symlink file when present: {relative}"
        )
    try:
        return path, True, path.read_bytes()
    except OSError as error:
        raise ControlledValidationError(f"validation input cannot be read: {relative}") from error


def _safe_creation_leaf(root: Path, relative: str) -> tuple[Path, bool, bytes | None]:
    """Require pre-existing regular parents while allowing exactly one absent leaf."""

    path = _safe_relative_path(root, relative)
    root_path = root.resolve(strict=True)
    cursor = root_path
    for component in PurePosixPath(relative).parts[:-1]:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError as error:
            raise ControlledValidationError(
                f"D Agent Note parent directory is absent: {relative}"
            ) from error
        except OSError as error:
            raise ControlledValidationError(
                f"D Agent Note parent directory cannot be inspected: {relative}"
            ) from error
        if cursor.is_symlink() or not cursor.is_dir() or metadata.st_size < 0:
            raise ControlledValidationError(
                f"D Agent Note parent must be a regular non-symlink directory: {relative}"
            )
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return path, False, None
    except OSError as error:
        raise ControlledValidationError(f"D Agent Note target cannot be inspected: {relative}") from error
    if path.is_symlink() or not path.is_file() or metadata.st_size < 0:
        raise ControlledValidationError(
            f"D Agent Note target must be a regular non-symlink file when present: {relative}"
        )
    try:
        return path, True, path.read_bytes()
    except OSError as error:
        raise ControlledValidationError(f"D Agent Note target cannot be read: {relative}") from error


def _note_private_input_bytes(
    english_path: Path,
    chinese_path: Path,
    english: bytes,
    chinese: bytes,
    expected_english_sha256: str | None,
    expected_chinese_sha256: str | None,
) -> bytes:
    """Serialize only the two exact note writes for the private sandbox input."""

    return json.dumps(
        {
            "targets": [
                {
                    "path": str(english_path),
                    "contents_base64": base64.b64encode(english).decode("ascii"),
                    "expected_sha256": expected_english_sha256,
                },
                {
                    "path": str(chinese_path),
                    "contents_base64": base64.b64encode(chinese).decode("ascii"),
                    "expected_sha256": expected_chinese_sha256,
                },
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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


def _d_pairing_write_grants(
    worktree: Path,
    common: Path,
    bindings: tuple[DPairingBinding, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    """Grant only the selected D sidecars and content-addressed snapshot leaves."""

    object_dirs: set[Path] = set()
    refs: set[Path] = set()
    locks: set[Path] = set()
    sidecars: set[Path] = set()
    snapshot_parent = common / "refs" / "dsh" / "translation-pairing" / "snapshots"
    for binding in bindings:
        sidecars.add(_safe_relative_path(worktree, binding.sidecar_path))
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
        _prepare_private_metadata_input(plan, scratch)
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
            allow_sidecar_change=plan.check_id in {
                _CHECK_PAIRING_WRITE,
                _CHECK_D_PAIRING_WRITE,
                _CHECK_D_NOTE_WRITE,
            },
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
        and bool(postflight or not (plan.readme_bindings or plan.metadata is not None))
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
    metadata = plan.metadata.request if plan.metadata is not None else None
    expected = plan_validation(plan.worktree, plan.check_id, plan.runtime, metadata=metadata)
    if expected.to_dict() != plan.to_dict():
        raise ControlledValidationError("controlled validation plan changed after preview")


def _input_checks(
    plan: ValidationPlan,
    *,
    allow_sidecar_change: bool,
) -> tuple[dict[str, object], ...]:
    checks: list[dict[str, object]] = []
    metadata_sidecars: set[Path] = set()
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
    if plan.metadata is not None:
        metadata_checks, metadata_sidecars = _metadata_input_checks(
            plan, allow_sidecar_change=allow_sidecar_change
        )
        checks.extend(metadata_checks)
    if plan.git_common_dir is not None:
        sidecars = {
            _safe_regular_file(plan.worktree, binding.sidecar_path)
            for binding in plan.readme_bindings
        }
        sidecars.update(metadata_sidecars)
        for grant in plan.grants:
            if _path_is_within(plan.git_common_dir, grant):
                _safe_grant_path(plan.git_common_dir, grant)
                checks.append({"kind": "git_grant", "path": str(grant)})
            elif grant not in sidecars:
                raise ControlledValidationError("controlled validation has an unrecognized write grant")
    return tuple(checks)


def _metadata_input_checks(
    plan: ValidationPlan,
    *,
    allow_sidecar_change: bool,
) -> tuple[tuple[dict[str, object], ...], set[Path]]:
    """Recheck D profile inputs without exposing private Agent Note contents."""

    metadata = plan.metadata
    if metadata is None:
        raise ControlledValidationError("D metadata checks require a D metadata plan")
    if _d_profile_markers(plan.worktree) != metadata.profile_markers:
        raise ControlledValidationError("D profile package markers changed after preview")
    checks: list[dict[str, object]] = [
        {"kind": "d_profile_marker", **marker.to_dict()}
        for marker in metadata.profile_markers
    ]
    sidecars: set[Path] = set()
    if isinstance(metadata.request, PairingMetadata):
        if not metadata.pairing_bindings or metadata.agent_note is not None:
            raise ControlledValidationError("D pairing plan metadata is invalid")
        for binding in metadata.pairing_bindings:
            source_path, source = _safe_regular_bytes(plan.worktree, binding.anchor)
            translated_path, translated = _safe_regular_bytes(plan.worktree, binding.translated_path)
            source_blob = _git_blob_id(plan.worktree, binding.anchor)
            translated_blob = _git_blob_id(plan.worktree, binding.translated_path)
            if (
                source_blob != binding.source_blob_sha1
                or translated_blob != binding.translated_blob_sha1
                or sha256(source).hexdigest() != binding.source_sha256
                or sha256(translated).hexdigest() != binding.translated_sha256
            ):
                raise ControlledValidationError("selected D pairing source changed after preview")
            sidecar_path, sidecar_exists, sidecar = _safe_optional_regular_bytes(
                plan.worktree, binding.sidecar_path
            )
            sidecars.add(sidecar_path)
            sidecar_sha256 = sha256(sidecar).hexdigest() if sidecar is not None else None
            if not allow_sidecar_change and (
                sidecar_exists != binding.sidecar_exists
                or sidecar_sha256 != binding.sidecar_sha256
            ):
                raise ControlledValidationError("selected D pairing sidecar changed after preview")
            if allow_sidecar_change and not sidecar_exists:
                raise ControlledValidationError("D pairing write did not create its selected sidecar")
            checks.append({
                "kind": "d_pairing",
                "anchor": binding.anchor,
                "source_blob_sha1": source_blob,
                "translated_blob_sha1": translated_blob,
                "source_path": str(source_path),
                "translated_path": str(translated_path),
                "sidecar_path": str(sidecar_path),
                "sidecar_exists": sidecar_exists,
                "sidecar_changed": sidecar_sha256 != binding.sidecar_sha256,
            })
        return tuple(checks), sidecars
    if not isinstance(metadata.request, AgentNoteMetadata) or metadata.agent_note is None:
        raise ControlledValidationError("D Agent Note plan metadata is invalid")
    if metadata.pairing_bindings or metadata.note_program_sha256 != _NOTE_WRITE_PROGRAM_SHA256:
        raise ControlledValidationError("D Agent Note program binding changed after preview")
    note = metadata.agent_note
    english_path, english_exists, english = _safe_creation_leaf(plan.worktree, note.anchor)
    chinese_path, chinese_exists, chinese = _safe_creation_leaf(plan.worktree, note.translated_path)
    if not allow_sidecar_change and (
        english_exists != note.english_exists
        or chinese_exists != note.chinese_exists
        or (sha256(english).hexdigest() if english is not None else None) != note.english_sha256
        or (sha256(chinese).hexdigest() if chinese is not None else None) != note.chinese_sha256
    ):
        raise ControlledValidationError("D Agent Note target changed after preview")
    if allow_sidecar_change and (
        english is None
        or chinese is None
        or english != metadata.request.english
        or chinese != metadata.request.chinese
    ):
        raise ControlledValidationError("D Agent Note writer did not leave the exact planned bytes")
    private_input = _note_private_input_bytes(
        english_path, chinese_path, metadata.request.english, metadata.request.chinese,
        note.english_sha256, note.chinese_sha256,
    )
    if sha256(private_input).hexdigest() != note.private_input_sha256:
        raise ControlledValidationError("D Agent Note private input changed after preview")
    checks.append({
        "kind": "d_agent_note",
        "anchor": note.anchor,
        "translated_path": note.translated_path,
        "english_exists": english_exists,
        "chinese_exists": chinese_exists,
        "english_sha256": sha256(english).hexdigest() if english is not None else None,
        "chinese_sha256": sha256(chinese).hexdigest() if chinese is not None else None,
        "private_input_sha256": note.private_input_sha256,
    })
    return tuple(checks), sidecars


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


def _prepare_private_metadata_input(plan: ValidationPlan, scratch: Path) -> None:
    """Materialize only the fixed D Agent Note writer and its private JSON input."""

    metadata = plan.metadata
    if metadata is None or not isinstance(metadata.request, AgentNoteMetadata):
        return
    if metadata.agent_note is None or metadata.note_program_sha256 != _NOTE_WRITE_PROGRAM_SHA256:
        raise ControlledValidationError("D Agent Note writer program is not bound to this plan")
    note = metadata.agent_note
    english_path, _english_exists, _english = _safe_creation_leaf(plan.worktree, note.anchor)
    chinese_path, _chinese_exists, _chinese = _safe_creation_leaf(plan.worktree, note.translated_path)
    private_input = _note_private_input_bytes(
        english_path, chinese_path, metadata.request.english, metadata.request.chinese,
        note.english_sha256, note.chinese_sha256,
    )
    if sha256(private_input).hexdigest() != note.private_input_sha256:
        raise ControlledValidationError("D Agent Note private input is not bound to this plan")
    _write_private_scratch_file(scratch, _NOTE_WRITE_PROGRAM_NAME, _NOTE_WRITE_PROGRAM.encode())
    _write_private_scratch_file(scratch, _NOTE_WRITE_INPUT_NAME, private_input)


def _write_private_scratch_file(scratch: Path, name: str, payload: bytes) -> None:
    """Create one immutable regular scratch input without following a leaf symlink."""

    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        raise ControlledValidationError("controlled validation scratch filename is invalid")
    try:
        no_follow = os.O_NOFOLLOW
    except AttributeError as error:
        raise ControlledValidationError("controlled validation requires O_NOFOLLOW") from error
    target = scratch / name
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
            0o600,
        )
    except OSError as error:
        raise ControlledValidationError("controlled validation private scratch input cannot be created") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ControlledValidationError("controlled validation private scratch input cannot be written") from error
    try:
        metadata = target.lstat()
        actual = target.read_bytes()
    except OSError as error:
        raise ControlledValidationError("controlled validation private scratch input cannot be verified") from error
    if target.is_symlink() or not target.is_file() or metadata.st_size != len(payload) or actual != payload:
        raise ControlledValidationError("controlled validation private scratch input is unsafe")


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
