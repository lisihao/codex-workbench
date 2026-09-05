from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore
from .worktrees import WorktreeError, WorktreeManager


class DirtyWorktreeRecoveryError(WorktreeError):
    """A blocked dirty worktree cannot be resumed without losing provenance."""


@dataclass(frozen=True)
class CommandOutcome:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


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


class PnpmOfflineMaterializer:
    """Materialize a worktree-local pnpm linker without network access.

    A shared ``node_modules`` directory is deliberately not reused: pnpm's
    workspace links can otherwise resolve source imports back into another
    worktree.  The package manager's existing local store is used in offline
    mode while each worktree receives its own linker directory.
    """

    def __init__(
        self,
        *,
        binary: str = "pnpm",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.binary = binary
        self.runner = runner

    def materialize(self, worktree: Path, *, timeout_seconds: int) -> dict[str, object]:
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
        environment = os.environ.copy()
        environment.update({
            "CI": "true",
            "NO_UPDATE_NOTIFIER": "1",
            "npm_config_offline": "true",
        })
        version = self._run((binary, "--version"), worktree, environment, timeout_seconds)
        if version.exit_code != 0:
            raise DirtyWorktreeRecoveryError(
                f"pnpm version probe failed: {version.stderr.strip() or version.stdout.strip()}"
            )
        actual_version = version.stdout.strip()
        declared_version = declared.split("@", 1)[1].split("+", 1)[0]
        if actual_version.split(".", 1)[0] != declared_version.split(".", 1)[0]:
            raise DirtyWorktreeRecoveryError(
                f"pnpm major mismatch: package declares {declared_version}, authority provides {actual_version}"
            )
        install = self._run(
            (
                binary,
                "install",
                "--offline",
                "--frozen-lockfile",
                "--reporter=append-only",
            ),
            worktree,
            environment,
            timeout_seconds,
        )
        if install.exit_code != 0:
            raise DirtyWorktreeRecoveryError(
                "offline pnpm materialization failed: "
                f"{install.stderr.strip() or install.stdout.strip()}"
            )
        return {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "package_manager": declared,
            "pnpm_version": actual_version,
            "lockfile_sha256": sha256(lockfile.read_bytes()).hexdigest(),
            "commands": [version.to_dict(), install.to_dict()],
        }

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
        except (OSError, subprocess.TimeoutExpired) as error:
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
    ) -> dict[str, object]:
        path = self._validate_worktree(repository, base_sha, worktree, branch)
        changed_paths = tuple(sorted(self.worktrees.changed_paths(path, base_sha)))
        if changed_paths != tuple(sorted(expected_changed_paths)):
            raise DirtyWorktreeRecoveryError(
                "worktree changed paths do not match the blocked worker receipt"
            )
        untracked = self._git_bytes(path, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked:
            names = [name.decode("utf-8", errors="replace") for name in untracked.split(b"\0") if name]
            raise DirtyWorktreeRecoveryError(
                f"dirty worktree contains untracked files; explicit archival is required first: {', '.join(names)}"
            )
        check = self._git_text(path, "diff", "--check", base_sha)
        if check:
            raise DirtyWorktreeRecoveryError(f"dirty worktree fails git diff --check: {check}")
        patch = self._git_bytes(path, "diff", "--binary", base_sha)
        if not patch:
            raise DirtyWorktreeRecoveryError("dirty worktree has no tracked patch to preserve")
        patch_ref = self.artifacts.put_bytes(patch, "blocked-worktree.patch")
        return {
            "schema_version": 1,
            "source_attempt": attempt,
            "source_worktree": str(path),
            "source_branch": branch,
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
            "patch_ref": patch_ref,
            "patch_sha256": sha256(patch).hexdigest(),
        }

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
    ) -> RecoveryOutcome:
        """Prepare a verified recovery target without ever executing in source.

        The caller persists the prepared receipt only after this method has
        completed. Any failure therefore leaves the original blocked node and
        its source allocation authoritative.
        """

        try:
            source = self._validate_snapshot(repository, source_worktree, recovery)
            target = self._validate_target(
                repository,
                target_worktree,
                target_branch,
                target_attempt,
                recovery,
            )
            patch = self._load_patch(recovery)
            patch_path = self._patch_path(recovery)
            self.worktrees.apply_patch(target, patch_path)
            if self._git_bytes(target, "diff", "--binary", str(recovery["base_sha"])) != patch:
                raise DirtyWorktreeRecoveryError(
                    "recovery target patch does not exactly match the captured source patch"
                )
            checks = [
                "PASS: blocked dirty worktree snapshot is unchanged",
                "PASS: captured patch was applied to the clean recovery target",
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
            outcomes: list[CommandOutcome] = []
            for command_source in acceptance_commands:
                command = self._parse_command(command_source)
                outcome = self._run_command(command, target, timeout_seconds)
                outcomes.append(outcome)
                checks.append(
                    ("PASS" if outcome.exit_code == 0 else "FAIL")
                    + f": {' '.join(command)} (exit {outcome.exit_code})"
                )
                if outcome.exit_code != 0:
                    log_ref = self._store_logs(materialization, outcomes)
                    self._validate_snapshot(repository, str(source), recovery)
                    return RecoveryOutcome(
                        "failed",
                        f"declared recovery acceptance command failed: {' '.join(command)}",
                        {
                            "recovery-snapshot": str(recovery["patch_ref"]),
                            "dependency-materialization": materialization_ref,
                            "test-log": log_ref,
                        },
                        tuple(checks),
                        tuple(str(path) for path in recovery["changed_paths"]),
                        outcome.exit_code,
                    )
            if self._git_bytes(target, "diff", "--binary", str(recovery["base_sha"])) != patch:
                raise DirtyWorktreeRecoveryError(
                    "recovery target changed after offline materialization or acceptance"
                )
            self._validate_snapshot(repository, str(source), recovery)
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
        path = self._validate_worktree(repository, base_sha, worktree, branch)
        try:
            expected_source = Path(source_worktree).expanduser().resolve(strict=True)
        except OSError as error:
            raise DirtyWorktreeRecoveryError(
                "blocked recovery receipt source_worktree cannot be resolved"
            ) from error
        if path != expected_source:
            raise DirtyWorktreeRecoveryError("recovery source does not match the captured worktree")
        current_paths = tuple(sorted(self.worktrees.changed_paths(path, base_sha)))
        if current_paths != tuple(sorted(changed_paths)):
            raise DirtyWorktreeRecoveryError("dirty worktree changed paths drifted after recovery was scheduled")
        untracked = self._git_bytes(path, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked:
            raise DirtyWorktreeRecoveryError("dirty worktree gained untracked files after recovery was scheduled")
        patch = self._git_bytes(path, "diff", "--binary", base_sha)
        expected_hash = recovery["patch_sha256"]
        if not isinstance(expected_hash, str) or sha256(patch).hexdigest() != expected_hash:
            raise DirtyWorktreeRecoveryError("dirty worktree patch drifted after recovery was scheduled")
        if self._load_patch(recovery) != patch:
            raise DirtyWorktreeRecoveryError("dirty worktree no longer matches its preserved patch artifact")
        return path

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


    def _validate_worktree(self, repository: str, base_sha: str, worktree: str, branch: str) -> Path:
        repo = Path(repository).expanduser().resolve(strict=True)
        path = Path(worktree).expanduser().resolve(strict=True)
        root = self.worktrees.root.expanduser().resolve(strict=False)
        if not path.is_relative_to(root):
            raise DirtyWorktreeRecoveryError("recovery worktree is outside the Workbench worktree root")
        if self._git_text(path, "rev-parse", "--show-toplevel") != str(path):
            raise DirtyWorktreeRecoveryError("recovery path is not a standalone Git worktree")
        if self._git_text(path, "rev-parse", "HEAD") != self._git_text(repo, "rev-parse", f"{base_sha}^{{commit}}"):
            raise DirtyWorktreeRecoveryError("recovery worktree no longer matches its contract base")
        if self._git_text(path, "branch", "--show-current") != branch:
            raise DirtyWorktreeRecoveryError("recovery worktree no longer matches its allocated branch")
        return path

    @staticmethod
    def _parse_command(source: str) -> tuple[str, ...]:
        try:
            command = tuple(shlex.split(source))
        except ValueError as error:
            raise DirtyWorktreeRecoveryError(f"invalid acceptance command: {error}") from error
        if not command or any(token in {"|", "||", "&&", ";", ">", "<"} for token in command):
            raise DirtyWorktreeRecoveryError("recovery acceptance command must be an argv-only command")
        return command

    def _run_command(self, command: tuple[str, ...], cwd: Path, timeout_seconds: int) -> CommandOutcome:
        try:
            completed = self.runner(
                list(command),
                cwd=cwd,
                env={**os.environ, "CI": "true", "NO_UPDATE_NOTIFIER": "1", "npm_config_offline": "true"},
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DirtyWorktreeRecoveryError(f"cannot run recovery acceptance command {' '.join(command)}: {error}") from error
        return CommandOutcome(command, int(completed.returncode), _bounded(completed.stdout or ""), _bounded(completed.stderr or ""))

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
