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
from .dependency_inputs import (
    DependencyInputError,
    apply_recorded_dependency_input,
    changed_paths_since_input_tree,
)
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

    # pnpm 11.7 can wait on registry-backed supply-chain verification even
    # when `--offline` is present.  11.25 is the first authority runtime we
    # have verified to fail fast from the local cache instead.  Recovery must
    # never turn that upstream behavior into an unbounded worker lease.
    MINIMUM_PNPM_11_VERSION = (11, 25, 0)
    MAX_MATERIALIZATION_SECONDS = 120
    BINARY_ENVIRONMENT_VARIABLE = "CODEX_WORKBENCH_PNPM"
    STORE_ENVIRONMENT_VARIABLE = "CODEX_WORKBENCH_PNPM_STORE"

    def __init__(
        self,
        *,
        binary: str | None = None,
        store_dir: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.binary = binary or os.environ.get(self.BINARY_ENVIRONMENT_VARIABLE, "pnpm")
        configured_store = os.environ.get(self.STORE_ENVIRONMENT_VARIABLE)
        self.store_dir = store_dir or (Path(configured_store).expanduser() if configured_store else None)
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
        effective_timeout = min(timeout_seconds, self.MAX_MATERIALIZATION_SECONDS)
        if effective_timeout <= 0:
            raise DirtyWorktreeRecoveryError("pnpm recovery timeout must be positive")
        environment = os.environ.copy()
        environment.update({
            "CI": "true",
            "NO_UPDATE_NOTIFIER": "1",
            "npm_config_offline": "true",
            # pnpm 11 otherwise verifies release-age attestations against the
            # registry even when installation itself is declared offline. A
            # recovery must either use the local store or fail immediately.
            "npm_config_minimum_release_age": "0",
        })
        version = self._run((binary, "--version"), worktree, environment, effective_timeout)
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
        install_command: tuple[str, ...] = (
            binary,
            "install",
            "--offline",
            "--frozen-lockfile",
            "--config.minimumReleaseAge=0",
            "--reporter=append-only",
        )
        if self.store_dir is not None:
            store_dir = self.store_dir.resolve(strict=False)
            if not store_dir.is_dir():
                raise DirtyWorktreeRecoveryError(
                    f"configured pnpm store is unavailable: {store_dir}"
                )
            install_command += ("--store-dir", str(store_dir))
        install = self._run(
            install_command,
            worktree,
            environment,
            effective_timeout,
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
            "materialization_timeout_seconds": effective_timeout,
            "store_dir": str(self.store_dir.resolve(strict=False)) if self.store_dir else None,
            "commands": [version.to_dict(), install.to_dict()],
        }

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
    ) -> dict[str, object]:
        """Capture only the blocked worker's own patch.

        A dependent worker starts from a materialized ancestor tree rather
        than the contract commit. Its recovery receipt pins that exact input
        artifact and calculates the worker delta from that tree, so accepted
        ancestor patches never become part of the worker's patch.
        """

        path = self._validate_worktree(repository, base_sha, worktree, branch)
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
        changed_paths = tuple(sorted(changed_paths_since_input_tree(path, comparison_tree)))
        if changed_paths != tuple(sorted(expected_changed_paths)):
            raise DirtyWorktreeRecoveryError(
                "worktree changed paths do not match the blocked worker receipt"
            )
        untracked_paths = self.untracked_paths(path)
        requested_untracked = tuple(sorted(preserve_untracked_paths))
        if untracked_paths:
            if dependency_input_ref is None:
                raise DirtyWorktreeRecoveryError(
                    "preserving untracked recovery files requires a recorded dependency input"
                )
            if requested_untracked != untracked_paths:
                raise DirtyWorktreeRecoveryError(
                    "dirty worktree contains untracked files; pass the exact paths through explicit preservation: "
                    + ", ".join(untracked_paths)
                )
            recovery_context = {
                **recovery_context,
                "schema_version": 3,
                "untracked_paths": list(untracked_paths),
            }
        elif requested_untracked:
            raise DirtyWorktreeRecoveryError(
                "explicit untracked preservation paths no longer match the dirty worktree"
            )
        check = self._git_text(path, "diff", "--check", comparison_tree)
        if check:
            raise DirtyWorktreeRecoveryError(f"dirty worktree fails git diff --check: {check}")
        patch = self.captured_patch(path, comparison_tree, untracked_paths)
        if not patch:
            raise DirtyWorktreeRecoveryError("dirty worktree has no patch to preserve")
        patch_ref = self.artifacts.put_bytes(patch, "blocked-worktree.patch")
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
            if self._git_bytes(target, "diff", "--binary", comparison_tree) != patch:
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
        comparison_tree, _, _, _ = self._recovery_input_context(path, recovery)
        current_paths = tuple(sorted(changed_paths_since_input_tree(path, comparison_tree)))
        if current_paths != tuple(sorted(changed_paths)):
            raise DirtyWorktreeRecoveryError("dirty worktree changed paths drifted after recovery was scheduled")
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

        schema_version = recovery.get("schema_version")
        base_sha = recovery.get("base_sha")
        if not isinstance(base_sha, str) or not base_sha:
            raise DirtyWorktreeRecoveryError("blocked recovery receipt has invalid base_sha")
        if schema_version == 1:
            legacy = {
                "schema_version",
                "source_attempt",
                "source_worktree",
                "source_branch",
                "base_sha",
                "changed_paths",
                "patch_ref",
                "patch_sha256",
            }
            if set(recovery) != legacy:
                raise DirtyWorktreeRecoveryError("blocked legacy recovery receipt has an invalid shape")
            return (
                self._git_text(worktree, "rev-parse", f"{base_sha}^{{tree}}"),
                None,
                None,
                None,
            )
        if schema_version not in {2, 3}:
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
        if schema_version == 3:
            required.add("untracked_paths")
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
        if recovery.get("schema_version") in {1, 2}:
            return ()
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
