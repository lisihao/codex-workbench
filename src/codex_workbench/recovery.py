from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
from typing import BinaryIO, Callable, Iterator
import uuid

from .model import canonical_json
from .store import StateConflictError, WorkbenchStore
from .sync import RepositorySynchronizer
from .worktrees import WorktreeError, WorktreeManager


ARCHIVE_SCHEMA_VERSION = 1


class WorktreeRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryPolicy:
    state_root: Path
    enabled: bool
    recycle_root: Path
    archive_root: Path | None
    restore_root: Path
    outgoing_root: Path
    sweep_interval_seconds: int
    home_presence_ttl_seconds: int
    retry_backoff_seconds: int
    compression: str
    zstd_binary: str | None
    require_smb: bool
    remote_archive_host: str | None
    remote_state_root: str

    @classmethod
    def load(cls, state_root: Path) -> "RecoveryPolicy":
        root = state_root.expanduser().absolute()
        config_file = root / "config.json"
        raw: dict[str, object] = {}
        if config_file.is_file():
            value = json.loads(config_file.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise WorktreeRecoveryError("Workbench config must be a JSON object")
            configured = value.get("worktree_recovery", {})
            if not isinstance(configured, dict):
                raise WorktreeRecoveryError("worktree_recovery config must be an object")
            raw = configured

        def configured_path(name: str, default: Path | None) -> Path | None:
            value = raw.get(name)
            if value is None:
                return default
            if not isinstance(value, str) or not value.strip():
                raise WorktreeRecoveryError(f"worktree_recovery.{name} must be a path")
            return Path(value).expanduser().absolute()

        archive_root = configured_path("nas_archive_root", None)
        compression = str(raw.get("compression", "zstd"))
        if compression not in {"zstd", "gzip"}:
            raise WorktreeRecoveryError("worktree_recovery.compression must be zstd or gzip")
        zstd_value = raw.get("zstd_binary")
        zstd_binary = (
            str(Path(zstd_value).expanduser().absolute())
            if isinstance(zstd_value, str) and zstd_value
            else shutil.which("zstd")
        )
        interval = int(raw.get("sweep_interval_seconds", 60))
        ttl = int(raw.get("home_presence_ttl_seconds", 600))
        retry_backoff = int(raw.get("retry_backoff_seconds", 900))
        if interval < 10 or interval > 3600:
            raise WorktreeRecoveryError("worktree recovery sweep interval must be between 10 and 3600 seconds")
        if ttl < 60 or ttl > 3600:
            raise WorktreeRecoveryError("home presence TTL must be between 60 and 3600 seconds")
        if retry_backoff < 60 or retry_backoff > 86400:
            raise WorktreeRecoveryError("worktree recovery retry backoff must be between 60 and 86400 seconds")
        remote_host = raw.get("remote_archive_host")
        if remote_host is not None and (
            not isinstance(remote_host, str)
            or not remote_host
            or remote_host.startswith("-")
            or any(character.isspace() for character in remote_host)
        ):
            raise WorktreeRecoveryError("remote_archive_host must be one SSH destination")
        remote_state_root = str(
            raw.get("remote_state_root", "~/Library/Application Support/Codex Workbench")
        )
        return cls(
            state_root=root,
            enabled=bool(raw.get("enabled", True)),
            recycle_root=configured_path("recycle_root", root / "recycle" / "worktrees") or root,
            archive_root=archive_root,
            restore_root=configured_path("restore_root", root / "restored-worktrees") or root,
            outgoing_root=configured_path("outgoing_root", root / "recycle" / "outgoing") or root,
            sweep_interval_seconds=interval,
            home_presence_ttl_seconds=ttl,
            retry_backoff_seconds=retry_backoff,
            compression=compression,
            zstd_binary=zstd_binary,
            require_smb=bool(raw.get("require_smb", True)),
            remote_archive_host=remote_host,
            remote_state_root=remote_state_root,
        )

    @property
    def suffix(self) -> str:
        return "tar.zst" if self.compression == "zstd" else "tar.gz"


def _safe_segment(value: str) -> str:
    rendered = "".join(character if character.isalnum() or character in "._-" else "-" for character in value)
    rendered = rendered.strip(".-")
    if not rendered:
        raise WorktreeRecoveryError("identifier cannot form a recovery path")
    return rendered[:96]


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _write_synced_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _artifact_refs(value: object) -> set[str]:
    if isinstance(value, str):
        if re.fullmatch(r"sha256:[0-9a-f]{64}:[A-Za-z0-9][A-Za-z0-9._-]{0,31}", value):
            return {value}
        return set()
    if isinstance(value, dict):
        result: set[str] = set()
        for item in value.values():
            result.update(_artifact_refs(item))
        return result
    if isinstance(value, (list, tuple)):
        result = set()
        for item in value:
            result.update(_artifact_refs(item))
        return result
    return set()


class WorktreeRecoveryManager:
    def __init__(
        self,
        store: WorkbenchStore,
        policy: RecoveryPolicy,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.store = store
        self.policy = policy
        self.runner = runner
        self.worktrees = WorktreeManager(policy.state_root / "worktrees")

    @staticmethod
    def _git(repository: Path, *args: str, timeout: int = 120) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise WorktreeRecoveryError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def quarantine(self, allocation_id: str) -> dict[str, object]:
        allocation = self.store.get_worktree_allocation(allocation_id)
        if allocation["state"] not in {"active", "quarantine_pending"}:
            return allocation
        source = Path(str(allocation["current_path"])).expanduser().absolute()
        destination = (
            self.policy.recycle_root
            / _safe_segment(str(allocation["task_id"]))
            / f"{_safe_segment(str(allocation['node_id']))}-a{int(allocation['attempt'])}"
        ).absolute()
        self.store.begin_worktree_quarantine(allocation_id, str(destination))
        if destination.exists() and not source.exists():
            return self.store.finish_worktree_quarantine(allocation_id, str(destination))
        if not source.exists():
            raise WorktreeRecoveryError(f"allocated worktree no longer exists: {source}")
        try:
            self.worktrees.move(str(allocation["repository"]), source, destination)
        except WorktreeError as error:
            raise WorktreeRecoveryError(str(error)) from error
        return self.store.finish_worktree_quarantine(allocation_id, str(destination))

    @staticmethod
    def _tree_manifest(root: Path) -> tuple[list[dict[str, object]], list[str]]:
        entries: list[dict[str, object]] = []
        omitted: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if path.is_symlink():
                entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path), "mode": mode})
            elif path.is_dir():
                entries.append({"path": relative, "kind": "directory", "mode": mode})
            elif path.is_file():
                entries.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": mode,
                        "size": metadata.st_size,
                        "sha256": _hash_file(path),
                    }
                )
            else:
                omitted.append(relative)
        return entries, omitted

    @contextmanager
    def _tar_writer(self, output: Path) -> Iterator[tarfile.TarFile]:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.policy.compression == "gzip":
            with gzip.open(output, "wb", compresslevel=6) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    yield archive
            return
        if not self.policy.zstd_binary:
            raise WorktreeRecoveryError("zstd compression is configured but the zstd binary is unavailable")
        process = subprocess.Popen(
            [self.policy.zstd_binary, "-q", "-T0", "-f", "-o", str(output)],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None and process.stderr is not None
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                yield archive
        finally:
            process.stdin.close()
            return_code = process.wait(timeout=1800)
            error = process.stderr.read().decode(errors="replace").strip()
            if return_code:
                raise WorktreeRecoveryError(error or f"zstd exited {return_code}")

    @contextmanager
    def _tar_reader(self, source: Path) -> Iterator[tarfile.TarFile]:
        if source.name.endswith(".gz") or self.policy.compression == "gzip":
            with gzip.open(source, "rb") as compressed:
                with tarfile.open(fileobj=compressed, mode="r|") as archive:
                    yield archive
            return
        if not self.policy.zstd_binary:
            raise WorktreeRecoveryError("zstd is required to read this archive")
        process = subprocess.Popen(
            [self.policy.zstd_binary, "-q", "-d", "-c", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                yield archive
        finally:
            process.stdout.close()
            return_code = process.wait(timeout=1800)
            error = process.stderr.read().decode(errors="replace").strip()
            if return_code:
                raise WorktreeRecoveryError(error or f"zstd decode exited {return_code}")

    def create_capsule(self, allocation: dict[str, object], archive_id: str, output: Path) -> dict[str, object]:
        worktree = Path(str(allocation["current_path"])).expanduser().resolve(strict=True)
        if not _within(worktree, self.policy.recycle_root):
            raise WorktreeRecoveryError("only a quarantined Workbench path may be archived")
        repository = Path(str(allocation["repository"])).expanduser().resolve(strict=True)
        head_sha = self._git(worktree, "rev-parse", "HEAD")
        branch = str(allocation["branch"])
        if self._git(worktree, "branch", "--show-current") != branch:
            raise WorktreeRecoveryError("quarantined worktree branch does not match its allocation")
        entries, omitted = self._tree_manifest(worktree)
        task_contract = allocation.get("contract")
        node_spec = allocation.get("spec")
        node_result = allocation.get("node_result")
        supporting_refs = sorted(_artifact_refs((task_contract, node_spec, node_result)))
        manifest: dict[str, object] = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "allocation_id": allocation["allocation_id"],
            "task_id": allocation["task_id"],
            "node_id": allocation["node_id"],
            "attempt": allocation["attempt"],
            "repository": str(repository),
            "base_sha": allocation["base_sha"],
            "branch": branch,
            "head_sha": head_sha,
            "source_host": socket.gethostname(),
            "source_path": str(worktree),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "compression": self.policy.compression,
            "entries": entries,
            "omitted_special_files": omitted,
            "git_status": self._git(worktree, "status", "--porcelain=v1", "--untracked-files=all"),
            "task_contract": task_contract,
            "node_spec": node_spec,
            "node_result": node_result,
            "supporting_artifacts": {},
        }
        with tempfile.TemporaryDirectory(prefix="worktree-capsule-") as directory:
            metadata = Path(directory) / "metadata"
            metadata.mkdir()
            artifact_directory = metadata / "artifacts"
            artifact_files: dict[str, str] = {}
            for ref in supporting_refs:
                source = self.store.artifacts.verify(ref)
                _, digest, suffix = ref.split(":", 2)
                relative = f"artifacts/{digest}.{suffix}"
                artifact_directory.mkdir(exist_ok=True)
                shutil.copy2(source, metadata / relative)
                artifact_files[ref] = relative
            manifest["supporting_artifacts"] = artifact_files
            manifest_path = metadata / "manifest.json"
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            bundle = metadata / "repository.bundle"
            self._git(repository, "bundle", "create", str(bundle), branch, timeout=600)
            with self._tar_writer(output) as archive:
                archive.add(worktree, arcname="tree", recursive=False)
                for entry in entries:
                    relative = str(entry["path"])
                    archive.add(worktree / relative, arcname=f"tree/{relative}", recursive=False)
                archive.add(metadata, arcname="metadata", recursive=True)
        return manifest

    @staticmethod
    def _safe_member_path(destination: Path, name: str) -> Path:
        pure = PurePosixPath(name)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise WorktreeRecoveryError(f"archive member escapes recovery root: {name!r}")
        if pure.parts[0] not in {"tree", "metadata"}:
            raise WorktreeRecoveryError(f"unsupported archive member root: {name!r}")
        return destination.joinpath(*pure.parts)

    def _extract(self, archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        seen: set[str] = set()
        symlinks: list[tuple[Path, str, Path]] = []
        directory_modes: list[tuple[Path, int]] = []
        with self._tar_reader(archive_path) as archive:
            for member in archive:
                normalized = PurePosixPath(member.name).as_posix().rstrip("/")
                if normalized in seen:
                    raise WorktreeRecoveryError(f"duplicate archive member: {member.name!r}")
                seen.add(normalized)
                target = self._safe_member_path(destination, normalized)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directory_modes.append((target, member.mode & 0o777))
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise WorktreeRecoveryError(f"cannot read archive member: {member.name!r}")
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    os.chmod(target, member.mode & 0o777)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    member_root = destination / PurePosixPath(normalized).parts[0]
                    symlinks.append((target, member.linkname, member_root))
                else:
                    raise WorktreeRecoveryError(f"unsupported archive member type: {member.name!r}")
        for target, link_name, member_root in symlinks:
            if Path(link_name).is_absolute():
                raise WorktreeRecoveryError(f"absolute symlink is not recoverable: {target}")
            resolved = (target.parent / link_name).resolve(strict=False)
            if not _within(resolved, member_root):
                raise WorktreeRecoveryError(f"symlink escapes recovery root: {target}")
            target.symlink_to(link_name)
        for target, mode in reversed(directory_modes):
            os.chmod(target, mode)

    def _codec_test(self, archive_path: Path) -> None:
        if archive_path.name.endswith(".gz") or self.policy.compression == "gzip":
            with gzip.open(archive_path, "rb") as stream:
                for _ in iter(lambda: stream.read(1024 * 1024), b""):
                    pass
            return
        if not self.policy.zstd_binary:
            raise WorktreeRecoveryError("zstd is required to verify this archive")
        result = subprocess.run(
            [self.policy.zstd_binary, "-q", "-t", str(archive_path)],
            capture_output=True,
            timeout=1800,
            check=False,
        )
        if result.returncode:
            raise WorktreeRecoveryError(result.stderr.decode(errors="replace").strip() or "zstd integrity test failed")

    def verify_capsule(self, archive_path: Path, archive_id: str) -> dict[str, object]:
        self._codec_test(archive_path)
        self.policy.restore_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=f"verify-{_safe_segment(archive_id)}-", dir=self.policy.restore_root) as directory:
            extracted = Path(directory) / "capsule"
            self._extract(archive_path, extracted)
            manifest_path = extracted / "metadata" / "manifest.json"
            bundle = extracted / "metadata" / "repository.bundle"
            if not manifest_path.is_file() or not bundle.is_file():
                raise WorktreeRecoveryError("archive is missing recovery metadata")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
                raise WorktreeRecoveryError("unsupported worktree archive manifest")
            if manifest.get("archive_id") != archive_id:
                raise WorktreeRecoveryError("archive ID does not match its manifest")
            entries, omitted = self._tree_manifest(extracted / "tree")
            if entries != manifest.get("entries") or omitted:
                raise WorktreeRecoveryError("restored workspace does not match the archive manifest")
            if not isinstance(manifest.get("omitted_special_files"), list):
                raise WorktreeRecoveryError("archive special-file omission index is invalid")
            supporting = manifest.get("supporting_artifacts")
            if not isinstance(supporting, dict):
                raise WorktreeRecoveryError("archive supporting artifact index is invalid")
            for ref, relative in supporting.items():
                if not isinstance(ref, str) or not isinstance(relative, str):
                    raise WorktreeRecoveryError("archive supporting artifact index is invalid")
                artifact_path = extracted / "metadata" / relative
                if not _within(artifact_path, extracted / "metadata") or not artifact_path.is_file():
                    raise WorktreeRecoveryError(f"archive supporting artifact is missing: {ref}")
                expected = ref.split(":", 2)[1]
                if _hash_file(artifact_path) != expected:
                    raise WorktreeRecoveryError(f"archive supporting artifact hash mismatch: {ref}")
            clone = Path(directory) / "git-restore"
            result = subprocess.run(
                ["git", "clone", "--no-local", "--branch", str(manifest["branch"]), str(bundle), str(clone)],
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
            if result.returncode:
                raise WorktreeRecoveryError(result.stderr.strip() or result.stdout.strip())
            restored_head = self._git(clone, "rev-parse", "HEAD")
            if restored_head != manifest.get("head_sha"):
                raise WorktreeRecoveryError("Git bundle HEAD does not match the archived worktree")
            return manifest

    def _assert_archive_root(self) -> Path:
        if self.policy.archive_root is None:
            raise WorktreeRecoveryError("NAS archive root is not configured")
        configured = self.policy.archive_root.expanduser().absolute()
        ancestor = configured
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
            raise WorktreeRecoveryError(f"NAS archive parent is not writable: {ancestor}")
        if self.policy.require_smb:
            mount_point = ancestor.resolve(strict=True)
            while mount_point.parent != mount_point and not os.path.ismount(mount_point):
                mount_point = mount_point.parent
            smbutil = shutil.which("smbutil") or "/usr/bin/smbutil"
            result = subprocess.run(
                [smbutil, "statshares", "-m", str(mount_point)],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise WorktreeRecoveryError("configured archive root is not a verified SMB share")
        configured.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = configured.resolve(strict=True)
        return root

    def _archive_paths(self, archive_id: str) -> tuple[Path, Path, Path, Path]:
        root = self._assert_archive_root()
        now = datetime.now(UTC)
        directory = root / f"{now.year:04d}" / f"{now.month:02d}"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = directory / f"{archive_id}.{self.policy.suffix}"
        partial = directory / f"{archive_id}.{self.policy.suffix}.partial"
        sidecar = Path(str(final) + ".sha256")
        sidecar_partial = Path(str(sidecar) + ".partial")
        return partial, final, sidecar_partial, sidecar

    @staticmethod
    def _assert_archive_targets_absent(*paths: Path) -> None:
        occupied = next((path for path in paths if path.exists()), None)
        if occupied is not None:
            raise WorktreeRecoveryError(f"archive destination already exists: {occupied}")

    def _purge(self, allocation: dict[str, object], archive_id: str) -> dict[str, object]:
        if not allocation.get("purge_allowed"):
            raise StateConflictError("this worktree is still required by the delivery contract")
        try:
            self.worktrees.remove(
                str(allocation["repository"]),
                Path(str(allocation["current_path"])),
                str(allocation["branch"]),
            )
        except WorktreeError as error:
            raise WorktreeRecoveryError(str(error)) from error
        return self.store.mark_worktree_purged(str(allocation["allocation_id"]), archive_id)

    def archive_allocation(self, allocation_id: str, *, purge: bool = True) -> dict[str, object]:
        candidates = {
            str(candidate["allocation_id"]): candidate
            for candidate in self.store.reclaimable_worktree_allocations()
        }
        if allocation_id not in candidates:
            raise StateConflictError("worktree is not reclaimable")
        allocation = candidates[allocation_id]
        if allocation["state"] in {"active", "quarantine_pending"}:
            allocation = self.quarantine(allocation_id)
            allocation["purge_allowed"] = candidates[allocation_id]["purge_allowed"]
        if allocation["state"] not in {"quarantined", "archive_failed"}:
            raise StateConflictError(f"worktree allocation is {allocation['state']}, expected quarantined")
        archive_id = "wta-" + uuid.uuid4().hex
        self.store.begin_worktree_archive(
            archive_id,
            allocation_id,
            source_host=socket.gethostname(),
            source_path=str(allocation["current_path"]),
            transport="local-nas",
        )
        partial: Path | None = None
        sidecar_partial: Path | None = None
        try:
            partial, final, sidecar_partial, sidecar = self._archive_paths(archive_id)
            self._assert_archive_targets_absent(partial, final, sidecar_partial, sidecar)
            manifest = self.create_capsule(allocation, archive_id, partial)
            verified = self.verify_capsule(partial, archive_id)
            if verified != manifest:
                raise WorktreeRecoveryError("verified manifest changed during archive creation")
            digest = _hash_file(partial)
            _sync_file(partial)
            _write_synced_text(sidecar_partial, f"{digest}  {final.name}\n")
            os.replace(partial, final)
            os.replace(sidecar_partial, sidecar)
            receipt = self.store.finish_worktree_archive(
                archive_id,
                archive_path=str(final),
                archive_sha256=digest,
                size_bytes=final.stat().st_size,
                manifest=manifest,
            )
        except Exception as error:
            self.store.fail_worktree_archive(archive_id, f"{type(error).__name__}: {error}")
            raise
        finally:
            if partial is not None and partial.exists():
                partial.unlink()
            if sidecar_partial is not None and sidecar_partial.exists():
                sidecar_partial.unlink()
        if purge and allocation["purge_allowed"]:
            try:
                self._purge(allocation, archive_id)
            except Exception as error:
                self.store.mark_worktree_purge_failed(allocation_id, f"{type(error).__name__}: {error}")
                raise
            receipt = self.store.get_worktree_archive(archive_id)
        return receipt

    def ingest_remote(
        self,
        source: BinaryIO,
        *,
        archive_id: str,
        expected_sha256: str,
        transport: str,
        compression: str,
    ) -> dict[str, object]:
        if transport != "tailscale":
            raise ValueError("remote worktree archives must use the tailscale transport label")
        if _safe_segment(archive_id) != archive_id or not archive_id.startswith("wta-"):
            raise ValueError("archive_id is not a valid Workbench archive identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if compression != self.policy.compression:
            raise ValueError(
                f"remote archive compression {compression!r} does not match authority policy {self.policy.compression!r}"
            )
        try:
            existing = self.store.get_worktree_archive(archive_id)
        except KeyError:
            existing = None
        if existing is not None:
            if existing["state"] != "verified" or existing.get("archive_sha256") != expected_sha256:
                raise StateConflictError("remote archive ID already has a different or incomplete receipt")
            digest = sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise WorktreeRecoveryError("remote archive checksum does not match the existing receipt")
            return existing
        self.store.begin_worktree_archive(
            archive_id,
            None,
            source_host="remote-macbook",
            source_path="remote-quarantine",
            transport=transport,
        )
        partial: Path | None = None
        sidecar_partial: Path | None = None
        try:
            partial, final, sidecar_partial, sidecar = self._archive_paths(archive_id)
            self._assert_archive_targets_absent(partial, final, sidecar_partial, sidecar)
            digest = sha256()
            with partial.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise WorktreeRecoveryError("remote archive checksum does not match the sender receipt")
            manifest = self.verify_capsule(partial, archive_id)
            _sync_file(partial)
            _write_synced_text(sidecar_partial, f"{actual}  {final.name}\n")
            os.replace(partial, final)
            os.replace(sidecar_partial, sidecar)
            return self.store.finish_worktree_archive(
                archive_id,
                archive_path=str(final),
                archive_sha256=actual,
                size_bytes=final.stat().st_size,
                manifest=manifest,
            )
        except Exception as error:
            self.store.fail_worktree_archive(archive_id, f"{type(error).__name__}: {error}")
            raise
        finally:
            if partial is not None and partial.exists():
                partial.unlink()
            if sidecar_partial is not None and sidecar_partial.exists():
                sidecar_partial.unlink()

    @staticmethod
    def _remote_binary(state_root: str) -> str:
        value = state_root.rstrip("/")
        if value == "~":
            value = "$HOME"
        elif value.startswith("~/"):
            value = "$HOME/" + value[2:]
        if value == "$HOME" or value.startswith("$HOME/"):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}/app/bin/codex-workbench"'
        return shlex.quote(value + "/app/bin/codex-workbench")

    def send_allocation(self, allocation_id: str, host: str | None = None) -> dict[str, object]:
        destination_host = host or self.policy.remote_archive_host
        if not destination_host:
            raise WorktreeRecoveryError("remote archive host is not configured")
        allocation = self.store.get_worktree_allocation(allocation_id)
        candidates = {item["allocation_id"]: item for item in self.store.reclaimable_worktree_allocations()}
        if allocation_id not in candidates:
            raise StateConflictError("worktree is not reclaimable")
        allocation = candidates[allocation_id]
        if allocation["state"] in {"active", "quarantine_pending"}:
            purge_allowed = bool(allocation["purge_allowed"])
            allocation = self.quarantine(allocation_id)
            allocation["purge_allowed"] = purge_allowed
        archive_id = "wta-" + uuid.uuid4().hex
        self.policy.outgoing_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        outgoing = self.policy.outgoing_root / f"{archive_id}.{self.policy.suffix}"
        receipt_started = False
        archive_verified = False
        try:
            manifest = self.create_capsule(allocation, archive_id, outgoing)
            if self.verify_capsule(outgoing, archive_id) != manifest:
                raise WorktreeRecoveryError("local remote-transfer restore validation failed")
            digest = _hash_file(outgoing)
            self.store.begin_worktree_archive(
                archive_id,
                allocation_id,
                source_host=socket.gethostname(),
                source_path=str(allocation["current_path"]),
                transport="remote-tailscale-forced",
            )
            receipt_started = True
            remote_command = " ".join(
                (
                    "exec",
                    self._remote_binary(self.policy.remote_state_root),
                    "worktree",
                    "ingest",
                    "--archive-id",
                    archive_id,
                    "--sha256",
                    digest,
                    "--transport",
                    "tailscale",
                    "--compression",
                    self.policy.compression,
                )
            )
            ssh_arguments = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
            transport = RepositorySynchronizer._location_aware_transport()
            if transport is None:
                raise WorktreeRecoveryError(
                    "remote NAS transfer requires the installed location-aware Tailscale profile"
                )
            proxy_command, _ = transport
            transport_profile = "tailscale-forced"
            ssh_arguments.extend(
                (
                    "-o",
                    f"ProxyCommand={proxy_command} --force-tailscale",
                    "-o",
                    "HostKeyAlias=codex-workbench-authority",
                )
            )
            ssh_arguments.extend((destination_host, remote_command))
            with outgoing.open("rb") as stream:
                result = self.runner(
                    ssh_arguments,
                    stdin=stream,
                    capture_output=True,
                    timeout=1800,
                    check=False,
                )
            stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
            stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
            if result.returncode:
                raise WorktreeRecoveryError((stderr or stdout or "remote archive transfer failed").strip())
            remote_receipt = json.loads(stdout)
            if (
                remote_receipt.get("state") != "verified"
                or remote_receipt.get("archive_sha256") != digest
                or remote_receipt.get("archive_id") != archive_id
            ):
                raise WorktreeRecoveryError("Mac mini did not return a matching verified NAS receipt")
            receipt = self.store.finish_worktree_archive(
                archive_id,
                archive_path=str(remote_receipt["archive_path"]),
                archive_sha256=digest,
                size_bytes=int(remote_receipt["size_bytes"]),
                manifest=manifest,
            )
            archive_verified = True
            if allocation.get("purge_allowed"):
                try:
                    self._purge(allocation, archive_id)
                except Exception as error:
                    self.store.mark_worktree_purge_failed(
                        allocation_id,
                        f"{type(error).__name__}: {error}",
                    )
                    raise
                receipt = self.store.get_worktree_archive(archive_id)
            return {**receipt, "transport_profile": transport_profile, "remote_host": destination_host}
        except Exception as error:
            if receipt_started and not archive_verified:
                self.store.fail_worktree_archive(archive_id, f"{type(error).__name__}: {error}")
            raise
        finally:
            outgoing.unlink(missing_ok=True)

    def _retry_due(self, allocation: dict[str, object]) -> bool:
        if allocation["state"] != "archive_failed":
            return True
        latest = next(
            (
                archive
                for archive in self.store.list_worktree_archives()
                if archive.get("allocation_id") == allocation["allocation_id"]
            ),
            None,
        )
        if latest is None:
            return True
        updated = datetime.fromisoformat(str(latest["updated_at"]).replace("Z", "+00:00"))
        return datetime.now(UTC) >= updated.astimezone(UTC) + timedelta(
            seconds=self.policy.retry_backoff_seconds
        )

    def restore(self, archive_id: str, destination: Path | None = None) -> dict[str, object]:
        receipt = self.store.get_worktree_archive(archive_id)
        if receipt["state"] != "verified" or not receipt.get("archive_path"):
            raise StateConflictError("restore requires a verified archive receipt")
        archive_path = Path(str(receipt["archive_path"])).expanduser().resolve(strict=True)
        if _hash_file(archive_path) != receipt["archive_sha256"]:
            raise WorktreeRecoveryError("NAS archive checksum no longer matches its receipt")
        self.policy.restore_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        restore_path = (
            destination.expanduser().absolute()
            if destination is not None
            else self.policy.restore_root / _safe_segment(archive_id)
        )
        if restore_path.exists():
            raise WorktreeRecoveryError(f"restore destination already exists: {restore_path}")
        with tempfile.TemporaryDirectory(prefix=f"restore-{_safe_segment(archive_id)}-", dir=self.policy.restore_root) as directory:
            extracted = Path(directory) / "capsule"
            self._extract(archive_path, extracted)
            manifest = json.loads((extracted / "metadata" / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("archive_id") != archive_id:
                raise WorktreeRecoveryError("archive manifest does not match the requested receipt")
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--branch",
                    str(manifest["branch"]),
                    str(extracted / "metadata" / "repository.bundle"),
                    str(restore_path),
                ],
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
            if result.returncode:
                raise WorktreeRecoveryError(result.stderr.strip() or result.stdout.strip())
            for child in restore_path.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in (extracted / "tree").iterdir():
                target = restore_path / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target, symlinks=True)
                elif child.is_symlink():
                    target.symlink_to(os.readlink(child))
                else:
                    shutil.copy2(child, target)
            entries, omitted = self._tree_manifest(restore_path)
            if entries != manifest.get("entries") or omitted:
                raise WorktreeRecoveryError("restored worktree does not match the verified manifest")
        self.store.record_system_event(
            "worktree.restored",
            {"archive_id": archive_id, "destination": str(restore_path)},
        )
        return {"ok": True, "archive_id": archive_id, "destination": str(restore_path), "manifest": manifest}

    def sweep(self, *, max_items: int = 1) -> dict[str, object]:
        if not self.policy.enabled:
            return {"ok": True, "status": "disabled", "processed": []}
        processed: list[dict[str, object]] = []
        presence = self.store.active_home_presence()
        for allocation in self.store.reclaimable_worktree_allocations()[:max_items]:
            current = allocation
            if current["state"] in {"active", "quarantine_pending"}:
                current = self.quarantine(str(current["allocation_id"]))
                processed.append({"allocation_id": current["allocation_id"], "action": "quarantined"})
            if current["state"] in {"quarantined", "archive_failed"}:
                if not self._retry_due(current):
                    continue
                if presence is not None and self.policy.archive_root is not None:
                    receipt = self.archive_allocation(str(current["allocation_id"]))
                    processed.append({"allocation_id": current["allocation_id"], "action": "archived", "archive_id": receipt["archive_id"]})
                elif self.policy.remote_archive_host:
                    receipt = self.send_allocation(str(current["allocation_id"]))
                    processed.append({"allocation_id": current["allocation_id"], "action": "remote-archived", "archive_id": receipt["archive_id"]})
            elif current["state"] == "archived_verified" and current.get("purge_allowed"):
                receipt = next(
                    (item for item in self.store.list_worktree_archives() if item.get("allocation_id") == current["allocation_id"] and item["state"] == "verified"),
                    None,
                )
                if receipt is not None:
                    self._purge(current, str(receipt["archive_id"]))
                    processed.append({"allocation_id": current["allocation_id"], "action": "purged", "archive_id": receipt["archive_id"]})
        return {
            "ok": True,
            "status": "processed" if processed else "idle",
            "home_presence": presence,
            "processed": processed,
        }

    def run_forever(self, stop) -> None:
        while not stop.is_set():
            try:
                self.sweep()
            except Exception as error:
                self.store.record_system_event(
                    "worktree.recovery_failed",
                    {"error": f"{type(error).__name__}: {error}"},
                )
            stop.wait(self.policy.sweep_interval_seconds)
