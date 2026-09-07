"""Bounded, non-model execution-environment readiness checks.

This module deliberately does not invoke an executor, authenticate a provider,
materialize dependencies, or run a project lifecycle command.  It answers the
smaller question needed before a costly worker turn: whether the *allocated
worktree* has the explicitly required local tools, dependencies, and source
resolution.  Every negative result is an ``environment`` failure, never a
statement about model quality.

The pnpm checks mirror the recovery contract in ``dirty_worktree_recovery``:
an applicable project has both ``package.json`` and ``pnpm-lock.yaml``, a
``packageManager=pnpm@...`` declaration, a matching major version, and (for
pnpm 11) a managed runtime of at least 11.25.0.  Readiness only observes the
worktree-local linker; recovery remains the component allowed to materialize
one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Literal, Mapping


ReadinessCheckStatus = Literal["passed", "failed", "not-applicable"]
DependencyKind = Literal["file", "directory", "any"]
PnpmMode = Literal["auto", "required", "disabled"]

# Keep this value aligned with PnpmOfflineMaterializer.MINIMUM_PNPM_11_VERSION.
PNPM_MINIMUM_11_VERSION = (11, 25, 0)
MAX_REQUIREMENTS = 64
MAX_LINKER_WALK_DIRECTORIES = 10_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_PROBE_OUTPUT_BYTES = 8_192
MAX_PROBE_TIMEOUT_SECONDS = 15
MAX_TOTAL_TIMEOUT_SECONDS = 60
_SAFE_VERSION_ARGUMENTS = frozenset({"--version", "-V"})


@dataclass(frozen=True)
class ReadinessFailure:
    """An actionable environment-only reason that a worker must not start."""

    check_id: str
    code: str
    message: str
    remediation: str
    origin: Literal["environment"] = "environment"

    def to_dict(self) -> dict[str, str]:
        return {
            "origin": self.origin,
            "check_id": self.check_id,
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class ReadinessCheck:
    """One bounded observation made against the target worktree."""

    check_id: str
    kind: str
    status: ReadinessCheckStatus
    detail: Mapping[str, object]
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.check_id,
            "kind": self.kind,
            "status": self.status,
            "detail": _json_safe(self.detail),
        }
        if self.failure_code is not None:
            payload["failure_code"] = self.failure_code
        return payload


@dataclass(frozen=True)
class ExecutionReadinessReport:
    """Serializable evidence for one exact worktree readiness decision."""

    worktree: str
    ready: bool
    checks: tuple[ReadinessCheck, ...]
    failures: tuple[ReadinessFailure, ...]
    elapsed_ms: int
    schema_version: int = 1

    @property
    def failure_origin(self) -> Literal["environment"] | None:
        """All failures from this contract are environmental by construction."""

        return "environment" if self.failures else None

    @property
    def summary(self) -> str:
        if self.ready:
            return "execution environment is ready"
        return f"execution environment is not ready: {self.failures[0].message}"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "worktree": self.worktree,
            "ready": self.ready,
            "failure_origin": self.failure_origin,
            "summary": self.summary,
            "elapsed_ms": self.elapsed_ms,
            "checks": [check.to_dict() for check in self.checks],
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True)
class ToolRequirement:
    """A required executable whose only permitted probe is a version query."""

    name: str
    executable: str
    version_argument: str = "--version"
    required: bool = True
    probe_version: bool = True

    def __post_init__(self) -> None:
        _require_nonempty(self.name, "tool name")
        _require_nonempty(self.executable, "tool executable")
        if self.version_argument not in _SAFE_VERSION_ARGUMENTS:
            raise ValueError(
                "tool version_argument must be one of "
                f"{sorted(_SAFE_VERSION_ARGUMENTS)!r}; readiness probes cannot run arbitrary commands"
            )


@dataclass(frozen=True)
class PathDependencyRequirement:
    """A file or directory that must resolve inside the allocated worktree."""

    name: str
    relative_path: str
    kind: DependencyKind = "any"
    required: bool = True
    must_resolve_within_worktree: bool = True

    def __post_init__(self) -> None:
        _require_nonempty(self.name, "dependency name")
        _validated_relative_path(self.relative_path, "dependency relative_path")
        if self.kind not in {"file", "directory", "any"}:
            raise ValueError(f"unsupported dependency kind: {self.kind!r}")


@dataclass(frozen=True)
class SourceResolutionRequirement:
    """Resolve a local Python package without importing its application code.

    ``source_roots`` are relative to the target worktree, never the
    coordinator process's source tree.  The probe uses ``PathFinder`` under
    ``python -I -S`` so project startup hooks and package ``__init__`` code are
    not executed merely to establish readiness.
    """

    module: str
    source_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.module, "source module")
        if not all(part.isidentifier() for part in self.module.split(".")):
            raise ValueError("source module must be a dotted Python identifier")
        if not self.source_roots:
            raise ValueError("source_roots must name at least one target-worktree root")
        for root in self.source_roots:
            _validated_relative_path(root, "source root")


@dataclass(frozen=True)
class PnpmRequirement:
    """The non-mutating pnpm readiness policy for a target worktree."""

    mode: PnpmMode = "auto"
    binary: str | None = None
    require_linker: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "required", "disabled"}:
            raise ValueError(f"unsupported pnpm readiness mode: {self.mode!r}")
        if self.binary is not None:
            _require_nonempty(self.binary, "pnpm binary")


# A descriptive long form is useful to callers that keep requirements grouped
# by subsystem, while PnpmRequirement remains the concise public spelling.
PnpmReadinessRequirement = PnpmRequirement


@dataclass(frozen=True)
class ExecutionReadinessRequest:
    """Explicit inputs for a bounded readiness assessment."""

    worktree: Path | str
    tools: tuple[ToolRequirement, ...] = ()
    dependencies: tuple[PathDependencyRequirement, ...] = ()
    source_resolutions: tuple[SourceResolutionRequirement, ...] = ()
    pnpm: PnpmRequirement | None = field(default_factory=PnpmRequirement)
    python_executable: str = sys.executable
    probe_timeout_seconds: int = 10
    total_timeout_seconds: int = 30
    max_linker_walk_directories: int = MAX_LINKER_WALK_DIRECTORIES

    def __post_init__(self) -> None:
        total_requirements = len(self.tools) + len(self.dependencies) + len(self.source_resolutions)
        if total_requirements > MAX_REQUIREMENTS:
            raise ValueError(f"at most {MAX_REQUIREMENTS} explicit readiness requirements are allowed")
        for field_name, value in (
            ("probe_timeout_seconds", self.probe_timeout_seconds),
            ("total_timeout_seconds", self.total_timeout_seconds),
            ("max_linker_walk_directories", self.max_linker_walk_directories),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.probe_timeout_seconds > MAX_PROBE_TIMEOUT_SECONDS:
            raise ValueError(
                f"probe_timeout_seconds cannot exceed {MAX_PROBE_TIMEOUT_SECONDS}"
            )
        if self.total_timeout_seconds > MAX_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(
                f"total_timeout_seconds cannot exceed {MAX_TOTAL_TIMEOUT_SECONDS}"
            )
        if self.max_linker_walk_directories > MAX_LINKER_WALK_DIRECTORIES:
            raise ValueError(
                "max_linker_walk_directories cannot exceed "
                f"{MAX_LINKER_WALK_DIRECTORIES}"
            )
        if self.probe_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("probe_timeout_seconds cannot exceed total_timeout_seconds")
        _require_nonempty(self.python_executable, "python executable")


@dataclass(frozen=True)
class _ProbeOutcome:
    command: tuple[str, ...]
    timeout_seconds: int
    exit_code: int | None
    stdout: str
    stderr: str
    error: str | None = None
    timed_out: bool = False


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableResolver = Callable[[str], str | None]
Clock = Callable[[], float]


class ExecutionReadinessChecker:
    """Assess an exact worktree without starting a model or mutating it."""

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        which: ExecutableResolver = shutil.which,
        environment: Mapping[str, str] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._runner = runner
        self._which = which
        self._environment = dict(os.environ if environment is None else environment)
        self._clock = clock

    def assess(self, request: ExecutionReadinessRequest) -> ExecutionReadinessReport:
        """Return structured evidence; never raise for an observed environment fault."""

        started = self._clock()
        checks: list[ReadinessCheck] = []
        failures: list[ReadinessFailure] = []
        requested_path = Path(request.worktree).expanduser()
        try:
            worktree = requested_path.resolve(strict=True)
            if not worktree.is_dir():
                raise NotADirectoryError(str(worktree))
        except (OSError, RuntimeError) as error:
            self._failed(
                checks,
                failures,
                check_id="worktree",
                kind="worktree",
                code="invalid-worktree",
                message=f"target worktree is unavailable: {requested_path}",
                remediation="Allocate or restore the exact target worktree before scheduling execution.",
                detail={"requested_path": str(requested_path), "error": _error_text(error)},
            )
            return self._report(str(requested_path), checks, failures, started)

        self._passed(
            checks,
            check_id="worktree",
            kind="worktree",
            detail={
                "requested_path": str(requested_path),
                "resolved_path": str(worktree),
                "exact_target": True,
            },
        )
        deadline = started + request.total_timeout_seconds

        for requirement in request.dependencies:
            self._check_dependency(worktree, requirement, checks, failures)
        for requirement in request.tools:
            self._check_tool(worktree, requirement, request, deadline, checks, failures)
        for requirement in request.source_resolutions:
            self._check_source_resolution(
                worktree, requirement, request, deadline, checks, failures
            )
        if request.pnpm is not None:
            self._check_pnpm(worktree, request.pnpm, request, deadline, checks, failures)

        return self._report(str(worktree), checks, failures, started)

    def _report(
        self,
        worktree: str,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
        started: float,
    ) -> ExecutionReadinessReport:
        elapsed_ms = max(0, int((self._clock() - started) * 1_000))
        return ExecutionReadinessReport(
            worktree=worktree,
            ready=not failures,
            checks=tuple(checks),
            failures=tuple(failures),
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _passed(
        checks: list[ReadinessCheck],
        *,
        check_id: str,
        kind: str,
        detail: Mapping[str, object],
    ) -> None:
        checks.append(ReadinessCheck(check_id, kind, "passed", dict(detail)))

    @staticmethod
    def _not_applicable(
        checks: list[ReadinessCheck],
        *,
        check_id: str,
        kind: str,
        detail: Mapping[str, object],
    ) -> None:
        checks.append(ReadinessCheck(check_id, kind, "not-applicable", dict(detail)))

    @staticmethod
    def _failed(
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
        *,
        check_id: str,
        kind: str,
        code: str,
        message: str,
        remediation: str,
        detail: Mapping[str, object],
    ) -> None:
        checks.append(ReadinessCheck(check_id, kind, "failed", dict(detail), code))
        failures.append(ReadinessFailure(check_id, code, message, remediation))

    def _check_dependency(
        self,
        worktree: Path,
        requirement: PathDependencyRequirement,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
    ) -> None:
        check_id = f"dependency:{requirement.name}"
        candidate = worktree / requirement.relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            if requirement.required:
                self._failed(
                    checks,
                    failures,
                    check_id=check_id,
                    kind="dependency",
                    code="missing-dependency",
                    message=f"required dependency {requirement.name!r} is missing from the target worktree",
                    remediation=(
                        f"Provide {requirement.relative_path!r} in the allocated worktree or adjust "
                        "the explicit dependency contract."
                    ),
                    detail={"relative_path": requirement.relative_path, "expected_kind": requirement.kind},
                )
            else:
                self._not_applicable(
                    checks,
                    check_id=check_id,
                    kind="dependency",
                    detail={"relative_path": requirement.relative_path, "reason": "optional dependency is absent"},
                )
            return
        except (OSError, RuntimeError) as error:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="dependency",
                code="dependency-unreadable",
                message=f"required dependency {requirement.name!r} cannot be resolved",
                remediation="Repair permissions or the target-worktree dependency path before execution.",
                detail={"relative_path": requirement.relative_path, "error": _error_text(error)},
            )
            return

        if requirement.must_resolve_within_worktree and not _is_within(resolved, worktree):
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="dependency",
                code="source-outside-worktree",
                message=(
                    f"required dependency {requirement.name!r} resolves outside the target worktree"
                ),
                remediation=(
                    "Restore a worktree-local dependency or explicitly declare a dependency that is "
                    "allowed to resolve outside the worktree."
                ),
                detail={
                    "relative_path": requirement.relative_path,
                    "resolved_path": str(resolved),
                    "target_worktree": str(worktree),
                },
            )
            return

        actual_kind = "directory" if resolved.is_dir() else "file" if resolved.is_file() else "other"
        if requirement.kind != "any" and actual_kind != requirement.kind:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="dependency",
                code="dependency-kind-mismatch",
                message=(
                    f"required dependency {requirement.name!r} is {actual_kind}, "
                    f"not the required {requirement.kind}"
                ),
                remediation="Restore the required dependency with the declared file type.",
                detail={
                    "relative_path": requirement.relative_path,
                    "resolved_path": str(resolved),
                    "expected_kind": requirement.kind,
                    "actual_kind": actual_kind,
                },
            )
            return

        self._passed(
            checks,
            check_id=check_id,
            kind="dependency",
            detail={
                "relative_path": requirement.relative_path,
                "resolved_path": str(resolved),
                "kind": actual_kind,
            },
        )

    def _check_tool(
        self,
        worktree: Path,
        requirement: ToolRequirement,
        request: ExecutionReadinessRequest,
        deadline: float,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
    ) -> None:
        check_id = f"tool:{requirement.name}"
        executable = self._resolve_executable(requirement.executable)
        if executable is None:
            if requirement.required:
                self._failed(
                    checks,
                    failures,
                    check_id=check_id,
                    kind="tool",
                    code="missing-tool",
                    message=f"required tool {requirement.name!r} is unavailable",
                    remediation=(
                        f"Install or expose {requirement.executable!r} to the worker environment "
                        "before execution."
                    ),
                    detail={"executable": requirement.executable},
                )
            else:
                self._not_applicable(
                    checks,
                    check_id=check_id,
                    kind="tool",
                    detail={"executable": requirement.executable, "reason": "optional tool is unavailable"},
                )
            return

        if not requirement.probe_version:
            self._passed(
                checks,
                check_id=check_id,
                kind="tool",
                detail={"executable": executable, "version_probe": "not-requested"},
            )
            return

        outcome = self._probe(
            (executable, requirement.version_argument), worktree, request, deadline
        )
        if not self._record_probe_outcome(
            outcome,
            check_id=check_id,
            kind="tool",
            checks=checks,
            failures=failures,
            remediation=(
                f"Repair or expose a working {requirement.name!r} tool in the worker environment."
            ),
        ):
            return
        assert outcome is not None
        self._passed(
            checks,
            check_id=check_id,
            kind="tool",
            detail={
                "executable": executable,
                "argv": list(outcome.command),
                "version": _probe_text(outcome),
                "timeout_seconds": outcome.timeout_seconds,
            },
        )

    def _check_source_resolution(
        self,
        worktree: Path,
        requirement: SourceResolutionRequirement,
        request: ExecutionReadinessRequest,
        deadline: float,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
    ) -> None:
        check_id = f"source:{requirement.module}"
        roots: list[Path] = []
        for relative_root in requirement.source_roots:
            candidate = worktree / relative_root
            try:
                resolved_root = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                self._failed(
                    checks,
                    failures,
                    check_id=check_id,
                    kind="source-resolution",
                    code="missing-dependency",
                    message=(
                        f"source root {relative_root!r} for {requirement.module!r} is unavailable "
                        "in the target worktree"
                    ),
                    remediation="Restore the declared local source root before execution.",
                    detail={"source_root": relative_root, "error": _error_text(error)},
                )
                return
            if not resolved_root.is_dir() or not _is_within(resolved_root, worktree):
                self._failed(
                    checks,
                    failures,
                    check_id=check_id,
                    kind="source-resolution",
                    code="source-outside-worktree",
                    message=(
                        f"source root {relative_root!r} for {requirement.module!r} does not resolve "
                        "inside the target worktree"
                    ),
                    remediation="Use a directory contained by the allocated worktree as the source root.",
                    detail={
                        "source_root": relative_root,
                        "resolved_path": str(resolved_root),
                        "target_worktree": str(worktree),
                    },
                )
                return
            roots.append(resolved_root)

        python = self._resolve_executable(request.python_executable)
        if python is None:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="source-resolution",
                code="missing-tool",
                message="the Python interpreter required for source-resolution evidence is unavailable",
                remediation="Expose the configured Python interpreter in the worker environment.",
                detail={"python_executable": request.python_executable},
            )
            return

        outcome = self._probe(
            (python, "-I", "-S", "-c", _SOURCE_RESOLUTION_PROBE, requirement.module,
             *(str(root) for root in roots)),
            worktree,
            request,
            deadline,
            isolated_python=True,
        )
        if not self._record_probe_outcome(
            outcome,
            check_id=check_id,
            kind="source-resolution",
            checks=checks,
            failures=failures,
            remediation="Repair the local source layout or configured Python runtime before execution.",
        ):
            return
        assert outcome is not None
        try:
            payload = json.loads(outcome.stdout)
            if not isinstance(payload, dict):
                raise ValueError("probe output is not an object")
            if payload.get("module") != requirement.module:
                raise ValueError("probe output module does not match the request")
            origin = payload.get("origin")
            locations = payload.get("locations")
            if origin is not None and not isinstance(origin, str):
                raise ValueError("probe origin is invalid")
            if not isinstance(locations, list) or not all(isinstance(item, str) for item in locations):
                raise ValueError("probe locations are invalid")
        except (json.JSONDecodeError, ValueError) as error:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="source-resolution",
                code="source-probe-invalid",
                message=f"source-resolution probe for {requirement.module!r} returned invalid evidence",
                remediation="Repair the configured Python runtime and retry the bounded readiness check.",
                detail={"stdout": _bounded_text(outcome.stdout), "error": _error_text(error)},
            )
            return

        raw_paths = ([origin] if origin else []) + locations
        if not raw_paths:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="source-resolution",
                code="source-unresolved",
                message=f"source package {requirement.module!r} could not be resolved from the target worktree",
                remediation="Restore the package under one of the declared source roots.",
                detail={"source_roots": [str(root) for root in roots]},
            )
            return

        resolved_paths: list[Path] = []
        for raw_path in raw_paths:
            try:
                resolved_paths.append(Path(raw_path).resolve(strict=False))
            except (OSError, RuntimeError) as error:
                self._failed(
                    checks,
                    failures,
                    check_id=check_id,
                    kind="source-resolution",
                    code="source-probe-invalid",
                    message=f"source package {requirement.module!r} produced an unreadable resolution path",
                    remediation="Repair the target source layout before execution.",
                    detail={"raw_path": raw_path, "error": _error_text(error)},
                )
                return

        outside = [str(path) for path in resolved_paths if not _is_within(path, worktree)]
        if outside:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="source-resolution",
                code="source-outside-worktree",
                message=(
                    f"source package {requirement.module!r} resolves outside the allocated worktree"
                ),
                remediation=(
                    "Restore the package in the declared target-worktree source root; do not use a "
                    "different worktree's package as readiness evidence."
                ),
                detail={
                    "origin": origin,
                    "locations": locations,
                    "outside_paths": outside,
                    "target_worktree": str(worktree),
                },
            )
            return

        self._passed(
            checks,
            check_id=check_id,
            kind="source-resolution",
            detail={
                "module": requirement.module,
                "source_roots": [str(root) for root in roots],
                "origin": origin,
                "locations": locations,
                "probe": "python -I -S PathFinder (no target package import)",
                "timeout_seconds": outcome.timeout_seconds,
            },
        )

    def _check_pnpm(
        self,
        worktree: Path,
        requirement: PnpmRequirement,
        request: ExecutionReadinessRequest,
        deadline: float,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
    ) -> None:
        manifest_path = worktree / "package.json"
        lockfile_path = worktree / "pnpm-lock.yaml"
        manifest_exists = manifest_path.is_file()
        lockfile_exists = lockfile_path.is_file()
        manifest_check = "pnpm:manifest"

        if requirement.mode == "disabled":
            self._not_applicable(
                checks,
                check_id=manifest_check,
                kind="pnpm",
                detail={"reason": "pnpm readiness was explicitly disabled"},
            )
            return
        if not manifest_exists and not lockfile_exists:
            if requirement.mode == "required":
                self._failed(
                    checks,
                    failures,
                    check_id=manifest_check,
                    kind="pnpm",
                    code="missing-dependency",
                    message="this execution requires a pnpm project, but package.json and pnpm-lock.yaml are absent",
                    remediation="Provide the declared pnpm project inputs in the target worktree.",
                    detail={"package_json": False, "pnpm_lockfile": False},
                )
            else:
                self._not_applicable(
                    checks,
                    check_id=manifest_check,
                    kind="pnpm",
                    detail={"reason": "target has no package.json or pnpm-lock.yaml", "non_node_usable": True},
                )
            return
        if not manifest_exists or not lockfile_exists:
            missing = "package.json" if not manifest_exists else "pnpm-lock.yaml"
            self._failed(
                checks,
                failures,
                check_id=manifest_check,
                kind="pnpm",
                code="missing-dependency",
                message=f"pnpm project inputs are incomplete; {missing} is missing from the target worktree",
                remediation=(
                    "Restore both package.json and pnpm-lock.yaml from the allocated worktree's "
                    "contracted source."
                ),
                detail={"package_json": manifest_exists, "pnpm_lockfile": lockfile_exists},
            )
            return

        try:
            manifest_bytes = manifest_path.read_bytes()
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
            manifest = json.loads(manifest_bytes)
            declared = manifest.get("packageManager") if isinstance(manifest, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            self._failed(
                checks,
                failures,
                check_id=manifest_check,
                kind="pnpm",
                code="invalid-dependency",
                message="package.json cannot provide a valid pnpm toolchain declaration",
                remediation="Restore a readable package.json with packageManager=pnpm@<version>.",
                detail={"path": str(manifest_path), "error": _error_text(error)},
            )
            return
        if not isinstance(declared, str) or not declared.startswith("pnpm@"):
            self._failed(
                checks,
                failures,
                check_id=manifest_check,
                kind="pnpm",
                code="toolchain-mismatch",
                message="pnpm readiness requires package.json packageManager=pnpm@<version>",
                remediation="Declare the pnpm version used by the worktree before execution.",
                detail={"package_manager": declared},
            )
            return

        declared_version = declared.split("@", 1)[1].split("+", 1)[0]
        declared_semver = _semantic_version(declared_version)
        if declared_semver is None:
            self._failed(
                checks,
                failures,
                check_id=manifest_check,
                kind="pnpm",
                code="toolchain-mismatch",
                message=f"declared pnpm version is not semantic: {declared_version!r}",
                remediation="Declare a three-part semantic pnpm version in package.json.",
                detail={"package_manager": declared},
            )
            return
        self._passed(
            checks,
            check_id=manifest_check,
            kind="pnpm",
            detail={
                "package_json": str(manifest_path),
                "pnpm_lockfile": str(lockfile_path),
                "package_manager": declared,
                "declared_version": declared_version,
            },
        )

        binary, binary_source = self._pnpm_binary(requirement)
        executable = self._resolve_executable(binary)
        toolchain_check = "pnpm:toolchain"
        if executable is None:
            self._failed(
                checks,
                failures,
                check_id=toolchain_check,
                kind="pnpm",
                code="missing-tool",
                message="the required pnpm runtime is unavailable on the worker authority",
                remediation=(
                    "Expose the Workbench-managed pnpm runtime before execution; readiness will not "
                    "run pnpm install or download a runtime."
                ),
                detail={"binary": binary, "binary_source": binary_source},
            )
        else:
            # pnpm may honor the target repository's packageManager field even
            # for --version and transparently select that project version.
            # Observe the authority runtime in a neutral directory, matching
            # the offline materializer, then compare its major with the
            # repository declaration below.
            probe_directory = Path(tempfile.gettempdir()).resolve()
            outcome = self._probe(
                (executable, "--version"),
                probe_directory,
                request,
                deadline,
            )
            if self._record_probe_outcome(
                outcome,
                check_id=toolchain_check,
                kind="pnpm",
                checks=checks,
                failures=failures,
                remediation=(
                    "Repair the Workbench-managed pnpm runtime; readiness only performs a bounded "
                    "version probe."
                ),
            ):
                assert outcome is not None
                actual_version = _probe_text(outcome)
                actual_semver = _semantic_version(actual_version)
                if actual_semver is None:
                    self._failed(
                        checks,
                        failures,
                        check_id=toolchain_check,
                        kind="pnpm",
                        code="toolchain-mismatch",
                        message=f"authority pnpm version is not semantic: {actual_version!r}",
                        remediation="Configure a semantic Workbench-managed pnpm runtime.",
                        detail={"binary": executable, "version": actual_version},
                    )
                elif actual_semver[0] != declared_semver[0]:
                    self._failed(
                        checks,
                        failures,
                        check_id=toolchain_check,
                        kind="pnpm",
                        code="toolchain-mismatch",
                        message=(
                            f"pnpm major mismatch: package declares {declared_version}, "
                            f"authority provides {actual_version}"
                        ),
                        remediation="Configure a Workbench-managed pnpm runtime with the declared major.",
                        detail={
                            "binary": executable,
                            "declared_version": declared_version,
                            "actual_version": actual_version,
                        },
                    )
                elif actual_semver[0] == 11 and actual_semver < PNPM_MINIMUM_11_VERSION:
                    minimum = ".".join(str(part) for part in PNPM_MINIMUM_11_VERSION)
                    self._failed(
                        checks,
                        failures,
                        check_id=toolchain_check,
                        kind="pnpm",
                        code="toolchain-mismatch",
                        message=(
                            f"pnpm {actual_version} is unsupported for managed offline worktrees; "
                            f"pnpm 11 must be at least {minimum}"
                        ),
                        remediation="Configure the Workbench-managed pnpm 11.25.0-or-newer runtime.",
                        detail={
                            "binary": executable,
                            "declared_version": declared_version,
                            "actual_version": actual_version,
                            "minimum_version": minimum,
                        },
                    )
                else:
                    self._passed(
                        checks,
                        check_id=toolchain_check,
                        kind="pnpm",
                        detail={
                            "binary": executable,
                            "binary_source": binary_source,
                            "probe_cwd": str(probe_directory),
                            "argv": list(outcome.command),
                            "declared_version": declared_version,
                            "actual_version": actual_version,
                            "managed_pnpm_11_minimum": ".".join(
                                str(part) for part in PNPM_MINIMUM_11_VERSION
                            ),
                            "timeout_seconds": outcome.timeout_seconds,
                        },
                    )

        if requirement.require_linker:
            self._check_pnpm_linker(worktree, request, checks, failures)
        else:
            self._not_applicable(
                checks,
                check_id="pnpm:linker",
                kind="pnpm",
                detail={"reason": "linker readiness was explicitly not required"},
            )

    def _check_pnpm_linker(
        self,
        worktree: Path,
        request: ExecutionReadinessRequest,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
    ) -> None:
        check_id = "pnpm:linker"
        try:
            linker_paths = _pnpm_linker_paths(worktree, request.max_linker_walk_directories)
        except OSError as error:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="pnpm",
                code="dependency-unreadable",
                message="the target worktree pnpm linker layout cannot be inspected",
                remediation="Repair the target worktree filesystem before execution.",
                detail={"error": _error_text(error)},
            )
            return
        except ValueError as error:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="pnpm",
                code="probe-limit-exceeded",
                message="the target worktree pnpm linker scan exceeded its bounded directory limit",
                remediation=(
                    "Reduce the worktree scan scope or repair the linker layout; readiness will not "
                    "walk it without a bound."
                ),
                detail={"limit": request.max_linker_walk_directories, "error": _error_text(error)},
            )
            return

        root_linker = worktree / "node_modules"
        root_complete = _pnpm_root_linker_complete(root_linker)
        missing_package_linkers = [
            relative.as_posix()
            for relative in linker_paths
            if relative != Path("node_modules")
            and not ((worktree / relative).is_dir() or (worktree / relative).is_symlink())
        ]
        if not root_complete or missing_package_linkers:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind="pnpm",
                code="missing-dependency",
                message="the target worktree does not have a complete pnpm linker layout",
                remediation=(
                    "Materialize or restore the worktree-local linker through the authorized recovery "
                    "path; readiness never runs pnpm install."
                ),
                detail={
                    "root_linker": str(root_linker),
                    "root_complete": root_complete,
                    "required_root_markers": [".modules.yaml", ".bin"],
                    "linker_paths": [relative.as_posix() for relative in linker_paths],
                    "missing_package_linkers": missing_package_linkers,
                },
            )
            return
        self._passed(
            checks,
            check_id=check_id,
            kind="pnpm",
            detail={
                "root_linker": str(root_linker),
                "root_complete": True,
                "required_root_markers": [".modules.yaml", ".bin"],
                "linker_paths": [relative.as_posix() for relative in linker_paths],
                "package_local_linkers": [
                    relative.as_posix()
                    for relative in linker_paths
                    if relative != Path("node_modules")
                ],
            },
        )

    def _pnpm_binary(self, requirement: PnpmRequirement) -> tuple[str, str]:
        if requirement.binary is not None:
            return requirement.binary, "requirement"
        managed = self._environment.get("CODEX_WORKBENCH_PNPM")
        if managed:
            return managed, "CODEX_WORKBENCH_PNPM"
        return "pnpm", "PATH"

    def _resolve_executable(self, requested: str) -> str | None:
        expanded = os.path.expanduser(requested)
        if os.path.sep in expanded or (os.path.altsep and os.path.altsep in expanded):
            candidate = Path(expanded)
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return self._which(expanded)

    def _probe(
        self,
        command: tuple[str, ...],
        worktree: Path,
        request: ExecutionReadinessRequest,
        deadline: float,
        *,
        isolated_python: bool = False,
    ) -> _ProbeOutcome | None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return None
        timeout = min(request.probe_timeout_seconds, max(1, int(remaining + 0.999)))
        environment = dict(self._environment)
        if isolated_python:
            # -I ignores these, and removing them documents that readiness
            # source evidence cannot inherit a coordinator-side source tree.
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
        try:
            completed = self._runner(
                list(command),
                cwd=worktree,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return _ProbeOutcome(
                command, timeout, None, _bounded_text(_timeout_output(error, "output")),
                _bounded_text(_timeout_output(error, "stderr")), _error_text(error), True
            )
        except OSError as error:
            return _ProbeOutcome(command, timeout, None, "", "", _error_text(error))
        return _ProbeOutcome(
            command,
            timeout,
            int(completed.returncode),
            _bounded_text(completed.stdout or ""),
            _bounded_text(completed.stderr or ""),
        )

    def _record_probe_outcome(
        self,
        outcome: _ProbeOutcome | None,
        *,
        check_id: str,
        kind: str,
        checks: list[ReadinessCheck],
        failures: list[ReadinessFailure],
        remediation: str,
    ) -> bool:
        if outcome is None:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind=kind,
                code="probe-budget-exhausted",
                message="readiness probe budget was exhausted before this check could run",
                remediation=remediation,
                detail={},
            )
            return False
        if outcome.timed_out:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind=kind,
                code="probe-timeout",
                message="bounded readiness probe timed out",
                remediation=remediation,
                detail={"argv": list(outcome.command), "timeout_seconds": outcome.timeout_seconds},
            )
            return False
        if outcome.error is not None:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind=kind,
                code="probe-error",
                message="readiness probe could not start",
                remediation=remediation,
                detail={"argv": list(outcome.command), "error": outcome.error},
            )
            return False
        if outcome.exit_code != 0:
            self._failed(
                checks,
                failures,
                check_id=check_id,
                kind=kind,
                code="probe-failed",
                message=f"readiness probe exited {outcome.exit_code}",
                remediation=remediation,
                detail={
                    "argv": list(outcome.command),
                    "exit_code": outcome.exit_code,
                    "stdout": outcome.stdout,
                    "stderr": outcome.stderr,
                },
            )
            return False
        return True


def assess_execution_readiness(
    request: ExecutionReadinessRequest,
    *,
    runner: CommandRunner = subprocess.run,
    which: ExecutableResolver = shutil.which,
    environment: Mapping[str, str] | None = None,
    clock: Clock = time.monotonic,
) -> ExecutionReadinessReport:
    """Convenience entry point for a single pure readiness assessment."""

    return ExecutionReadinessChecker(
        runner=runner,
        which=which,
        environment=environment,
        clock=clock,
    ).assess(request)


def check_execution_readiness(
    request: ExecutionReadinessRequest,
    **kwargs: Any,
) -> ExecutionReadinessReport:
    """Compatibility-friendly verb for callers that prefer ``check_*`` APIs."""

    return assess_execution_readiness(request, **kwargs)


def _validated_relative_path(value: str, label: str) -> Path:
    _require_nonempty(value, label)
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or ".." in path.parts or ".." in windows.parts:
        raise ValueError(f"{label} must be a relative path contained by the target worktree")
    return path


def _require_nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    normalized = value.strip().split("+", 1)[0].split("-", 1)[0]
    fields = normalized.split(".")
    if len(fields) != 3 or any(not field.isdigit() for field in fields):
        return None
    return tuple(int(field) for field in fields)  # type: ignore[return-value]


def _pnpm_root_linker_complete(node_modules: Path) -> bool:
    """Use the same completion markers as PnpmOfflineMaterializer."""

    return (
        node_modules.is_dir()
        and (node_modules / ".modules.yaml").is_file()
        and (node_modules / ".bin").is_dir()
    )


def _pnpm_linker_paths(worktree: Path, maximum_directories: int) -> tuple[Path, ...]:
    """Find root and package-local pnpm linker directories without walking them."""

    paths = {Path("node_modules")}
    walked = 0
    errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        errors.append(error)

    for current, directories, _files in os.walk(worktree, onerror=onerror):
        walked += 1
        if walked > maximum_directories:
            raise ValueError(f"walked more than {maximum_directories} directories")
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
    if errors:
        raise errors[0]
    return tuple(sorted(paths, key=str))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_text(value: str, limit: int = MAX_PROBE_OUTPUT_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n[output truncated by readiness]\n"


def _probe_text(outcome: _ProbeOutcome) -> str:
    return (outcome.stdout.strip() or outcome.stderr.strip())


def _timeout_output(error: subprocess.TimeoutExpired, attribute: str) -> str:
    value = getattr(error, attribute, "")
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _error_text(error: BaseException) -> str:
    return _bounded_text(str(error) or type(error).__name__)


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


_SOURCE_RESOLUTION_PROBE = r"""
import importlib.machinery
import json
import sys

module = sys.argv[1]
roots = sys.argv[2:]
parts = module.split(".")
search = roots
spec = None
for index, part in enumerate(parts):
    spec = importlib.machinery.PathFinder.find_spec(part, search)
    if spec is None:
        break
    if index < len(parts) - 1:
        locations = spec.submodule_search_locations
        if locations is None:
            spec = None
            break
        search = list(locations)

if spec is None:
    payload = {"module": module, "origin": None, "locations": []}
else:
    payload = {
        "module": module,
        "origin": spec.origin,
        "locations": list(spec.submodule_search_locations or ()),
    }
print(json.dumps(payload, sort_keys=True))
""".strip()


__all__ = [
    "DependencyKind",
    "ExecutionReadinessChecker",
    "ExecutionReadinessReport",
    "ExecutionReadinessRequest",
    "MAX_PROBE_TIMEOUT_SECONDS",
    "MAX_TOTAL_TIMEOUT_SECONDS",
    "PNPM_MINIMUM_11_VERSION",
    "PathDependencyRequirement",
    "PnpmMode",
    "PnpmReadinessRequirement",
    "PnpmRequirement",
    "ReadinessCheck",
    "ReadinessCheckStatus",
    "ReadinessFailure",
    "SourceResolutionRequirement",
    "ToolRequirement",
    "assess_execution_readiness",
    "check_execution_readiness",
]
