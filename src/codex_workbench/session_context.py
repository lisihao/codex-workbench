from __future__ import annotations

from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any, BinaryIO

from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .model import canonical_hash
from .store import WorkbenchStore


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = 120) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {message[-2000:]}")
    return result.stdout.strip()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe context bundle path: {value!r}")
    return path


def _read_archive(source: BinaryIO) -> bytes:
    data = source.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("context bundle exceeds 256 MiB")
    if not data:
        raise ValueError("context bundle is empty")
    return data


def _extract_archive(data: bytes, destination: Path) -> dict[str, Any]:
    extracted = 0
    names: set[str] = set()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as error:
        raise ValueError(f"invalid context bundle: {error}") from error
    with archive:
        for member in archive:
            relative = _safe_relative(member.name)
            if member.name in names:
                raise ValueError(f"duplicate context bundle member: {member.name}")
            names.add(member.name)
            if member.isdir():
                (destination / Path(*relative.parts)).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"unsupported context bundle member type: {member.name}")
            if member.size > MAX_MEMBER_BYTES:
                raise ValueError(f"context bundle member exceeds 64 MiB: {member.name}")
            extracted += member.size
            if extracted > MAX_EXTRACTED_BYTES:
                raise ValueError("expanded context bundle exceeds 512 MiB")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read context bundle member: {member.name}")
            target = destination / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            target.chmod(0o600)
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("context bundle requires a manifest.json smaller than 1 MiB")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid context bundle manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("context bundle manifest must be an object")
    return manifest


def _validated_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported context bundle schema_version")
    thread_id = manifest.get("source_thread_id")
    repository = manifest.get("repository")
    if not isinstance(thread_id, str) or _IDENTIFIER.fullmatch(thread_id) is None:
        raise ValueError("source_thread_id is invalid")
    if not isinstance(repository, dict):
        raise ValueError("repository metadata is required")
    name = repository.get("name")
    base_sha = repository.get("head")
    if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
        raise ValueError("repository name is invalid")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise ValueError("repository head must be a full commit SHA")
    scopes = manifest.get("suggested_scopes")
    if not isinstance(scopes, list) or not scopes or not all(
        isinstance(item, str) and item.strip() for item in scopes
    ):
        raise ValueError("suggested_scopes must be a non-empty string array")
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise ValueError("files must be an array")
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("file metadata must be an object")
        _safe_relative(str(entry.get("archive_path", "")))
        logical_path = str(entry.get("logical_path", ""))
        if entry.get("kind") == "repository":
            _safe_relative(logical_path)
    return manifest


def _resolve_repository(manifest: dict[str, Any]) -> tuple[Path, str]:
    repository = manifest["repository"]
    root = Path(
        os.environ.get("CODEX_WORKBENCH_PROJECTS_ROOT", "~/Projects")
    ).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / repository["name"]
    origin = repository.get("origin")
    if not target.exists():
        if not isinstance(origin, str) or not origin.strip():
            raise ValueError(
                f"Mac mini repository {target} is missing and the bundle has no origin"
            )
        _run(["git", "clone", "--no-checkout", "--", origin, str(target)], timeout=600)
    resolved = Path(
        _run(["git", "-C", str(target), "rev-parse", "--show-toplevel"])
    ).resolve()
    if resolved != target.resolve():
        raise ValueError("resolved repository is outside the configured project root")
    base_sha = repository["head"].lower()
    probe = subprocess.run(
        ["git", "-C", str(target), "cat-file", "-e", f"{base_sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        _run(["git", "-C", str(target), "fetch", "--prune", "origin"], timeout=600)
        _run(["git", "-C", str(target), "cat-file", "-e", f"{base_sha}^{{commit}}"])
    return target.resolve(), base_sha


def _materialize_context(
    config: WorkbenchConfig,
    bundle_root: Path,
    manifest: dict[str, Any],
    archive_digest: str,
) -> tuple[Path, str, str]:
    repository, base_sha = _resolve_repository(manifest)
    thread_id = manifest["source_thread_id"]
    worktree = config.state_root / "session-contexts" / thread_id / archive_digest[:16]
    completion_marker = worktree / ".workbench-context" / "manifest.json"
    if worktree.exists() and not completion_marker.is_file():
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            check=False,
        )
        if worktree.exists():
            shutil.rmtree(worktree)
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _run(
            ["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree), base_sha],
            timeout=300,
        )
        patch_path = bundle_root / "git.patch"
        if patch_path.is_file() and patch_path.stat().st_size:
            _run(["git", "apply", "--binary", "--index", str(patch_path)], cwd=worktree)
        external_index = 0
        for entry in manifest.get("files", []):
            archive_path = bundle_root / Path(*_safe_relative(entry["archive_path"]).parts)
            if not archive_path.is_file():
                raise ValueError(f"declared context file is missing: {entry['archive_path']}")
            if entry.get("kind") == "repository":
                relative = _safe_relative(entry["logical_path"])
                if relative.parts[0] == ".git":
                    raise ValueError("context files cannot write .git")
                target = worktree / Path(*relative.parts)
            else:
                external_index += 1
                basename = Path(str(entry.get("logical_path") or "attachment")).name
                target = worktree / ".workbench-context" / "files" / f"{external_index:03d}-{basename}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive_path, target)
        metadata_root = worktree / ".workbench-context"
        metadata_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle_root / "manifest.json", metadata_root / "manifest.json")
        transcript = bundle_root / "transcript.jsonl"
        if transcript.is_file():
            shutil.copyfile(transcript, metadata_root / "transcript.jsonl")
        _run(["git", "add", "-A"], cwd=worktree)
        dirty = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=worktree, check=False
        ).returncode
        if dirty:
            _run(
                [
                    "git",
                    "-c",
                    "user.name=Codex Workbench",
                    "-c",
                    "user.email=workbench@localhost",
                    "commit",
                    "-m",
                    f"workbench context import {thread_id}",
                ],
                cwd=worktree,
            )
    imported_sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree)
    transcript_path = worktree / ".workbench-context" / "transcript.jsonl"
    excerpt = transcript_path.read_text(errors="replace")[-20000:] if transcript_path.is_file() else ""
    return worktree.resolve(), imported_sha, excerpt


def import_session_context(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    source: BinaryIO,
    *,
    command_id: str,
) -> dict[str, Any]:
    data = _read_archive(source)
    archive_digest = sha256(data).hexdigest()
    artifacts = ArtifactStore(config.state_root / "artifacts")
    archive_ref = artifacts.put_bytes(data, "tar.gz")
    with tempfile.TemporaryDirectory(prefix="codex-workbench-context-") as directory:
        bundle_root = Path(directory)
        manifest = _validated_manifest(_extract_archive(data, bundle_root))
        worktree, imported_sha, excerpt = _materialize_context(
            config, bundle_root, manifest, archive_digest
        )
    context_ref = archive_ref
    request_hash = canonical_hash(
        {"archive_ref": archive_ref, "source_thread_id": manifest["source_thread_id"]}
    )
    receipt = store.record_session_context(
        command_id=command_id,
        request_hash=request_hash,
        source_thread_id=manifest["source_thread_id"],
        context_ref=context_ref,
        archive_ref=archive_ref,
        manifest=manifest,
        repository=str(worktree),
        base_sha=imported_sha,
        allowed_scopes=tuple(manifest["suggested_scopes"]),
        context_excerpt=excerpt,
    )
    return {
        "ok": True,
        "state": "active",
        **{key: value for key, value in receipt.items() if key != "context_excerpt"},
    }
