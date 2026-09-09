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
)
from .executors import codex_subscription_environment
from .worktrees import WorktreeError, WorktreeManager


class DirtyWorktreeRecoveryError(WorktreeError):
    """A blocked dirty worktree cannot be resumed without losing provenance."""


_RECOVERY_ACCEPTANCE_ENVIRONMENT = frozenset(
    {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
    }
)


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
            if self._has_template_marker(
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
    ) -> dict[str, object]:
        """Capture only the blocked worker's own patch.

        A dependent worker starts from a materialized ancestor tree rather
        than the contract commit. Its recovery receipt pins that exact input
        artifact and calculates the worker delta from that tree, so accepted
        ancestor patches never become part of the worker's patch.
        """

        path = self._validate_worktree(repository, base_sha, worktree, branch,
                                       checkpoint_sha=expected_checkpoint_sha)
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
        changed_paths = tuple(sorted(changed_paths_since_input_tree(path, comparison_tree)))
        if changed_paths != reported_recoverable:
            raise DirtyWorktreeRecoveryError(
                "worktree changed paths do not match the blocked worker receipt"
            )
        generated_residue_ref = self.discard_generated_residue(
            path,
            expected_generated,
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
        if generated_residue_ref is not None:
            recovery_context = {
                **recovery_context,
                "schema_version": {1: 4, 2: 5, 3: 6}[int(recovery_context["schema_version"])],
                "generated_residue_paths": list(expected_generated),
                "generated_residue_ref": generated_residue_ref,
            }
        check = self._git_text(path, "diff", "--check", comparison_tree)
        if check:
            raise DirtyWorktreeRecoveryError(f"dirty worktree fails git diff --check: {check}")
        patch = self.captured_patch(path, comparison_tree, untracked_paths)
        if not patch:
            raise DirtyWorktreeRecoveryError("dirty worktree has no patch to preserve")
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
                declared_command, command, environment_overrides = self._parse_command(
                    command_source
                )
                outcome = self._run_command(
                    declared_command,
                    target,
                    timeout_seconds,
                    executable=command,
                    environment_overrides=environment_overrides,
                )
                outcomes.append(outcome)
                checks.append(
                    ("PASS" if outcome.exit_code == 0 else "FAIL")
                    + f": {' '.join(declared_command)} (exit {outcome.exit_code})"
                )
                if outcome.exit_code != 0:
                    log_ref = self._store_logs(materialization, outcomes)
                    self._validate_snapshot(repository, str(source), recovery)
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

    def prepare_for_retry(
        self,
        *,
        repository: str,
        source_worktree: str,
        target_worktree: str,
        target_branch: str,
        target_attempt: int,
        recovery: Mapping[str, object],
    ) -> RecoveryOutcome:
        """Restore a sealed failed attempt before its normal executor runs.

        Unlike ``prepare``, this deliberately does not execute acceptance
        commands or materialize dependencies: the retry's original executor
        still owns those actions.  It only proves that the old worker patch
        and recorded ancestor input can be reproduced on a fresh target
        without changing the source worktree.
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
            self.worktrees.apply_patch(target, self._patch_path(recovery))
            self.mark_untracked_intent_to_add(
                target,
                self._recovery_untracked_paths(recovery),
            )
            if self._git_bytes(target, "diff", "--binary", comparison_tree) != patch:
                raise DirtyWorktreeRecoveryError(
                    "retry target patch does not exactly match the captured failed attempt"
                )
            self._validate_snapshot(repository, str(source), recovery)
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
        ignored_paths = self.ignored_paths(path)
        if ignored_paths:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree acquired ignored paths after recovery was scheduled: "
                + ", ".join(ignored_paths)
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
        if schema_version in {1, 4}:
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
            if schema_version == 4:
                legacy |= {"generated_residue_paths", "generated_residue_ref"}
            if "source_checkpoint_sha" in recovery:
                legacy.add("source_checkpoint_sha")
            if set(recovery) != legacy:
                raise DirtyWorktreeRecoveryError("blocked legacy recovery receipt has an invalid shape")
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
        if schema_version not in {4, 5, 6}:
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
                + ", ".join(unexpected)
            )
        observed = tuple(path for path in ignored if is_python_bytecode_residue_path(path))
        undeclared = tuple(sorted(set(observed) - set(expected)))
        if undeclared:
            raise DirtyWorktreeRecoveryError(
                "dirty worktree contains unreported generated residue: "
                + ", ".join(undeclared)
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
                + ", ".join(remaining)
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
        if recovery.get("schema_version") in {1, 2, 4, 5}:
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


    def _validate_worktree(self, repository: str, base_sha: str, worktree: str, branch: str,
                           *, checkpoint_sha: object = None) -> Path:
        repo = Path(repository).expanduser().resolve(strict=True)
        path = Path(worktree).expanduser().resolve(strict=True)
        root = self.worktrees.root.expanduser().resolve(strict=False)
        if not path.is_relative_to(root):
            raise DirtyWorktreeRecoveryError("recovery worktree is outside the Workbench worktree root")
        if self._git_text(path, "rev-parse", "--show-toplevel") != str(path):
            raise DirtyWorktreeRecoveryError("recovery path is not a standalone Git worktree")
        base = self._git_text(repo, "rev-parse", f"{base_sha}^{{commit}}")
        expected_head = base
        if checkpoint_sha is not None:
            if not isinstance(checkpoint_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", checkpoint_sha):
                raise DirtyWorktreeRecoveryError("checkpoint requires an exact full commit SHA")
            common = self._git_text(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if common != self._git_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"):
                raise DirtyWorktreeRecoveryError("checkpoint source belongs to another repository")
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

    def _run_command(
        self,
        declared_command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        *,
        executable: tuple[str, ...] | None = None,
        environment_overrides: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:
        command = executable or declared_command
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
