"""Authority-owned local deployment and live-verification adapter.

The adapter is intentionally narrower than a general command runner.  It can
only launch this checkout's ``scripts/install-macos.py`` through a detached
helper for one configured loopback target, then verify the installed manifest
and two read-only HTTP responses.  The SQLite lifecycle remains the sole
owner of dispatches, retries, and receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import WorkbenchConfig
from .delivery_lifecycle import (
    DeliveryStageAdapter,
    DeliveryStageContext,
    DeliveryStageOutcome,
    live_verification_requirements,
)
from .deployment_helper import (
    DeploymentHelperRequest,
    SourceIdentity,
    collect_source_identity,
    deployment_paths,
    read_deployment_json,
    write_deployment_request,
)
from .model import canonical_hash
from .store import WorkbenchStore


_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class HttpObservation:
    """One bounded read-only HTTP observation."""

    status: int
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise ValueError("HTTP observation status must be an integer")
        if not isinstance(self.payload, Mapping):
            raise ValueError("HTTP observation payload must be an object")


class HttpReader(Protocol):
    """Read one configured endpoint without authentication or mutation."""

    def __call__(self, url: str, timeout_seconds: int) -> HttpObservation: ...


class HelperLauncher(Protocol):
    """Start the detached helper using the fixed argv prepared by this adapter."""

    def __call__(self, argv: list[str], source_root: Path) -> int: ...


class SourceIdentityReader(Protocol):
    """Observe the configured source checkout without executing project code."""

    def __call__(self, source_root: Path) -> SourceIdentity: ...


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty text value")
    return value.strip()


def _strict_positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _local_http_url(value: object, label: str) -> str:
    url = _required_text(value, label)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError(f"{label} must be an explicit loopback HTTP URL")
    if parsed.port is None or parsed.path not in {"/health", "/api/snapshot"}:
        raise ValueError(f"{label} must include a local port and a supported read-only path")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{label} may not include credentials, a query, or a fragment")
    return url


def _installer_arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("local_deployment.installer_arguments must be an array")
    values: list[str] = []
    for raw in value:
        argument = _required_text(raw, "local deployment installer argument")
        if argument in {"--source", "--state-root", "--dry-run"} or argument.startswith(
            ("--source=", "--state-root=", "--dry-run=")
        ):
            raise ValueError("local deployment installer arguments may not override the dispatch binding")
        values.append(argument)
    return tuple(values)


class CommitStagingProvider(Protocol):
    """Create or validate one controlled checkout of an existing local commit."""

    def prepare(self, source_root: Path, commit: str, dispatch_id: str) -> Path: ...


def _normalized_commit(value: object, label: str) -> str:
    commit = _required_text(value, label).lower()
    if len(commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full hexadecimal Git commit")
    return commit


class GitCommitStagingProvider:
    """Use a detached local Git worktree without modifying the primary checkout."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 60,
    ) -> None:
        self.runner = runner
        self.timeout_seconds = _strict_positive_int(timeout_seconds, "Git staging timeout_seconds")

    def prepare(self, source_root: Path, commit: str, dispatch_id: str) -> Path:
        primary = source_root.expanduser().resolve(strict=True)
        if not primary.is_dir():
            raise ValueError("configured source root is not a directory")
        normalized_commit = _normalized_commit(commit, "deployment source commit")
        safe_dispatch = _required_text(dispatch_id, "deployment dispatch_id")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for character in safe_dispatch):
            raise ValueError("deployment dispatch_id cannot name a staging checkout")
        if self._git(primary, "rev-parse", "--show-toplevel") != str(primary):
            raise ValueError("configured source root is not the primary Git worktree root")
        primary_common = self._common_dir(primary)
        self._git(primary, "cat-file", "-e", f"{normalized_commit}^{{commit}}")
        staging_parent = primary.parent.resolve(strict=True)
        prohibited_roots = (
            (Path.home() / "Documents").resolve(),
            (Path.home() / "Library" / "Mobile Documents").resolve(),
        )
        for prohibited in prohibited_roots:
            try:
                staging_parent.relative_to(prohibited)
            except ValueError:
                continue
            raise ValueError("deployment staging may not be created under Documents or iCloud")
        root = staging_parent / ".codex-workbench-deployment-staging"
        if root.is_symlink():
            raise ValueError("deployment staging root may not be a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        target = root / safe_dispatch
        if target.exists() or target.is_symlink():
            return self._validated_existing(primary_common, target, normalized_commit)
        result = self._run(
            ["git", "-C", str(primary), "worktree", "add", "--detach", str(target), normalized_commit]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ValueError(f"controlled deployment staging checkout could not be created: {detail[:512]}")
        return self._validated_existing(primary_common, target, normalized_commit)

    def _validated_existing(self, primary_common: Path, target: Path, commit: str) -> Path:
        if target.is_symlink() or not target.is_dir():
            raise ValueError("existing deployment staging path is not a regular directory")
        resolved = target.resolve(strict=True)
        observed_commit = _normalized_commit(self._git(resolved, "rev-parse", "HEAD"), "staging HEAD")
        if observed_commit != commit:
            raise ValueError("existing deployment staging checkout has a different commit and will not be overwritten")
        if self._common_dir(resolved) != primary_common:
            raise ValueError("existing deployment staging checkout belongs to a different Git repository")
        if self._git(resolved, "status", "--porcelain"):
            raise ValueError("existing deployment staging checkout is dirty and will not be overwritten")
        return resolved

    def _common_dir(self, worktree: Path) -> Path:
        raw = self._git(worktree, "rev-parse", "--git-common-dir")
        candidate = Path(raw)
        return (candidate if candidate.is_absolute() else worktree / candidate).resolve(strict=True)

    def _git(self, worktree: Path, *arguments: str) -> str:
        result = self._run(["git", "-C", str(worktree), *arguments])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ValueError(f"Git staging validation failed: {detail[:512]}")
        return result.stdout.strip()

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                argv,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Git staging command failed: {type(error).__name__}: {error}") from error


def _primary_git_head(source_root: Path) -> str:
    provider = GitCommitStagingProvider()
    primary = source_root.expanduser().resolve(strict=True)
    if provider._git(primary, "rev-parse", "--show-toplevel") != str(primary):
        raise ValueError("configured source root is not the primary Git worktree root")
    return _normalized_commit(provider._git(primary, "rev-parse", "HEAD"), "primary source HEAD")


@dataclass(frozen=True)
class LocalDeploymentAuthority:
    """Explicit authority configuration for one local installable target."""

    target: str
    source_root: Path
    state_root: Path
    installer_python: Path
    installer_arguments: tuple[str, ...]
    installer_timeout_seconds: int
    observation_timeout_seconds: int
    rollback_probe_timeout_seconds: int
    rollback_probe_interval_seconds: int
    health_check_name: str
    health_url: str
    functional_check_name: str
    functional_url: str

    def __post_init__(self) -> None:
        target = _required_text(self.target, "local deployment target")
        try:
            source_root = self.source_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"local deployment source root is unavailable: {type(error).__name__}: {error}") from error
        if not source_root.is_dir():
            raise ValueError("local deployment source root is not a directory")
        try:
            state_root = self.state_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"local deployment state root is unavailable: {type(error).__name__}: {error}") from error
        if not state_root.is_dir() or state_root.is_symlink():
            raise ValueError("local deployment state root must be a regular directory")
        try:
            installer_python = self.installer_python.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"local deployment Python is unavailable: {type(error).__name__}: {error}") from error
        if not installer_python.is_file() or not os.access(installer_python, os.X_OK):
            raise ValueError("local deployment Python must be an executable file")
        installer = source_root / "scripts" / "install-macos.py"
        helper = source_root / "src" / "codex_workbench" / "deployment_helper.py"
        if installer.is_symlink() or not installer.is_file():
            raise ValueError("local deployment source has no regular install-macos.py")
        if helper.is_symlink() or not helper.is_file():
            raise ValueError("local deployment source has no regular deployment helper")
        installer_arguments = _installer_arguments(self.installer_arguments)
        installer_timeout = _strict_positive_int(
            self.installer_timeout_seconds, "local deployment installer_timeout_seconds"
        )
        observation_timeout = _strict_positive_int(
            self.observation_timeout_seconds, "local deployment observation_timeout_seconds"
        )
        rollback_timeout = _strict_positive_int(
            self.rollback_probe_timeout_seconds, "local deployment rollback_probe_timeout_seconds"
        )
        rollback_interval = _strict_positive_int(
            self.rollback_probe_interval_seconds, "local deployment rollback_probe_interval_seconds"
        )
        if rollback_interval > rollback_timeout:
            raise ValueError("local deployment rollback probe interval may not exceed its timeout")
        health_name = _required_text(self.health_check_name, "local deployment health check name")
        functional_name = _required_text(
            self.functional_check_name, "local deployment functional check name"
        )
        health_url = _local_http_url(self.health_url, "local deployment health URL")
        functional_url = _local_http_url(self.functional_url, "local deployment functional URL")
        if urlsplit(health_url).path != "/health":
            raise ValueError("local deployment health URL must use /health")
        if urlsplit(functional_url).path != "/api/snapshot":
            raise ValueError("local deployment functional URL must use /api/snapshot")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "state_root", state_root)
        object.__setattr__(self, "installer_python", installer_python)
        object.__setattr__(self, "installer_arguments", installer_arguments)
        object.__setattr__(self, "installer_timeout_seconds", installer_timeout)
        object.__setattr__(self, "observation_timeout_seconds", observation_timeout)
        object.__setattr__(self, "rollback_probe_timeout_seconds", rollback_timeout)
        object.__setattr__(self, "rollback_probe_interval_seconds", rollback_interval)
        object.__setattr__(self, "health_check_name", health_name)
        object.__setattr__(self, "functional_check_name", functional_name)
        object.__setattr__(self, "health_url", health_url)
        object.__setattr__(self, "functional_url", functional_url)

    @property
    def installer_script(self) -> Path:
        """Return the only installer script this authority may invoke."""

        return self.source_root / "scripts" / "install-macos.py"

    @property
    def helper_script(self) -> Path:
        """Return the source-bound detached helper script."""

        return self.source_root / "src" / "codex_workbench" / "deployment_helper.py"

    @property
    def install_manifest(self) -> Path:
        """Return the installed artifact identity written by install-macos.py."""

        return self.state_root / "app" / "install-manifest.json"


def _default_http_reader(url: str, timeout_seconds: int) -> HttpObservation:
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL is loopback-validated configuration.
        payload = json.loads(response.read().decode("utf-8"))
        return HttpObservation(status=int(response.status), payload=payload)


def _default_helper_launcher(argv: list[str], source_root: Path) -> int:
    process = subprocess.Popen(
        argv,
        cwd=str(source_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        shell=False,
    )
    return process.pid


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _now() -> datetime:
    return datetime.now(UTC)


class LocalDeploymentStageAdapter:
    """Execute only deploy/live-verify for one registered loopback target."""

    def __init__(
        self,
        authority: LocalDeploymentAuthority,
        *,
        source_identity_reader: SourceIdentityReader = collect_source_identity,
        staging_provider: CommitStagingProvider | None = None,
        source_commit_reader: Callable[[Path], str] = _primary_git_head,
        helper_launcher: HelperLauncher = _default_helper_launcher,
        http_reader: HttpReader = _default_http_reader,
        pid_alive: Callable[[int], bool] = _default_pid_alive,
        clock: Callable[[], datetime] = _now,
    ):
        self.authority = authority
        self.source_identity_reader = source_identity_reader
        self.staging_provider = staging_provider or GitCommitStagingProvider()
        self.source_commit_reader = source_commit_reader
        self.helper_launcher = helper_launcher
        self.http_reader = http_reader
        self.pid_alive = pid_alive
        self.clock = clock

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        """Start one detached deploy helper or perform read-only live verification."""

        if context.stage == "deploy":
            return self._execute_deploy(context)
        if context.stage == "live-verify":
            return self._live_verify(context)
        raise ValueError("local deployment adapter only handles deploy and live-verify")

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        """Observe only existing helper records, manifests, and HTTP responses."""

        if context.stage == "deploy":
            return self._observe_deploy(context)
        if context.stage == "live-verify":
            return self._live_verify(context)
        raise ValueError("local deployment adapter only handles deploy and live-verify")

    def rollback_deployment(
        self,
        context: DeliveryStageContext,
        _outcome: DeliveryStageOutcome,
    ) -> Mapping[str, Any]:
        """Return only a helper-persisted, independently observed rollback receipt.

        This method does not rerun an installer, restore files, or infer success
        from a nonzero exit.  A false or malformed receipt remains explicitly
        unverified so the lifecycle can preserve an indeterminate deployment.
        """

        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        try:
            result = read_deployment_json(paths.result)
        except ValueError as error:
            return {
                "verified": False,
                "dispatch_id": context.dispatch_id,
                "result_path": str(paths.result),
                "reason": f"deployment helper result is unreadable: {type(error).__name__}: {error}",
            }
        receipt = result.get("rollback_receipt") if isinstance(result, Mapping) else None
        if not isinstance(receipt, Mapping):
            return {
                "verified": False,
                "dispatch_id": context.dispatch_id,
                "result_path": str(paths.result),
                "reason": "deployment helper has no independent rollback recovery receipt",
            }
        baseline = receipt.get("baseline")
        observed_manifest = receipt.get("observed_manifest")
        checks = receipt.get("checks")
        health = checks.get("health") if isinstance(checks, Mapping) else None
        functional = checks.get("functional") if isinstance(checks, Mapping) else None
        pre_epoch = baseline.get("coordinator_epoch") if isinstance(baseline, Mapping) else None
        health_epoch = health.get("coordinator_epoch") if isinstance(health, Mapping) else None
        functional_epoch = functional.get("coordinator_epoch") if isinstance(functional, Mapping) else None
        health_instance = health.get("coordinator_instance_id") if isinstance(health, Mapping) else None
        functional_instance = (
            functional.get("coordinator_instance_id") if isinstance(functional, Mapping) else None
        )
        independently_verified = (
            receipt.get("verified") is True
            and isinstance(baseline, Mapping)
            and baseline.get("eligible") is True
            and isinstance(observed_manifest, Mapping)
            and observed_manifest == baseline.get("manifest")
            and isinstance(health, Mapping)
            and health.get("status") == "passed"
            and isinstance(functional, Mapping)
            and functional.get("status") == "passed"
            and isinstance(pre_epoch, int)
            and not isinstance(pre_epoch, bool)
            and isinstance(health_epoch, int)
            and not isinstance(health_epoch, bool)
            and health_epoch > pre_epoch
            and functional_epoch == health_epoch
            and isinstance(health_instance, str)
            and health_instance == functional_instance
        )
        if independently_verified:
            return dict(receipt)
        return {
            "verified": False,
            "dispatch_id": context.dispatch_id,
            "result_path": str(paths.result),
            "reason": "rollback recovery receipt is incomplete or not independently verified",
            "observed_receipt": dict(receipt),
        }

    def _execute_deploy(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        authorization_problem = self._authorization_problem(context)
        if authorization_problem is not None:
            return self._denied(context, authorization_problem)
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        try:
            existing = read_deployment_json(paths.request)
        except ValueError as error:
            return self._indeterminate(context, "existing deployment request is unreadable", error)
        if existing is not None:
            return self._observe_deploy(context)
        expected_commit = self._expected_commit(context)
        try:
            deployment_commit = expected_commit or _normalized_commit(
                self.source_commit_reader(self.authority.source_root),
                "primary source HEAD",
            )
            staged_source_root = self.staging_provider.prepare(
                self.authority.source_root,
                deployment_commit,
                context.dispatch_id,
            )
            source = self.source_identity_reader(staged_source_root)
        except Exception as error:
            return self._blocked(
                context,
                "commit-pinned deployment staging could not be prepared before installer dispatch",
                {"error": f"{type(error).__name__}: {error}"},
            )
        if source.commit != deployment_commit:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:deployment-staging-mismatch",
                status="failed",
                receipt={
                    "target": self.authority.target,
                    "expected_commit": deployment_commit,
                    "staging_source_root": str(staged_source_root),
                    "observed_source": source.to_dict(),
                    "external_action": "not_started",
                },
                failure={
                    "kind": "verification-failure",
                    "detail": "controlled deployment staging checkout does not match the accepted delivery commit",
                },
                retry_eligible=False,
            )
        pre_install_epoch = self._pre_install_coordinator_epoch(context)
        if pre_install_epoch is None:
            return self._blocked(
                context,
                "deployment dispatch has no positive pre-install coordinator epoch to fence restart verification",
            )
        rollback_baseline = self._capture_rollback_baseline()
        rollback_probe = {
            "health_url": self.authority.health_url,
            "functional_url": self.authority.functional_url,
            "timeout_seconds": self.authority.rollback_probe_timeout_seconds,
            "interval_seconds": self.authority.rollback_probe_interval_seconds,
        }
        request = DeploymentHelperRequest(
            dispatch_id=context.dispatch_id,
            target=self.authority.target,
            source_root=staged_source_root,
            trusted_source_root=self.authority.source_root,
            state_root=self.authority.state_root,
            installer_python=self.authority.installer_python,
            installer_script=staged_source_root / "scripts" / "install-macos.py",
            installer_arguments=self.authority.installer_arguments,
            installer_timeout_seconds=self.authority.installer_timeout_seconds,
            source=source,
            pre_install_coordinator_epoch=pre_install_epoch,
            rollback_baseline=rollback_baseline,
            rollback_probe=rollback_probe,
            created_at=self.clock().isoformat(timespec="seconds"),
        )
        try:
            request_path = write_deployment_request(request)
        except (OSError, ValueError) as error:
            return self._blocked(
                context,
                "deployment request could not be persisted before helper launch",
                {"error": f"{type(error).__name__}: {error}"},
            )
        helper_argv = [
            str(self.authority.installer_python),
            str(staged_source_root / "src" / "codex_workbench" / "deployment_helper.py"),
            "--request",
            str(request_path),
        ]
        try:
            pid = self.helper_launcher(helper_argv, staged_source_root)
        except Exception as error:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:deployment-helper-start",
                status="failed",
                receipt={
                    "target": self.authority.target,
                    "request_path": str(request_path),
                    "external_action": "not_started",
                },
                failure={
                    "kind": "execution-environment",
                    "detail": f"detached deployment helper could not start: {type(error).__name__}: {error}",
                },
                retry_eligible=True,
            )
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return self._indeterminate(
                context,
                "detached deployment helper returned no usable process identifier",
            )
        return self._deferred(
            context,
            "detached deployment helper was launched; await its atomic result record",
            {"request_path": str(request_path), "helper_pid": pid},
        )

    def _observe_deploy(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        authorization_problem = self._authorization_problem(context)
        if authorization_problem is not None:
            return self._denied(context, authorization_problem)
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        try:
            raw_request = read_deployment_json(paths.request)
        except ValueError as error:
            return self._indeterminate(context, "deployment request is unreadable during reconciliation", error)
        if raw_request is None:
            return self._indeterminate(
                context,
                "no dispatch-bound helper request exists after a durable deployment dispatch",
            )
        try:
            request = DeploymentHelperRequest.from_dict(raw_request)
        except ValueError as error:
            return self._indeterminate(context, "deployment request is malformed during reconciliation", error)
        if (
            request.dispatch_id != context.dispatch_id
            or request.target != self.authority.target
            or request.trusted_source_root != self.authority.source_root
            or request.state_root != self.authority.state_root
        ):
            return self._indeterminate(context, "deployment request does not bind this dispatch and local target")
        expected_commit = self._expected_commit(context)
        pre_install_epoch = self._pre_install_coordinator_epoch(context)
        if (
            pre_install_epoch is None
            or request.pre_install_coordinator_epoch != pre_install_epoch
            or (expected_commit is not None and request.source.commit != expected_commit)
        ):
            return self._indeterminate(
                context,
                "deployment request does not match the current fenced source identity and coordinator epoch",
            )
        try:
            raw_result = read_deployment_json(paths.result)
        except ValueError as error:
            return self._indeterminate(context, "deployment helper result is unreadable", error)
        if raw_result is not None:
            return self._deployment_result_outcome(context, request, raw_result, paths.result)
        try:
            running = read_deployment_json(paths.running)
        except ValueError as error:
            return self._indeterminate(context, "deployment helper running record is unreadable", error)
        if self._running_record_matches(running, context.dispatch_id) and self.pid_alive(int(running["pid"])):
            return self._deferred(
                context,
                "detached deployment helper is still running; no failure attempt is consumed",
                {"running": dict(running or {}), "request_path": str(paths.request)},
            )
        return self._indeterminate(
            context,
            "deployment helper disappeared without an atomic result record; installer effects are unknown",
        )

    def _deployment_result_outcome(
        self,
        context: DeliveryStageContext,
        request: DeploymentHelperRequest,
        raw_result: Mapping[str, Any],
        result_path: Path,
    ) -> DeliveryStageOutcome:
        if (
            raw_result.get("schema_version") != 1
            or raw_result.get("dispatch_id") != context.dispatch_id
            or raw_result.get("target") != self.authority.target
            or raw_result.get("status") not in {"succeeded", "failed"}
            or not isinstance(raw_result.get("installer"), Mapping)
        ):
            return self._indeterminate(context, "deployment helper result is not a valid dispatch-bound receipt")
        try:
            observed_source = SourceIdentity.from_dict(raw_result.get("source"))
        except ValueError as error:
            return self._indeterminate(context, "deployment helper result has an invalid source identity", error)
        if observed_source != request.source:
            return self._indeterminate(context, "deployment helper result source differs from its frozen request")
        receipt = {
            "target": self.authority.target,
            "request_path": str(deployment_paths(self.authority.state_root, context.dispatch_id).request),
            "result_path": str(result_path),
            "trusted_source_root": str(request.trusted_source_root),
            "staging_source_root": str(request.source_root),
            "source": request.source.to_dict(),
            "installer": dict(raw_result["installer"]),
            "helper_status": raw_result["status"],
            "rollback_receipt": (
                dict(raw_result["rollback_receipt"])
                if isinstance(raw_result.get("rollback_receipt"), Mapping)
                else None
            ),
        }
        if raw_result["status"] != "succeeded":
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:deployment-installer-failed",
                status="failed",
                receipt=receipt,
                failure={
                    "kind": "execution-environment",
                    "detail": "the detached installer recorded a non-successful result",
                },
                retry_eligible=True,
            )
        deploy_identity = {
            "target": self.authority.target,
            "dispatch_id": context.dispatch_id,
            "request_fingerprint": canonical_hash(request.to_dict()),
            "commit": request.source.commit,
            "version": request.source.version,
            "source_manifest": request.source.to_dict()["manifest"],
            "pre_install_coordinator_epoch": request.pre_install_coordinator_epoch,
            "trusted_source_root": str(request.trusted_source_root),
            "staging_source_root": str(request.source_root),
        }
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:deployment-installed",
            receipt=receipt,
            evidence_fingerprint=canonical_hash(
                {
                    "stage": context.stage,
                    "dispatch_id": context.dispatch_id,
                    "deployment_request": request.to_dict(),
                    "deployment_result": dict(raw_result),
                }
            ),
            identities={"deploy": deploy_identity},
        )

    def _live_verify(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        authorization_problem = self._authorization_problem(context)
        if authorization_problem is not None:
            return self._denied(context, authorization_problem)
        requirements_problem = self._requirements_problem(context)
        if requirements_problem is not None:
            return self._blocked(context, requirements_problem)
        deploy_identity = self._deploy_identity(context)
        if deploy_identity is None:
            return self._indeterminate(
                context,
                "live verification cannot bind to a prior successful deployment helper receipt",
            )
        prior_dispatch = deploy_identity["dispatch_id"]
        paths = deployment_paths(self.authority.state_root, prior_dispatch)
        try:
            raw_request = read_deployment_json(paths.request)
            raw_result = read_deployment_json(paths.result)
        except ValueError as error:
            return self._indeterminate(context, "prior deployment evidence is unreadable", error)
        if raw_request is None or raw_result is None:
            return self._indeterminate(
                context,
                "prior deployment helper request or result is absent; installed artifact identity is unknown",
            )
        try:
            request = DeploymentHelperRequest.from_dict(raw_request)
            result_source = SourceIdentity.from_dict(raw_result.get("source"))
        except ValueError as error:
            return self._indeterminate(context, "prior deployment evidence is malformed", error)
        if (
            raw_result.get("status") != "succeeded"
            or request.target != self.authority.target
            or result_source != request.source
            or deploy_identity["request_fingerprint"] != canonical_hash(request.to_dict())
            or deploy_identity["commit"] != request.source.commit
            or deploy_identity["version"] != request.source.version
            or deploy_identity["source_manifest"] != request.source.to_dict()["manifest"]
            or deploy_identity["pre_install_coordinator_epoch"]
            != request.pre_install_coordinator_epoch
        ):
            return self._indeterminate(
                context,
                "prior deployment evidence does not prove the artifact being live was installed by this dispatch",
            )
        try:
            manifest = read_deployment_json(self.authority.install_manifest)
        except ValueError as error:
            return self._failed_live(context, request, "installed manifest is unreadable", error)
        if manifest is None:
            return self._failed_live(context, request, "installed manifest is absent")
        manifest_identity = {
            "commit": manifest.get("commit"),
            "version": manifest.get("version"),
        }
        if manifest_identity != {"commit": request.source.commit, "version": request.source.version}:
            return self._failed_live(
                context,
                request,
                "installed manifest does not match the dispatch-bound source version",
                {"observed_manifest": manifest_identity},
            )
        try:
            health = self.http_reader(self.authority.health_url, self.authority.observation_timeout_seconds)
            functional = self.http_reader(
                self.authority.functional_url, self.authority.observation_timeout_seconds
            )
        except Exception as error:
            return self._failed_live(context, request, "read-only runtime probe failed", error)
        health_check = self._runtime_check(
            health,
            request.source,
            require_ok=True,
            minimum_epoch=request.pre_install_coordinator_epoch,
            authority_under_health=False,
        )
        functional_check = self._runtime_check(
            functional,
            request.source,
            require_ok=False,
            minimum_epoch=request.pre_install_coordinator_epoch,
            authority_under_health=True,
        )
        if (
            health_check.get("status") == "passed"
            and functional_check.get("status") == "passed"
            and (
                health_check.get("coordinator_epoch") != functional_check.get("coordinator_epoch")
                or health_check.get("coordinator_instance_id")
                != functional_check.get("coordinator_instance_id")
            )
        ):
            functional_check = {
                **functional_check,
                "status": "failed",
                "reason": "health and functional probes observed different coordinator instances",
            }
        receipt = {
            "target": self.authority.target,
            "deployment_dispatch_id": prior_dispatch,
            "expected_source": request.source.to_dict(),
            "installed_manifest": {"path": str(self.authority.install_manifest), **manifest_identity},
            "checks": {
                "health": {self.authority.health_check_name: health_check},
                "functional": {self.authority.functional_check_name: functional_check},
            },
        }
        failed = [
            check
            for check in (health_check, functional_check)
            if check.get("status") != "passed"
        ]
        if failed:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:live-verification-failed",
                status="failed",
                receipt=receipt,
                failure={
                    "kind": "verification-failure",
                    "detail": "installed runtime did not produce matching HTTP 200 source identity evidence",
                },
                retry_eligible=True,
            )
        runtime_identity = {
            "target": self.authority.target,
            "commit": request.source.commit,
            "version": request.source.version,
            "health_url": self.authority.health_url,
            "functional_url": self.authority.functional_url,
            "coordinator_epoch": health_check["coordinator_epoch"],
            "coordinator_instance_id": health_check["coordinator_instance_id"],
        }
        requested = context.objective.get("identities")
        expected_runtime = requested.get("runtime") if isinstance(requested, Mapping) else None
        if expected_runtime is not None and expected_runtime != runtime_identity:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:runtime-identity-mismatch",
                status="failed",
                receipt={**receipt, "expected_runtime": expected_runtime},
                failure={
                    "kind": "verification-failure",
                    "detail": "observed runtime identity differs from the frozen delivery objective",
                },
                retry_eligible=False,
            )
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:live-verified",
            receipt=receipt,
            evidence_fingerprint=canonical_hash(
                {
                    "stage": context.stage,
                    "dispatch_id": context.dispatch_id,
                    "deployment_dispatch_id": prior_dispatch,
                    "receipt": receipt,
                }
            ),
            identities={"runtime": runtime_identity},
        )

    def _authorization_problem(self, context: DeliveryStageContext) -> str | None:
        deployment = self._deployment_endpoint(context)
        if deployment is None:
            return "delivery objective has no explicit deployment endpoint"
        if deployment.get("target") != self.authority.target:
            return "delivery objective target is not registered for this local authority"
        authorization = context.authorization
        scope = authorization.get("scope") if isinstance(authorization, Mapping) else None
        authorized = scope.get("deployment") if isinstance(scope, Mapping) else None
        if not isinstance(authorized, Mapping) or authorized.get("target") != self.authority.target:
            return "delivery stage lacks a matching scoped deployment authorization"
        return None

    @staticmethod
    def _deployment_endpoint(context: DeliveryStageContext) -> Mapping[str, Any] | None:
        endpoints = context.objective.get("requested_endpoints")
        deployment = endpoints.get("deployment") if isinstance(endpoints, Mapping) else None
        return deployment if isinstance(deployment, Mapping) else None

    def _requirements_problem(self, context: DeliveryStageContext) -> str | None:
        try:
            requirements = live_verification_requirements(context.objective)
        except ValueError as error:
            return f"live verification requirements are invalid: {error}"
        if requirements.get("health") != (self.authority.health_check_name,):
            return "delivery health check does not match the registered read-only health probe"
        if requirements.get("functional") != (self.authority.functional_check_name,):
            return "delivery functional check does not match the registered read-only functional probe"
        return None

    @staticmethod
    def _expected_commit(context: DeliveryStageContext) -> str | None:
        identities = context.objective.get("identities")
        if not isinstance(identities, Mapping):
            return None
        candidate = identities.get("build", identities.get("source"))
        if isinstance(candidate, Mapping):
            candidate = candidate.get("integration_commit", candidate.get("commit"))
        if not isinstance(candidate, str):
            return None
        normalized = candidate.lower()
        if len(normalized) not in {40, 64} or any(character not in "0123456789abcdef" for character in normalized):
            return None
        return normalized

    def _capture_rollback_baseline(self) -> dict[str, Any]:
        try:
            manifest = read_deployment_json(self.authority.install_manifest)
        except ValueError as error:
            return {"eligible": False, "reason": f"installed manifest is unreadable: {type(error).__name__}: {error}"}
        if not isinstance(manifest, Mapping):
            return {"eligible": False, "reason": "no installed manifest exists before deployment"}
        commit = manifest.get("commit")
        version = manifest.get("version")
        if not isinstance(commit, str) or not isinstance(version, str) or not commit.strip() or not version.strip():
            return {"eligible": False, "reason": "installed manifest has no exact commit and version"}
        normalized_commit = commit.lower()
        if len(normalized_commit) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in normalized_commit
        ):
            return {"eligible": False, "reason": "installed manifest commit is invalid"}
        try:
            health = self.http_reader(self.authority.health_url, self.authority.observation_timeout_seconds)
            functional = self.http_reader(
                self.authority.functional_url, self.authority.observation_timeout_seconds
            )
        except Exception as error:
            return {
                "eligible": False,
                "manifest": {"commit": normalized_commit, "version": version},
                "reason": f"pre-install loopback observation failed: {type(error).__name__}: {error}",
            }
        health_check = self._baseline_runtime_check(
            health,
            commit=normalized_commit,
            version=version,
            require_ok=True,
            authority_under_health=False,
        )
        functional_check = self._baseline_runtime_check(
            functional,
            commit=normalized_commit,
            version=version,
            require_ok=False,
            authority_under_health=True,
        )
        same_instance = (
            health_check.get("coordinator_epoch") == functional_check.get("coordinator_epoch")
            and health_check.get("coordinator_instance_id")
            == functional_check.get("coordinator_instance_id")
        )
        eligible = (
            health_check.get("status") == "passed"
            and functional_check.get("status") == "passed"
            and same_instance
        )
        baseline = {
            "eligible": eligible,
            "manifest": {"commit": normalized_commit, "version": version},
            "health": health_check,
            "functional": functional_check,
        }
        if eligible:
            baseline["coordinator_epoch"] = health_check["coordinator_epoch"]
            baseline["coordinator_instance_id"] = health_check["coordinator_instance_id"]
        else:
            baseline["reason"] = "pre-install manifest and runtime identities are not one healthy instance"
        return baseline

    @staticmethod
    def _baseline_runtime_check(
        observation: HttpObservation,
        *,
        commit: str,
        version: str,
        require_ok: bool,
        authority_under_health: bool,
    ) -> dict[str, Any]:
        build = observation.payload.get("build")
        health = observation.payload.get("health")
        authority = (
            health.get("authority")
            if authority_under_health and isinstance(health, Mapping)
            else observation.payload.get("authority")
        )
        identity = {
            "version": observation.payload.get("version"),
            "build_commit": build.get("commit") if isinstance(build, Mapping) else None,
            "build_version": build.get("version") if isinstance(build, Mapping) else None,
            "coordinator_epoch": authority.get("coordinator_epoch") if isinstance(authority, Mapping) else None,
            "coordinator_instance_id": authority.get("instance_id") if isinstance(authority, Mapping) else None,
        }
        passed = (
            observation.status == 200
            and (not require_ok or observation.payload.get("ok") is True)
            and identity["version"] == version
            and identity["build_commit"] == commit
            and identity["build_version"] == version
            and isinstance(authority, Mapping)
            and authority.get("active") is True
            and isinstance(identity["coordinator_epoch"], int)
            and not isinstance(identity["coordinator_epoch"], bool)
            and identity["coordinator_epoch"] > 0
            and isinstance(identity["coordinator_instance_id"], str)
            and bool(identity["coordinator_instance_id"].strip())
        )
        return {"status": "passed" if passed else "failed", "http_status": observation.status, **identity}

    @staticmethod
    def _pre_install_coordinator_epoch(context: DeliveryStageContext) -> int | None:
        lease = context.objective.get("lease")
        epoch = lease.get("coordinator_epoch") if isinstance(lease, Mapping) else None
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
            return None
        return epoch

    @staticmethod
    def _running_record_matches(value: Mapping[str, Any] | None, dispatch_id: str) -> bool:
        return (
            isinstance(value, Mapping)
            and value.get("schema_version") == 1
            and value.get("dispatch_id") == dispatch_id
            and isinstance(value.get("pid"), int)
            and not isinstance(value.get("pid"), bool)
            and value["pid"] > 0
        )

    @staticmethod
    def _deploy_identity(context: DeliveryStageContext) -> dict[str, Any] | None:
        identities = context.objective.get("identities")
        raw = identities.get("deploy") if isinstance(identities, Mapping) else None
        if not isinstance(raw, Mapping):
            return None
        dispatch_id = raw.get("dispatch_id")
        fingerprint = raw.get("request_fingerprint")
        commit = raw.get("commit")
        version = raw.get("version")
        manifest = raw.get("source_manifest")
        pre_install_epoch = raw.get("pre_install_coordinator_epoch")
        if (
            not isinstance(dispatch_id, str)
            or not isinstance(fingerprint, str)
            or not isinstance(commit, str)
            or not isinstance(version, str)
            or not isinstance(manifest, Mapping)
            or not isinstance(pre_install_epoch, int)
            or isinstance(pre_install_epoch, bool)
            or pre_install_epoch <= 0
        ):
            return None
        return {
            "dispatch_id": dispatch_id,
            "request_fingerprint": fingerprint,
            "commit": commit,
            "version": version,
            "source_manifest": dict(manifest),
            "pre_install_coordinator_epoch": pre_install_epoch,
        }

    @staticmethod
    def _runtime_check(
        observation: HttpObservation,
        source: SourceIdentity,
        *,
        require_ok: bool,
        minimum_epoch: int,
        authority_under_health: bool,
    ) -> dict[str, Any]:
        build = observation.payload.get("build")
        health = observation.payload.get("health")
        authority = (
            health.get("authority")
            if authority_under_health and isinstance(health, Mapping)
            else observation.payload.get("authority")
        )
        identity = {
            "version": observation.payload.get("version"),
            "build_commit": build.get("commit") if isinstance(build, Mapping) else None,
            "build_version": build.get("version") if isinstance(build, Mapping) else None,
            "coordinator_epoch": (
                authority.get("coordinator_epoch") if isinstance(authority, Mapping) else None
            ),
            "coordinator_instance_id": (
                authority.get("instance_id") if isinstance(authority, Mapping) else None
            ),
        }
        passed = (
            observation.status == 200
            and (not require_ok or observation.payload.get("ok") is True)
            and identity["version"] == source.version
            and identity["build_commit"] == source.commit
            and identity["build_version"] == source.version
            and isinstance(authority, Mapping)
            and authority.get("active") is True
            and isinstance(identity["coordinator_epoch"], int)
            and not isinstance(identity["coordinator_epoch"], bool)
            and identity["coordinator_epoch"] > minimum_epoch
            and isinstance(identity["coordinator_instance_id"], str)
            and bool(identity["coordinator_instance_id"].strip())
        )
        return {
            "status": "passed" if passed else "failed",
            "http_status": observation.status,
            **identity,
        }

    def _deferred(
        self,
        context: DeliveryStageContext,
        detail: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> DeliveryStageOutcome:
        wakeup = self.clock() + timedelta(seconds=self.authority.observation_timeout_seconds)
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:deployment-deferred",
            status="deferred",
            receipt={"target": self.authority.target, **dict(receipt or {})},
            failure={"kind": "execution-environment", "detail": detail},
            retry_eligible=True,
            next_wakeup_at=wakeup.isoformat(timespec="seconds"),
        )

    def _blocked(
        self,
        context: DeliveryStageContext,
        detail: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:deployment-blocked",
            status="blocked",
            receipt={"target": self.authority.target, **dict(receipt or {})},
            failure={"kind": "execution-environment", "detail": detail},
            retry_eligible=False,
        )

    def _denied(self, context: DeliveryStageContext, detail: str) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:deployment-authorization",
            status="denied",
            receipt={"target": self.authority.target, "external_action": "not_started"},
            failure={"kind": "permission-denied", "detail": detail},
            retry_eligible=False,
        )

    def _indeterminate(
        self,
        context: DeliveryStageContext,
        detail: str,
        error: Exception | None = None,
    ) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:deployment-indeterminate",
            status="indeterminate",
            receipt={
                "target": self.authority.target,
                **(
                    {"error": f"{type(error).__name__}: {error}"}
                    if error is not None
                    else {}
                ),
            },
            failure={"kind": "unknown-effects", "detail": detail},
            retry_eligible=False,
        )

    def _failed_live(
        self,
        context: DeliveryStageContext,
        request: DeploymentHelperRequest,
        detail: str,
        observation: object | None = None,
    ) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:live-verification-failed",
            status="failed",
            receipt={
                "target": self.authority.target,
                "expected_source": request.source.to_dict(),
                **({"observation": str(observation)} if observation is not None else {}),
            },
            failure={"kind": "verification-failure", "detail": detail},
            retry_eligible=True,
        )


def _load_local_deployment_authority(config: WorkbenchConfig) -> LocalDeploymentAuthority | None:
    try:
        raw = json.loads(config.config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    local = raw.get("local_deployment") if isinstance(raw, Mapping) else None
    if not isinstance(local, Mapping) or local.get("schema_version") != 1:
        return None
    health = local.get("health")
    functional = local.get("functional")
    if not isinstance(health, Mapping) or not isinstance(functional, Mapping):
        return None
    try:
        return LocalDeploymentAuthority(
            target=_required_text(local.get("target"), "local_deployment.target"),
            source_root=Path(_required_text(local.get("source_root"), "local_deployment.source_root")),
            state_root=config.state_root,
            installer_python=Path(
                _required_text(local.get("installer_python"), "local_deployment.installer_python")
            ),
            installer_arguments=_installer_arguments(local.get("installer_arguments")),
            installer_timeout_seconds=_strict_positive_int(
                local.get("installer_timeout_seconds"), "local_deployment.installer_timeout_seconds"
            ),
            observation_timeout_seconds=_strict_positive_int(
                local.get("observation_timeout_seconds"),
                "local_deployment.observation_timeout_seconds",
            ),
            rollback_probe_timeout_seconds=_strict_positive_int(
                local.get("rollback_probe_timeout_seconds"),
                "local_deployment.rollback_probe_timeout_seconds",
            ),
            rollback_probe_interval_seconds=_strict_positive_int(
                local.get("rollback_probe_interval_seconds"),
                "local_deployment.rollback_probe_interval_seconds",
            ),
            health_check_name=_required_text(health.get("name"), "local_deployment.health.name"),
            health_url=_local_http_url(health.get("url"), "local_deployment.health.url"),
            functional_check_name=_required_text(
                functional.get("name"), "local_deployment.functional.name"
            ),
            functional_url=_local_http_url(functional.get("url"), "local_deployment.functional.url"),
        )
    except (TypeError, ValueError):
        return None


def build_authority_deployment_stage_adapter(
    store: WorkbenchStore,
    config: WorkbenchConfig,
) -> DeliveryStageAdapter | None:
    """Build an adapter only for an explicit authority-local target.

    ``store`` remains part of the factory signature so the lifecycle composer
    owns the single durable authority.  This factory intentionally does not
    create a database, infer a target, or use a request-supplied command.
    ``None`` lets the caller retain its explicit unavailable-stage adapter.
    """

    del store
    if config.deployment_role != "authority" or config.authority_host != socket.gethostname():
        return None
    authority = _load_local_deployment_authority(config)
    return LocalDeploymentStageAdapter(authority) if authority is not None else None


__all__ = [
    "HttpObservation",
    "LocalDeploymentAuthority",
    "LocalDeploymentStageAdapter",
    "build_authority_deployment_stage_adapter",
]
