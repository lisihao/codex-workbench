"""Detached, file-backed runner for one explicitly authorized local install.

The lifecycle coordinator records the dispatch before it starts this helper.
This module deliberately owns neither a scheduler nor a database: its durable
inputs and outputs are one request file and one result file bound to that
existing dispatch ID.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Any, Callable, Iterator, Mapping


_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_DISPATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_SCHEMA_VERSION = 1
_SOURCE_IDENTITY_PATHS = (
    "pyproject.toml",
    "src/codex_workbench/__init__.py",
    "src/codex_workbench/deployment_helper.py",
    "scripts/install-macos.py",
)
_BOUND_INSTALLER_OPTIONS = ("--source", "--state-root", "--dry-run")
_OUTPUT_LIMIT = 4096


def _now() -> str:
    """Return one UTC timestamp suitable for a JSON receipt."""

    return datetime.now(UTC).isoformat(timespec="seconds")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty text value")
    return value.strip()


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_required_text(value, label)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _validate_dispatch_id(value: object) -> str:
    dispatch_id = _required_text(value, "dispatch_id")
    if _DISPATCH_ID.fullmatch(dispatch_id) is None:
        raise ValueError("dispatch_id contains unsupported characters")
    return dispatch_id


def _bounded_output(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[-_OUTPUT_LIMIT:]


def _validate_installer_arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("installer.arguments must be an array")
    arguments: list[str] = []
    for raw in value:
        argument = _required_text(raw, "installer argument")
        if any(argument == option or argument.startswith(f"{option}=") for option in _BOUND_INSTALLER_OPTIONS):
            raise ValueError(f"installer argument {argument!r} may not override the dispatch binding")
        arguments.append(argument)
    return tuple(arguments)


def _local_probe_url(value: object, label: str, expected_path: str) -> str:
    url = _required_text(value, label)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(f"{label} must be a loopback HTTP URL")
    if parsed.port is None or parsed.path != expected_path:
        raise ValueError(f"{label} must include a local port and {expected_path}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{label} may not include credentials, a query, or a fragment")
    return url


def _rollback_baseline(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("rollback_baseline must be an object or null")
    eligible = value.get("eligible")
    if not isinstance(eligible, bool):
        raise ValueError("rollback_baseline.eligible must be a boolean")
    normalized = dict(value)
    if not eligible:
        return normalized
    manifest = value.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("eligible rollback baseline requires a manifest")
    commit = _required_text(manifest.get("commit"), "rollback baseline commit").lower()
    version = _required_text(manifest.get("version"), "rollback baseline version")
    epoch = value.get("coordinator_epoch")
    instance = _required_text(value.get("coordinator_instance_id"), "rollback baseline coordinator instance")
    if _COMMIT.fullmatch(commit) is None or not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
        raise ValueError("eligible rollback baseline has an invalid identity")
    normalized["manifest"] = {"commit": commit, "version": version}
    normalized["coordinator_epoch"] = epoch
    normalized["coordinator_instance_id"] = instance
    return normalized


def _rollback_probe(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("rollback_probe must be an object")
    timeout = value.get("timeout_seconds")
    interval = value.get("interval_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("rollback_probe.timeout_seconds must be a positive integer")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0 or interval > timeout:
        raise ValueError("rollback_probe.interval_seconds must be a positive integer no greater than timeout")
    return {
        "health_url": _local_probe_url(value.get("health_url"), "rollback health URL", "/health"),
        "functional_url": _local_probe_url(
            value.get("functional_url"), "rollback functional URL", "/api/snapshot"
        ),
        "timeout_seconds": timeout,
        "interval_seconds": interval,
    }


@dataclass(frozen=True)
class SourceIdentity:
    """Exact source files and revision a helper must install."""

    commit: str
    version: str
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        commit = _required_text(self.commit, "source.commit").lower()
        if _COMMIT.fullmatch(commit) is None:
            raise ValueError("source.commit must be a full hexadecimal Git commit")
        version = _required_text(self.version, "source.version")
        if not isinstance(self.manifest, Mapping):
            raise ValueError("source.manifest must be an object")
        algorithm = self.manifest.get("algorithm")
        files = self.manifest.get("files")
        if algorithm != "sha256" or not isinstance(files, Mapping) or not files:
            raise ValueError("source.manifest must contain sha256 file entries")
        normalized_files: dict[str, str] = {}
        for name, digest in files.items():
            relative = _required_text(name, "source.manifest file")
            if relative not in _SOURCE_IDENTITY_PATHS:
                raise ValueError(f"source.manifest file {relative!r} is not an installer identity input")
            digest_text = _required_text(digest, "source.manifest digest").lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest_text) is None:
                raise ValueError("source.manifest digest must be sha256 hexadecimal")
            normalized_files[relative] = digest_text
        if set(normalized_files) != set(_SOURCE_IDENTITY_PATHS):
            raise ValueError("source.manifest must cover every installer identity input")
        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "manifest", {"algorithm": "sha256", "files": normalized_files})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe source record."""

        return {
            "commit": self.commit,
            "version": self.version,
            "manifest": {
                "algorithm": self.manifest["algorithm"],
                "files": dict(self.manifest["files"]),
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> "SourceIdentity":
        """Decode a source record emitted by the authority adapter."""

        if not isinstance(value, Mapping):
            raise ValueError("source must be an object")
        return cls(
            commit=_required_text(value.get("commit"), "source.commit"),
            version=_required_text(value.get("version"), "source.version"),
            manifest=value.get("manifest"),
        )


@dataclass(frozen=True)
class DeploymentHelperRequest:
    """The one immutable installer invocation allowed for a dispatch."""

    dispatch_id: str
    target: str
    source_root: Path
    trusted_source_root: Path
    state_root: Path
    installer_python: Path
    installer_script: Path
    installer_arguments: tuple[str, ...]
    installer_timeout_seconds: int
    source: SourceIdentity
    pre_install_coordinator_epoch: int
    rollback_baseline: Mapping[str, Any] | None
    rollback_probe: Mapping[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        dispatch_id = _validate_dispatch_id(self.dispatch_id)
        target = _required_text(self.target, "target")
        source_root = _absolute_path(str(self.source_root), "source_root")
        trusted_source_root = _absolute_path(str(self.trusted_source_root), "trusted_source_root")
        state_root = _absolute_path(str(self.state_root), "state_root")
        installer_python = _absolute_path(str(self.installer_python), "installer.python")
        installer_script = _absolute_path(str(self.installer_script), "installer.script")
        if not isinstance(self.installer_timeout_seconds, int) or isinstance(
            self.installer_timeout_seconds, bool
        ) or self.installer_timeout_seconds <= 0:
            raise ValueError("installer_timeout_seconds must be a positive integer")
        arguments = _validate_installer_arguments(self.installer_arguments)
        if not isinstance(self.source, SourceIdentity):
            raise ValueError("source must be a SourceIdentity")
        if not isinstance(self.pre_install_coordinator_epoch, int) or isinstance(
            self.pre_install_coordinator_epoch, bool
        ) or self.pre_install_coordinator_epoch <= 0:
            raise ValueError("pre_install_coordinator_epoch must be a positive integer")
        rollback_baseline = _rollback_baseline(self.rollback_baseline)
        rollback_probe = _rollback_probe(self.rollback_probe)
        _required_text(self.created_at, "created_at")
        object.__setattr__(self, "dispatch_id", dispatch_id)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "trusted_source_root", trusted_source_root)
        object.__setattr__(self, "state_root", state_root)
        object.__setattr__(self, "installer_python", installer_python)
        object.__setattr__(self, "installer_script", installer_script)
        object.__setattr__(self, "installer_arguments", arguments)
        object.__setattr__(self, "rollback_baseline", rollback_baseline)
        object.__setattr__(self, "rollback_probe", rollback_probe)

    @property
    def installer_argv(self) -> tuple[str, ...]:
        """Return the fixed argv passed to the repository installer."""

        return (
            str(self.installer_python),
            str(self.installer_script),
            "--source",
            str(self.source_root),
            "--state-root",
            str(self.state_root),
            *self.installer_arguments,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the request without introducing an executable shell string."""

        return {
            "schema_version": _SCHEMA_VERSION,
            "dispatch_id": self.dispatch_id,
            "target": self.target,
            "source_root": str(self.source_root),
            "trusted_source_root": str(self.trusted_source_root),
            "state_root": str(self.state_root),
            "installer": {
                "python": str(self.installer_python),
                "script": str(self.installer_script),
                "arguments": list(self.installer_arguments),
                "timeout_seconds": self.installer_timeout_seconds,
            },
            "source": self.source.to_dict(),
            "pre_install_coordinator_epoch": self.pre_install_coordinator_epoch,
            "rollback_baseline": dict(self.rollback_baseline) if self.rollback_baseline is not None else None,
            "rollback_probe": dict(self.rollback_probe),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "DeploymentHelperRequest":
        """Decode a request and reject a widened installer action."""

        if not isinstance(value, Mapping):
            raise ValueError("deployment helper request must be an object")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("deployment helper request schema is unsupported")
        installer = value.get("installer")
        if not isinstance(installer, Mapping):
            raise ValueError("installer must be an object")
        request = cls(
            dispatch_id=_validate_dispatch_id(value.get("dispatch_id")),
            target=_required_text(value.get("target"), "target"),
            source_root=_absolute_path(value.get("source_root"), "source_root"),
            trusted_source_root=_absolute_path(
                value.get("trusted_source_root", value.get("source_root")), "trusted_source_root"
            ),
            state_root=_absolute_path(value.get("state_root"), "state_root"),
            installer_python=_absolute_path(installer.get("python"), "installer.python"),
            installer_script=_absolute_path(installer.get("script"), "installer.script"),
            installer_arguments=_validate_installer_arguments(installer.get("arguments")),
            installer_timeout_seconds=installer.get("timeout_seconds"),
            source=SourceIdentity.from_dict(value.get("source")),
            pre_install_coordinator_epoch=value.get("pre_install_coordinator_epoch"),
            rollback_baseline=value.get("rollback_baseline"),
            rollback_probe=value.get("rollback_probe"),
            created_at=_required_text(value.get("created_at"), "created_at"),
        )
        expected_script = request.source_root / "scripts" / "install-macos.py"
        if request.installer_script != expected_script:
            raise ValueError("installer.script must be the source checkout install-macos.py")
        return request


@dataclass(frozen=True)
class DeploymentPaths:
    """Filesystem locations derived only from a trusted state root and dispatch."""

    root: Path
    request: Path
    result: Path
    running: Path
    lock: Path


def deployment_paths(state_root: Path, dispatch_id: str) -> DeploymentPaths:
    """Return paths for one dispatch without using task-state storage."""

    safe_dispatch = _validate_dispatch_id(dispatch_id)
    root = _absolute_path(str(state_root), "state_root") / "deployment-dispatches"
    return DeploymentPaths(
        root=root,
        request=root / f"{safe_dispatch}.request.json",
        result=root / f"{safe_dispatch}.result.json",
        running=root / f"{safe_dispatch}.running.json",
        lock=root / "deployment-helper.lock",
    )


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Persist one JSON document atomically with authority-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked deployment record {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_deployment_json(path: Path) -> dict[str, Any] | None:
    """Read one durable helper document, returning ``None`` only when absent."""

    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"deployment record is not a regular file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"deployment record is unreadable: {type(error).__name__}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("deployment record must be a JSON object")
    return raw


def write_deployment_request(request: DeploymentHelperRequest) -> Path:
    """Write the dispatch-bound request before any helper is launched."""

    paths = deployment_paths(request.state_root, request.dispatch_id)
    existing = read_deployment_json(paths.request)
    serialized = request.to_dict()
    if existing is not None:
        if existing != serialized:
            raise ValueError("existing deployment request conflicts with the dispatch-bound request")
        return paths.request
    _atomic_write_json(paths.request, serialized)
    return paths.request


def collect_source_identity(
    source_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: int = 15,
) -> SourceIdentity:
    """Read the fixed checkout identity without running project code."""

    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("source identity timeout_seconds must be a positive integer")
    try:
        root = source_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"source root is unavailable: {type(error).__name__}: {error}") from error
    if not root.is_dir():
        raise ValueError("source root is not a directory")
    try:
        result = runner(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"source commit observation failed: {type(error).__name__}: {error}") from error
    commit = result.stdout.strip().lower()
    if result.returncode != 0 or _COMMIT.fullmatch(commit) is None:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ValueError(f"source commit observation failed: {detail[:512]}")
    manifest_files: dict[str, str] = {}
    for relative in _SOURCE_IDENTITY_PATHS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"source identity input is not a regular file: {relative}")
        manifest_files[relative] = sha256(path.read_bytes()).hexdigest()
    try:
        with (root / "pyproject.toml").open("rb") as input_file:
            project = tomllib.load(input_file).get("project")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"source version metadata is unreadable: {type(error).__name__}: {error}") from error
    version = project.get("version") if isinstance(project, Mapping) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("source version metadata has no project.version")
    init_text = (root / "src/codex_workbench/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', init_text, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise ValueError("source package version and pyproject version differ")
    return SourceIdentity(
        commit=commit,
        version=version,
        manifest={"algorithm": "sha256", "files": manifest_files},
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize helper execution without adding a scheduler or state database."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"deployment helper lock is symlinked: {path}")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_local_json(url: str, timeout_seconds: int) -> tuple[int, Mapping[str, Any]]:
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("loopback probe response must be a JSON object")
        return int(response.status), payload


def _rollback_runtime_check(
    status: int,
    payload: Mapping[str, Any],
    *,
    commit: str,
    version: str,
    previous_epoch: int,
    nested_authority: bool,
    require_ok: bool,
) -> dict[str, Any]:
    build = payload.get("build")
    health = payload.get("health")
    authority = (
        health.get("authority")
        if nested_authority and isinstance(health, Mapping)
        else payload.get("authority")
    )
    observed = {
        "version": payload.get("version"),
        "build_commit": build.get("commit") if isinstance(build, Mapping) else None,
        "build_version": build.get("version") if isinstance(build, Mapping) else None,
        "coordinator_epoch": authority.get("coordinator_epoch") if isinstance(authority, Mapping) else None,
        "coordinator_instance_id": authority.get("instance_id") if isinstance(authority, Mapping) else None,
    }
    passed = (
        status == 200
        and (not require_ok or payload.get("ok") is True)
        and observed["version"] == version
        and observed["build_commit"] == commit
        and observed["build_version"] == version
        and isinstance(authority, Mapping)
        and authority.get("active") is True
        and isinstance(observed["coordinator_epoch"], int)
        and not isinstance(observed["coordinator_epoch"], bool)
        and observed["coordinator_epoch"] > previous_epoch
        and isinstance(observed["coordinator_instance_id"], str)
        and bool(observed["coordinator_instance_id"].strip())
    )
    return {"status": "passed" if passed else "failed", "http_status": status, **observed}


def _rollback_probe_once(
    request: DeploymentHelperRequest,
    baseline: Mapping[str, Any],
    *,
    http_reader: Callable[[str, int], tuple[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    manifest = baseline["manifest"]
    commit = manifest["commit"]
    version = manifest["version"]
    previous_epoch = baseline["coordinator_epoch"]
    try:
        observed_manifest = read_deployment_json(request.state_root / "app" / "install-manifest.json")
    except ValueError as error:
        return {"verified": False, "reason": f"installed manifest is unreadable: {type(error).__name__}: {error}"}
    if (
        not isinstance(observed_manifest, Mapping)
        or observed_manifest.get("commit") != commit
        or observed_manifest.get("version") != version
    ):
        return {
            "verified": False,
            "reason": "installed manifest does not match the pre-install runtime identity",
            "observed_manifest": dict(observed_manifest or {}),
        }
    probe = request.rollback_probe
    try:
        health_status, health_payload = http_reader(probe["health_url"], probe["timeout_seconds"])
        functional_status, functional_payload = http_reader(
            probe["functional_url"], probe["timeout_seconds"]
        )
    except Exception as error:
        return {"verified": False, "reason": f"rollback loopback probe failed: {type(error).__name__}: {error}"}
    health = _rollback_runtime_check(
        health_status,
        health_payload,
        commit=commit,
        version=version,
        previous_epoch=previous_epoch,
        nested_authority=False,
        require_ok=True,
    )
    functional = _rollback_runtime_check(
        functional_status,
        functional_payload,
        commit=commit,
        version=version,
        previous_epoch=previous_epoch,
        nested_authority=True,
        require_ok=False,
    )
    same_instance = (
        health.get("coordinator_epoch") == functional.get("coordinator_epoch")
        and health.get("coordinator_instance_id") == functional.get("coordinator_instance_id")
    )
    verified = health["status"] == "passed" and functional["status"] == "passed" and same_instance
    return {
        "verified": verified,
        "reason": (
            "pre-install manifest and read-only runtime identity were restored"
            if verified
            else "pre-install runtime identity is not yet proven restored"
        ),
        "baseline": dict(baseline),
        "observed_manifest": {"commit": observed_manifest.get("commit"), "version": observed_manifest.get("version")},
        "checks": {"health": health, "functional": functional},
    }


def _verify_rollback(
    request: DeploymentHelperRequest,
    *,
    http_reader: Callable[[str, int], tuple[int, Mapping[str, Any]]],
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    baseline = request.rollback_baseline
    if not isinstance(baseline, Mapping) or baseline.get("eligible") is not True:
        return {
            "verified": False,
            "reason": "no healthy pre-install manifest and runtime baseline exists for rollback verification",
            "baseline": dict(baseline or {}),
        }
    deadline = monotonic() + request.rollback_probe["timeout_seconds"]
    last: dict[str, Any] | None = None
    while True:
        last = _rollback_probe_once(request, baseline, http_reader=http_reader)
        if last.get("verified") is True or monotonic() >= deadline:
            return last
        sleeper(float(request.rollback_probe["interval_seconds"]))


def _result_document(
    request: DeploymentHelperRequest,
    *,
    status: str,
    started_at: str,
    installer: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "dispatch_id": request.dispatch_id,
        "target": request.target,
        "status": status,
        "source": request.source.to_dict(),
        "started_at": started_at,
        "finished_at": _now(),
        "installer": dict(installer),
    }


def _remove_running(path: Path) -> None:
    if path.is_symlink():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def run_deployment_helper(
    request_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    identity_reader: Callable[[Path], SourceIdentity] = collect_source_identity,
    rollback_http_reader: Callable[[str, int], tuple[int, Mapping[str, Any]]] = _read_local_json,
) -> dict[str, Any]:
    """Run one fixed installer and atomically persist its observable outcome.

    This function is intentionally testable with fake runners.  It never uses
    a shell and never reads commands from the delivery objective.
    """

    supplied = request_path.expanduser().resolve(strict=True)
    raw = read_deployment_json(supplied)
    if raw is None:
        raise ValueError("deployment helper request is absent")
    request = DeploymentHelperRequest.from_dict(raw)
    paths = deployment_paths(request.state_root, request.dispatch_id)
    if supplied != paths.request.resolve(strict=True):
        raise ValueError("deployment helper request path does not match its state root and dispatch")
    if request.installer_script.resolve(strict=True) != (
        request.source_root.resolve(strict=True) / "scripts" / "install-macos.py"
    ):
        raise ValueError("deployment helper installer is outside the fixed source checkout")
    with _exclusive_lock(paths.lock):
        existing = read_deployment_json(paths.result)
        if existing is not None:
            return existing
        started_at = _now()
        _atomic_write_json(
            paths.running,
            {
                "schema_version": _SCHEMA_VERSION,
                "dispatch_id": request.dispatch_id,
                "target": request.target,
                "pid": os.getpid(),
                "started_at": started_at,
            },
        )
        try:
            observed = identity_reader(request.source_root)
            if observed != request.source:
                result = _result_document(
                    request,
                    status="failed",
                    started_at=started_at,
                    installer={
                        "status": "not-started",
                        "reason": "source identity changed after dispatch request was recorded",
                        "expected_source": request.source.to_dict(),
                        "observed_source": observed.to_dict(),
                    },
                )
            else:
                completed = runner(
                    list(request.installer_argv),
                    cwd=str(request.source_root),
                    text=True,
                    capture_output=True,
                    timeout=request.installer_timeout_seconds,
                    check=False,
                    shell=False,
                )
                result = _result_document(
                    request,
                    status="succeeded" if completed.returncode == 0 else "failed",
                    started_at=started_at,
                    installer={
                        "status": "completed",
                        "argv": list(request.installer_argv),
                        "returncode": completed.returncode,
                        "stdout": _bounded_output(completed.stdout),
                        "stderr": _bounded_output(completed.stderr),
                    },
                )
                if completed.returncode != 0:
                    result["rollback_receipt"] = _verify_rollback(
                        request,
                        http_reader=rollback_http_reader,
                    )
        except subprocess.TimeoutExpired as error:
            result = _result_document(
                request,
                status="failed",
                started_at=started_at,
                installer={
                    "status": "failed-to-complete",
                    "reason": f"TimeoutExpired: {error}",
                    "argv": list(request.installer_argv),
                },
            )
        except OSError as error:
            result = _result_document(
                request,
                status="failed",
                started_at=started_at,
                installer={
                    "status": "failed-to-start",
                    "reason": f"OSError: {error}",
                    "argv": list(request.installer_argv),
                },
            )
        _atomic_write_json(paths.result, result)
        _remove_running(paths.running)
        return result


def main(argv: list[str] | None = None) -> int:
    """Execute a request passed by the authority adapter's fixed helper argv."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = run_deployment_helper(Path(arguments.request))
    except (OSError, ValueError) as error:
        print(f"deployment helper failed before it could record a result: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"dispatch_id": result.get("dispatch_id"), "status": result.get("status")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
