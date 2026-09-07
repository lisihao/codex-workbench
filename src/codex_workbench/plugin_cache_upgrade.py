"""Recoverable, offline Codex plugin cache upgrades.

The native installer remains the only authority for plugin registration. This
module preserves versioned cache trees around that call and never runs Hooks or
writes Codex trust state.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from typing import Any, Callable, Iterator
from uuid import uuid4


PLUGIN_CACHE_UPGRADE_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_UPGRADE_ID = re.compile(r"^plugin-upgrade-[0-9a-f]{32}$")
_STAGING_NAME = re.compile(r"^\.pending-[0-9a-f]{32}$")
_PLUGIN_ROOT_REFERENCE = re.compile(
    r"\$(?:\{PLUGIN_ROOT\}|PLUGIN_ROOT)/([A-Za-z0-9._+@/-]+)"
)


class PluginCacheUpgradeError(RuntimeError):
    """Raised when a Codex plugin cache upgrade cannot remain recoverable."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _safe_name(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not _SAFE_NAME.fullmatch(value)
    ):
        raise PluginCacheUpgradeError(
            f"{label} must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def _safe_version(value: Any, label: str = "plugin version") -> str:
    if not isinstance(value, str) or not _SAFE_VERSION.fullmatch(value):
        raise PluginCacheUpgradeError(f"{label} is not a safe path component")
    return value


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _assert_no_symlink_ancestors(path: Path, label: str) -> None:
    current = _absolute(path)
    while True:
        if current.is_symlink():
            raise PluginCacheUpgradeError(f"{label} has a symlink ancestor: {current}")
        if current.parent == current:
            return
        current = current.parent


def _plugin_tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise PluginCacheUpgradeError(f"plugin cache version is not a directory: {root}")
    entries: list[dict[str, Any]] = [
        {
            "kind": "directory",
            "path": ".",
            "mode": root.stat().st_mode & 0o777,
        }
    ]
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise PluginCacheUpgradeError(
                f"plugin cache contains a symlink: {relative.as_posix()}"
            )
        if path.is_dir():
            entries.append(
                {
                    "kind": "directory",
                    "path": relative.as_posix(),
                    "mode": path.stat().st_mode & 0o777,
                }
            )
            continue
        if not path.is_file():
            raise PluginCacheUpgradeError(
                f"plugin cache contains an unsupported entry: {relative.as_posix()}"
            )
        payload = path.read_bytes()
        entries.append(
            {
                "kind": "file",
                "path": relative.as_posix(),
                "mode": path.stat().st_mode & 0o777,
                "size": len(payload),
                "sha256": sha256(payload).hexdigest(),
            }
        )
    encoded = json.dumps(
        entries,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _read_json(path: Path, label: str) -> Any:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PluginCacheUpgradeError(f"{label} is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                return json.load(stream)
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginCacheUpgradeError(f"{label} is unreadable or invalid: {error}") from error


def _relative_plugin_resource(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PluginCacheUpgradeError(f"{label} must be a relative path")
    normalized = value[2:] if value.startswith("./") else value
    relative = PurePosixPath(normalized)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PluginCacheUpgradeError(f"{label} must be a safe relative path")
    return Path(*relative.parts)


def _validate_declared_resources(root: Path, manifest: dict[str, Any]) -> None:
    hooks = manifest.get("hooks")
    if hooks is not None:
        hooks_path = root / _relative_plugin_resource(hooks, "plugin hooks path")
        _assert_no_symlink_ancestors(hooks_path, "plugin hooks path")
        hooks_payload = _read_json(hooks_path, "plugin hooks manifest")
        serialized = json.dumps(hooks_payload, ensure_ascii=False, sort_keys=True)
        for match in _PLUGIN_ROOT_REFERENCE.finditer(serialized):
            referenced = root / _relative_plugin_resource(
                match.group(1),
                "Hook PLUGIN_ROOT reference",
            )
            _assert_no_symlink_ancestors(referenced, "Hook PLUGIN_ROOT reference")
            if not referenced.is_file():
                raise PluginCacheUpgradeError(
                    f"Hook PLUGIN_ROOT reference is missing: {referenced}"
                )
    skills = manifest.get("skills")
    if skills is not None:
        skills_path = root / _relative_plugin_resource(skills, "plugin skills path")
        _assert_no_symlink_ancestors(skills_path, "plugin skills path")
        if not skills_path.is_dir():
            raise PluginCacheUpgradeError(
                f"plugin skills directory is missing: {skills_path}"
            )
        skill_files = list(skills_path.rglob("SKILL.md"))
        if not skill_files:
            raise PluginCacheUpgradeError(
                f"plugin skills directory has no SKILL.md: {skills_path}"
            )
        for skill_file in skill_files:
            _assert_no_symlink_ancestors(skill_file, "plugin skill")
            if not skill_file.is_file():
                raise PluginCacheUpgradeError(f"plugin skill is invalid: {skill_file}")


def _sync_path(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root: Path) -> None:
    paths = sorted(
        root.rglob("*"),
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for path in paths:
        if path.is_symlink():
            raise PluginCacheUpgradeError(
                f"cannot sync a plugin cache tree containing a symlink: {path}"
            )
        _sync_path(path)
    _sync_path(root)


def _sync_directory_lineage(path: Path, stop: Path) -> None:
    current = path
    while True:
        _sync_path(current)
        if current == stop:
            return
        if current.parent == current or stop not in current.parents:
            raise PluginCacheUpgradeError(
                f"cannot sync directory outside expected root {stop}: {path}"
            )
        current = current.parent


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(descriptor)
        os.replace(temporary, path)
        _sync_path(path.parent)
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    finally:
        os.close(descriptor)


def _copy_plugin_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


@contextmanager
def _exclusive_upgrade_lock(lock_path: Path) -> Iterator[None]:
    _assert_no_symlink_ancestors(lock_path, "plugin upgrade lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise PluginCacheUpgradeError(
            f"cannot open plugin upgrade lock {lock_path}: {error}"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PluginCacheUpgradeError(
                f"plugin upgrade lock is not a regular file: {lock_path}"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise PluginCacheUpgradeError(
                    f"another plugin cache upgrade is active: {lock_path}"
                ) from error
            raise
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _run_json(
    command: tuple[str, ...],
    *,
    codex_home: Path,
    runner: Runner,
) -> Any:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
    except OSError as error:
        raise PluginCacheUpgradeError(
            f"cannot execute {' '.join(command[:3])}: {error}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise PluginCacheUpgradeError(
            f"command failed ({' '.join(command[:3])}): {detail}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PluginCacheUpgradeError(
            f"command returned invalid JSON ({' '.join(command[:3])})"
        ) from error


def _installed_item(
    payload: Any,
    *,
    marketplace: str,
    plugin: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), list):
        raise PluginCacheUpgradeError("Codex plugin list has an invalid JSON envelope")
    plugin_id = f"{plugin}@{marketplace}"
    matches = [
        item
        for item in payload["installed"]
        if isinstance(item, dict) and item.get("pluginId") == plugin_id
    ]
    if len(matches) != 1:
        raise PluginCacheUpgradeError(
            f"expected exactly one installed plugin {plugin_id}, found {len(matches)}"
        )
    item = matches[0]
    version = item.get("version")
    source = item.get("source")
    marketplace_source = item.get("marketplaceSource")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(source, dict)
        or source.get("source") != "local"
        or not isinstance(source.get("path"), str)
        or not source["path"]
        or not isinstance(marketplace_source, dict)
        or marketplace_source.get("sourceType") not in {"local", "git"}
    ):
        raise PluginCacheUpgradeError(
            f"installed plugin {plugin_id} lacks a supported version or source"
        )
    return item


def _source_version(item: dict[str, Any], plugin: str) -> str:
    source = _source_path(item)
    manifest = _read_json(
        source / ".codex-plugin" / "plugin.json",
        "plugin source manifest",
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != plugin
        or not isinstance(manifest.get("version"), str)
        or not manifest["version"]
    ):
        raise PluginCacheUpgradeError(
            "plugin source manifest does not match the requested plugin"
        )
    _validate_declared_resources(source, manifest)
    return _safe_version(manifest["version"], "plugin source version")


def _source_path(item: dict[str, Any]) -> Path:
    source = Path(item["source"]["path"]).expanduser()
    if not source.is_absolute():
        raise PluginCacheUpgradeError("plugin source path must be absolute")
    return source


def _cache_versions(cache_root: Path, plugin: str) -> list[dict[str, str]]:
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise PluginCacheUpgradeError(
            f"installed plugin cache is missing: {cache_root}"
        )
    versions: list[dict[str, str]] = []
    for candidate in sorted(cache_root.iterdir(), key=lambda path: path.name):
        version = _safe_version(candidate.name, "cached plugin version")
        if candidate.is_symlink() or not candidate.is_dir():
            raise PluginCacheUpgradeError(
                f"plugin cache has an unsupported version entry: {candidate}"
            )
        manifest = _read_json(
            candidate / ".codex-plugin" / "plugin.json",
            "cached plugin manifest",
        )
        if (
            not isinstance(manifest, dict)
            or manifest.get("name") != plugin
            or manifest.get("version") != version
        ):
            raise PluginCacheUpgradeError(
                f"cached plugin identity does not match its version directory: {candidate}"
            )
        _validate_declared_resources(candidate, manifest)
        versions.append(
            {
                "version": version,
                "digest": _plugin_tree_digest(candidate),
            }
        )
    if not versions:
        raise PluginCacheUpgradeError(f"plugin cache has no version directories: {cache_root}")
    return versions


def _paths(
    codex_home: Path,
    *,
    marketplace: str,
    plugin: str,
) -> tuple[Path, Path, Path, Path]:
    cache_root = codex_home / "plugins" / "cache" / marketplace / plugin
    retention_root = codex_home / "plugin-cache-retention" / marketplace / plugin
    pending = retention_root / "pending"
    receipts = retention_root / "receipts"
    for path, label in (
        (codex_home, "Codex home"),
        (cache_root, "plugin cache"),
        (retention_root, "plugin retention root"),
        (pending, "pending plugin retention"),
        (receipts, "plugin retention receipts"),
        (retention_root / "retained", "retained plugin caches"),
    ):
        _assert_no_symlink_ancestors(path, label)
    return cache_root, retention_root, pending, receipts


def _lock_path(codex_home: Path, *, marketplace: str, plugin: str) -> Path:
    lock_path = (
        codex_home
        / "plugin-cache-retention"
        / marketplace
        / f".{plugin}.upgrade.lock"
    )
    _assert_no_symlink_ancestors(lock_path, "plugin upgrade lock")
    return lock_path


def _reject_prepublish_staging(retention_root: Path) -> None:
    if not retention_root.exists():
        return
    if retention_root.is_symlink() or not retention_root.is_dir():
        raise PluginCacheUpgradeError(
            f"plugin retention root is unsafe: {retention_root}"
        )
    residues: list[Path] = []
    for candidate in retention_root.iterdir():
        if not candidate.name.startswith(".pending-"):
            continue
        if (
            not _STAGING_NAME.fullmatch(candidate.name)
            or candidate.is_symlink()
            or not candidate.is_dir()
        ):
            raise PluginCacheUpgradeError(
                f"unsafe pre-publish plugin staging residue: {candidate}"
            )
        residues.append(candidate)
    if residues:
        rendered = ", ".join(str(path) for path in sorted(residues))
        raise PluginCacheUpgradeError(
            "pre-publish plugin staging residue found; native installation was not "
            f"started, so the installed cache remains authoritative: {rendered}. "
            "Inspect or quarantine this residue before retrying"
        )


def inspect_upgrade(
    *,
    codex_home: str | Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    marketplace = _safe_name(marketplace, "marketplace")
    plugin = _safe_name(plugin, "plugin")
    home = _absolute(codex_home)
    if not home.is_dir():
        raise PluginCacheUpgradeError(f"Codex home is not a directory: {home}")
    cache_root, retention_root, pending, _ = _paths(
        home,
        marketplace=marketplace,
        plugin=plugin,
    )
    payload = _run_json(
        (codex_binary, "plugin", "list", "--json"),
        codex_home=home,
        runner=runner,
    )
    item = _installed_item(payload, marketplace=marketplace, plugin=plugin)
    versions = _cache_versions(cache_root, plugin)
    installed_version = _safe_version(item["version"], "installed plugin version")
    if installed_version not in {entry["version"] for entry in versions}:
        raise PluginCacheUpgradeError(
            f"installed version {installed_version} is missing from cache; "
            "restore that exact cache before upgrading"
        )
    source_path = _source_path(item)
    return {
        "schema_version": PLUGIN_CACHE_UPGRADE_SCHEMA_VERSION,
        "plugin_id": f"{plugin}@{marketplace}",
        "marketplace": marketplace,
        "plugin": plugin,
        "codex_home": str(home),
        "cache_root": str(cache_root),
        "retention_root": str(retention_root),
        "pending_recovery": pending.is_dir(),
        "installed_version": installed_version,
        "source_version": _source_version(item, plugin),
        "source_path": str(source_path),
        "source_digest": _plugin_tree_digest(source_path),
        "marketplace_source_type": item["marketplaceSource"]["sourceType"],
        "cached_versions": versions,
        "mutates_trust": False,
        "runs_hook": False,
    }


def _snapshot_cache(plan: dict[str, Any]) -> Path:
    cache_root = _absolute(plan["cache_root"])
    retention_root = _absolute(plan["retention_root"])
    pending = retention_root / "pending"
    if pending.exists() or pending.is_symlink():
        raise PluginCacheUpgradeError(
            f"an interrupted plugin cache upgrade must be recovered first: {pending}"
        )
    retention_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    retention_root.chmod(0o700)
    _sync_directory_lineage(retention_root, _absolute(plan["codex_home"]))
    staging = retention_root / f".pending-{uuid4().hex}"
    versions_root = staging / "versions"
    versions_root.mkdir(parents=True, mode=0o700)
    try:
        for entry in plan["cached_versions"]:
            version = _safe_version(entry["version"], "cached plugin version")
            source = cache_root / version
            if _plugin_tree_digest(source) != entry["digest"]:
                raise PluginCacheUpgradeError(
                    f"plugin cache changed before snapshot: {version}"
                )
            _copy_plugin_tree(source, versions_root / version)
            if _plugin_tree_digest(versions_root / version) != entry["digest"]:
                raise PluginCacheUpgradeError(
                    f"plugin cache snapshot differs from source: {version}"
                )
        receipt = {
            "schema_version": PLUGIN_CACHE_UPGRADE_SCHEMA_VERSION,
            "upgrade_id": f"plugin-upgrade-{uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "plugin_id": plan["plugin_id"],
            "marketplace": plan["marketplace"],
            "plugin": plan["plugin"],
            "codex_home": plan["codex_home"],
            "cache_root": plan["cache_root"],
            "installed_version": plan["installed_version"],
            "source_version": plan["source_version"],
            "source_path": plan["source_path"],
            "source_digest": plan["source_digest"],
            "target_version": (
                plan["source_version"]
                if plan["marketplace_source_type"] == "local"
                else None
            ),
            "target_source_path": (
                plan["source_path"]
                if plan["marketplace_source_type"] == "local"
                else None
            ),
            "target_source_digest": (
                plan["source_digest"]
                if plan["marketplace_source_type"] == "local"
                else None
            ),
            "marketplace_source_type": plan["marketplace_source_type"],
            "cached_versions": plan["cached_versions"],
        }
        _write_json(staging / "receipt.json", receipt)
        _sync_tree(staging)
        staging.replace(pending)
        _sync_path(retention_root)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return pending


def _pending_receipt(
    pending: Path,
    *,
    codex_home: Path,
    marketplace: str,
    plugin: str,
) -> dict[str, Any]:
    cache_root, retention_root, expected_pending, _ = _paths(
        codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    receipt_path = pending / "receipt.json"
    _assert_no_symlink_ancestors(receipt_path, "pending plugin cache receipt")
    receipt = _read_json(receipt_path, "pending plugin cache receipt")
    required = {
        "schema_version",
        "upgrade_id",
        "created_at",
        "plugin_id",
        "marketplace",
        "plugin",
        "codex_home",
        "cache_root",
        "installed_version",
        "source_version",
        "source_path",
        "source_digest",
        "target_version",
        "target_source_path",
        "target_source_digest",
        "marketplace_source_type",
        "cached_versions",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required
        or receipt.get("schema_version") != PLUGIN_CACHE_UPGRADE_SCHEMA_VERSION
        or receipt.get("plugin_id") != f"{plugin}@{marketplace}"
        or receipt.get("marketplace") != marketplace
        or receipt.get("plugin") != plugin
        or receipt.get("codex_home") != str(codex_home)
        or receipt.get("cache_root") != str(cache_root)
        or pending != expected_pending
        or pending.parent != retention_root
        or not isinstance(receipt.get("cached_versions"), list)
        or not isinstance(receipt.get("upgrade_id"), str)
        or not _SAFE_UPGRADE_ID.fullmatch(receipt["upgrade_id"])
        or receipt.get("marketplace_source_type") not in {"local", "git"}
    ):
        raise PluginCacheUpgradeError("pending plugin cache receipt has an invalid scope")
    _safe_version(receipt.get("installed_version"), "pending installed version")
    _safe_version(receipt.get("source_version"), "pending source version")
    if not Path(str(receipt.get("source_path", ""))).is_absolute():
        raise PluginCacheUpgradeError("pending source path must be absolute")
    if (
        not isinstance(receipt.get("source_digest"), str)
        or not _SAFE_DIGEST.fullmatch(receipt["source_digest"])
    ):
        raise PluginCacheUpgradeError("pending source digest is invalid")
    if receipt.get("target_version") is not None:
        _safe_version(receipt["target_version"], "pending target version")
    if (
        receipt.get("target_source_path") is not None
        and not Path(str(receipt["target_source_path"])).is_absolute()
    ):
        raise PluginCacheUpgradeError("pending target source path must be absolute")
    target_digest = receipt.get("target_source_digest")
    if target_digest is not None and (
        not isinstance(target_digest, str) or not _SAFE_DIGEST.fullmatch(target_digest)
    ):
        raise PluginCacheUpgradeError("pending target source digest is invalid")
    target_fields = (
        receipt.get("target_version"),
        receipt.get("target_source_path"),
        target_digest,
    )
    if any(value is None for value in target_fields) and not all(
        value is None for value in target_fields
    ):
        raise PluginCacheUpgradeError(
            "pending target version, source path, and digest must be recorded together"
        )
    return receipt


def _validated_pending_snapshot(
    pending: Path,
    *,
    codex_home: Path,
    marketplace: str,
    plugin: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    receipt = _pending_receipt(
        pending,
        codex_home=codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    validated: list[dict[str, str]] = []
    expected_versions: set[str] = set()
    for raw_entry in receipt["cached_versions"]:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"version", "digest"}:
            raise PluginCacheUpgradeError("pending cached-version receipt is invalid")
        version = _safe_version(
            raw_entry.get("version"),
            "pending cached plugin version",
        )
        digest = raw_entry.get("digest")
        if (
            not isinstance(digest, str)
            or not _SAFE_DIGEST.fullmatch(digest)
            or version in expected_versions
        ):
            raise PluginCacheUpgradeError("pending cached-version receipt is invalid")
        expected_versions.add(version)
        validated.append({"version": version, "digest": digest})
    if not validated:
        raise PluginCacheUpgradeError("pending cached-version receipt is empty")
    versions_root = pending / "versions"
    if versions_root.is_symlink() or not versions_root.is_dir():
        raise PluginCacheUpgradeError("pending plugin cache versions directory is invalid")
    candidates = list(versions_root.iterdir())
    observed_versions = {candidate.name for candidate in candidates}
    if (
        len(candidates) != len(observed_versions)
        or observed_versions != expected_versions
    ):
        raise PluginCacheUpgradeError(
            "pending plugin cache snapshot contains unrecorded version entries"
        )
    for entry in validated:
        source = versions_root / entry["version"]
        if _plugin_tree_digest(source) != entry["digest"]:
            raise PluginCacheUpgradeError(
                f"pending plugin cache snapshot is corrupt: {entry['version']}"
            )
    restore_root = pending / "restore-staging"
    _assert_no_symlink_ancestors(
        restore_root,
        "plugin cache restore staging",
    )
    if restore_root.exists() and not restore_root.is_dir():
        raise PluginCacheUpgradeError(
            f"plugin cache restore staging is not a directory: {restore_root}"
        )
    return receipt, validated


def _set_pending_target(
    pending: Path,
    receipt: dict[str, Any],
    target_version: str,
    target_source_path: Path,
) -> dict[str, Any]:
    if not target_source_path.is_absolute():
        raise PluginCacheUpgradeError("plugin target source path must be absolute")
    target_source_digest = _plugin_tree_digest(target_source_path)
    updated = {
        **receipt,
        "target_version": _safe_version(target_version, "plugin target version"),
        "target_source_path": str(target_source_path),
        "target_source_digest": target_source_digest,
    }
    _write_json(pending / "receipt.json", updated)
    return updated


def restore_pending_cache(
    *,
    codex_home: str | Path,
    marketplace: str,
    plugin: str,
) -> dict[str, Any]:
    marketplace = _safe_name(marketplace, "marketplace")
    plugin = _safe_name(plugin, "plugin")
    home = _absolute(codex_home)
    cache_root, _, pending, _ = _paths(
        home,
        marketplace=marketplace,
        plugin=plugin,
    )
    if not pending.is_dir() or pending.is_symlink():
        raise PluginCacheUpgradeError("no pending plugin cache upgrade is available")
    receipt, retained_versions = _validated_pending_snapshot(
        pending,
        codex_home=home,
        marketplace=marketplace,
        plugin=plugin,
    )
    if cache_root.exists() and (cache_root.is_symlink() or not cache_root.is_dir()):
        raise PluginCacheUpgradeError(f"plugin cache path is unsafe: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_directory_lineage(cache_root, home)
    for entry in retained_versions:
        version = entry["version"]
        source = pending / "versions" / version
        destination = cache_root / version
        if destination.exists() or destination.is_symlink():
            if _plugin_tree_digest(destination) != entry["digest"]:
                raise PluginCacheUpgradeError(
                    f"existing cache conflicts with retained version {version}"
                )
            continue
        restore_root = pending / "restore-staging"
        _assert_no_symlink_ancestors(
            restore_root,
            "plugin cache restore staging",
        )
        staging = restore_root / version
        if staging.is_symlink():
            raise PluginCacheUpgradeError(
                f"plugin cache restore staging is unsafe: {staging}"
            )
        if staging.exists():
            if not staging.is_dir():
                raise PluginCacheUpgradeError(
                    f"plugin cache restore staging is invalid: {staging}"
                )
            shutil.rmtree(staging)
        restore_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _copy_plugin_tree(source, staging)
        if _plugin_tree_digest(staging) != entry["digest"]:
            raise PluginCacheUpgradeError(
                f"restored plugin cache staging failed verification: {version}"
            )
        _sync_tree(staging)
        if pending.stat().st_dev != cache_root.parent.stat().st_dev:
            raise PluginCacheUpgradeError(
                "plugin cache retention must be on the same filesystem as the cache"
            )
        try:
            staging.replace(destination)
        except OSError as error:
            if destination.is_dir() and not destination.is_symlink():
                if _plugin_tree_digest(destination) == entry["digest"]:
                    shutil.rmtree(staging)
                    continue
            raise PluginCacheUpgradeError(
                f"cannot atomically restore plugin cache version {version}: {error}"
            ) from error
        _sync_path(cache_root)
        if _plugin_tree_digest(destination) != entry["digest"]:
            raise PluginCacheUpgradeError(
                f"restored plugin cache failed verification: {version}"
            )
    return receipt


def _finish_pending(
    pending: Path,
    receipts: Path,
    receipt: dict[str, Any],
    *,
    status: str,
    installed_version: str | None,
    error: str | None = None,
) -> Path:
    _assert_no_symlink_ancestors(receipts, "plugin retention receipts")
    retained_root = pending.parent / "retained"
    _assert_no_symlink_ancestors(retained_root, "retained plugin caches")
    retained_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    retained_path = retained_root / receipt["upgrade_id"]
    if retained_path.exists() or retained_path.is_symlink():
        raise PluginCacheUpgradeError(
            f"retained plugin cache transaction already exists: {retained_path}"
        )
    completed = {
        **receipt,
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "final_installed_version": installed_version,
        "error": error,
        "retained_cache": str(retained_path),
    }
    destination = receipts / f"{receipt['upgrade_id']}.json"
    _write_json(destination, completed)
    _sync_path(retained_root)
    pending.replace(retained_path)
    _sync_path(retained_root)
    _sync_path(pending.parent)
    return destination


def _plugin_list_item(
    *,
    codex_home: Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    runner: Runner,
) -> dict[str, Any]:
    payload = _run_json(
        (codex_binary, "plugin", "list", "--json"),
        codex_home=codex_home,
        runner=runner,
    )
    return _installed_item(payload, marketplace=marketplace, plugin=plugin)


def _attest_installed_cache(
    *,
    codex_home: Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    runner: Runner,
    expected_version: str | None = None,
    expected_source_version: str | None = None,
    expected_source_path: Path | None = None,
    expected_source_digest: str | None = None,
    expected_marketplace_source_type: str | None = None,
) -> dict[str, Any]:
    item = _plugin_list_item(
        codex_home=codex_home,
        codex_binary=codex_binary,
        marketplace=marketplace,
        plugin=plugin,
        runner=runner,
    )
    installed_version = _safe_version(item.get("version"), "installed plugin version")
    source_version = _source_version(item, plugin)
    source_path = _source_path(item)
    source_digest = _plugin_tree_digest(source_path)
    if expected_version is not None and installed_version != expected_version:
        raise PluginCacheUpgradeError(
            f"post-install plugin list reports {installed_version}, expected {expected_version}"
        )
    if expected_source_path is not None and source_path != expected_source_path:
        raise PluginCacheUpgradeError(
            f"post-install plugin source path reports {source_path}, "
            f"expected {expected_source_path}"
        )
    if expected_source_digest is not None and source_digest != expected_source_digest:
        raise PluginCacheUpgradeError(
            f"post-install plugin source digest reports {source_digest}, "
            f"expected {expected_source_digest}"
        )
    source_type = item["marketplaceSource"]["sourceType"]
    if (
        expected_marketplace_source_type is not None
        and source_type != expected_marketplace_source_type
    ):
        raise PluginCacheUpgradeError(
            f"post-install marketplace source type reports {source_type}, "
            f"expected {expected_marketplace_source_type}"
        )
    if (
        expected_source_version is not None
        and source_version != expected_source_version
    ):
        raise PluginCacheUpgradeError(
            f"post-install plugin source reports {source_version}, "
            f"expected {expected_source_version}"
        )
    cache_root, _, _, _ = _paths(
        codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    versions = _cache_versions(cache_root, plugin)
    matches = [entry for entry in versions if entry["version"] == installed_version]
    if len(matches) != 1:
        raise PluginCacheUpgradeError(
            f"configured plugin version {installed_version} is missing from cache"
        )
    if (
        expected_source_digest is not None
        and matches[0]["digest"] != expected_source_digest
    ):
        raise PluginCacheUpgradeError(
            "installed plugin cache is not an exact copy of the recorded source tree"
        )
    return {
        "installed_version": installed_version,
        "source_version": source_version,
        "source_path": str(source_path),
        "source_digest": source_digest,
        "installed_digest": matches[0]["digest"],
        "marketplace_source_type": source_type,
    }


def _native_add(
    *,
    codex_home: Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    expected_version: str,
    runner: Runner,
) -> Path:
    cache_root, _, _, _ = _paths(
        codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    install = _run_json(
        (
            codex_binary,
            "plugin",
            "add",
            f"{plugin}@{marketplace}",
            "--json",
        ),
        codex_home=codex_home,
        runner=runner,
    )
    if (
        not isinstance(install, dict)
        or install.get("pluginId") != f"{plugin}@{marketplace}"
        or install.get("version") != expected_version
        or not isinstance(install.get("installedPath"), str)
    ):
        raise PluginCacheUpgradeError(
            "Codex plugin add did not attest the requested new version"
        )
    installed_path = Path(install["installedPath"]).expanduser()
    if not installed_path.is_absolute():
        raise PluginCacheUpgradeError("Codex plugin add returned a relative cache path")
    expected_path = cache_root / expected_version
    if (
        installed_path != expected_path
        or installed_path.is_symlink()
        or not installed_path.is_dir()
    ):
        raise PluginCacheUpgradeError(
            "Codex plugin add returned an unexpected cache path"
        )
    return installed_path


def _recover_interrupted_upgrade(
    *,
    codex_home: Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    runner: Runner,
    pending: Path,
    receipts: Path,
) -> Path:
    receipt = restore_pending_cache(
        codex_home=codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    try:
        state = _attest_installed_cache(
            codex_home=codex_home,
            codex_binary=codex_binary,
            marketplace=marketplace,
            plugin=plugin,
            runner=runner,
        )
    except PluginCacheUpgradeError as initial_error:
        target_version = receipt.get("target_version")
        if not isinstance(target_version, str):
            raise PluginCacheUpgradeError(
                f"interrupted upgrade restored retained caches, but the configured "
                f"plugin is inconsistent and no exact target is recorded: {initial_error}; "
                f"pending recovery remains at {pending}"
            ) from initial_error
        try:
            _native_add(
                codex_home=codex_home,
                codex_binary=codex_binary,
                marketplace=marketplace,
                plugin=plugin,
                expected_version=target_version,
                runner=runner,
            )
            restore_pending_cache(
                codex_home=codex_home,
                marketplace=marketplace,
                plugin=plugin,
            )
            state = _attest_installed_cache(
                codex_home=codex_home,
                codex_binary=codex_binary,
                marketplace=marketplace,
                plugin=plugin,
                runner=runner,
                expected_version=target_version,
                expected_source_version=target_version,
                expected_source_path=Path(receipt["target_source_path"]),
                expected_source_digest=receipt["target_source_digest"],
                expected_marketplace_source_type=receipt[
                    "marketplace_source_type"
                ],
            )
        except PluginCacheUpgradeError as repair_error:
            try:
                restore_pending_cache(
                    codex_home=codex_home,
                    marketplace=marketplace,
                    plugin=plugin,
                )
            except PluginCacheUpgradeError as restore_error:
                raise PluginCacheUpgradeError(
                    f"interrupted upgrade repair failed: {repair_error}; retained "
                    f"cache restoration also failed: {restore_error}; pending "
                    f"recovery remains at {pending}"
                ) from repair_error
            raise PluginCacheUpgradeError(
                f"interrupted upgrade recovery could not restore a consistent "
                f"configured plugin: {repair_error}; pending recovery remains at {pending}"
            ) from repair_error
    return _finish_pending(
        pending,
        receipts,
        receipt,
        status="interrupted_upgrade_recovered",
        installed_version=state["installed_version"],
    )


def _apply_upgrade_locked(
    *,
    codex_home: Path,
    codex_binary: str,
    marketplace: str,
    plugin: str,
    recover_only: bool,
    runner: Runner,
) -> dict[str, Any]:
    cache_root, retention_root, pending, receipts = _paths(
        codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    recovered_receipt: str | None = None
    if pending.is_dir() and not pending.is_symlink():
        recovered_receipt = str(
            _recover_interrupted_upgrade(
                codex_home=codex_home,
                codex_binary=codex_binary,
                marketplace=marketplace,
                plugin=plugin,
                runner=runner,
                pending=pending,
                receipts=receipts,
            )
        )
    elif pending.exists() or pending.is_symlink():
        raise PluginCacheUpgradeError(
            f"pending plugin cache path is unsafe: {pending}"
        )
    _reject_prepublish_staging(retention_root)
    if recover_only:
        return {
            "ok": True,
            "mode": "recover-only",
            "status": "recovered" if recovered_receipt else "no-pending-upgrade",
            "recovered_receipt": recovered_receipt,
            "runs_hook": False,
            "mutates_trust": False,
        }

    plan = inspect_upgrade(
        codex_home=codex_home,
        codex_binary=codex_binary,
        marketplace=marketplace,
        plugin=plugin,
        runner=runner,
    )
    if (
        plan["marketplace_source_type"] == "local"
        and plan["source_version"] == plan["installed_version"]
    ):
        return {
            "ok": True,
            "mode": "apply",
            "status": "already_current",
            "plugin_id": plan["plugin_id"],
            "previous_version": plan["installed_version"],
            "installed_version": plan["installed_version"],
            "retained_versions": [
                entry["version"] for entry in plan["cached_versions"]
            ],
            "receipt": None,
            "recovered_receipt": recovered_receipt,
            "runs_hook": False,
            "mutates_trust": False,
            "prunes_old_cache": False,
        }
    pending = _snapshot_cache(plan)
    receipt = _pending_receipt(
        pending,
        codex_home=codex_home,
        marketplace=marketplace,
        plugin=plugin,
    )
    installed_version: str | None = None
    try:
        if plan["marketplace_source_type"] == "git":
            _run_json(
                (
                    codex_binary,
                    "plugin",
                    "marketplace",
                    "upgrade",
                    marketplace,
                    "--json",
                ),
                codex_home=codex_home,
                runner=runner,
            )
        item = _plugin_list_item(
            codex_home=codex_home,
            codex_binary=codex_binary,
            marketplace=marketplace,
            plugin=plugin,
            runner=runner,
        )
        desired_version = _source_version(item, plugin)
        desired_source_path = _source_path(item)
        receipt = _set_pending_target(
            pending,
            receipt,
            desired_version,
            desired_source_path,
        )
        if desired_version == plan["installed_version"]:
            installed_version = desired_version
            status = "already_current"
        else:
            _native_add(
                codex_home=codex_home,
                codex_binary=codex_binary,
                marketplace=marketplace,
                plugin=plugin,
                expected_version=desired_version,
                runner=runner,
            )
            installed_version = desired_version
            status = "upgraded"
        restore_pending_cache(
            codex_home=codex_home,
            marketplace=marketplace,
            plugin=plugin,
        )
        state = _attest_installed_cache(
            codex_home=codex_home,
            codex_binary=codex_binary,
            marketplace=marketplace,
            plugin=plugin,
            runner=runner,
            expected_version=installed_version,
            expected_source_version=desired_version,
            expected_source_path=desired_source_path,
            expected_source_digest=receipt["target_source_digest"],
            expected_marketplace_source_type=plan["marketplace_source_type"],
        )
        installed_version = state["installed_version"]
        completed_receipt = _finish_pending(
            pending,
            receipts,
            receipt,
            status=status,
            installed_version=installed_version,
        )
    except BaseException as error:
        try:
            restored = restore_pending_cache(
                codex_home=codex_home,
                marketplace=marketplace,
                plugin=plugin,
            )
            consistent = _attest_installed_cache(
                codex_home=codex_home,
                codex_binary=codex_binary,
                marketplace=marketplace,
                plugin=plugin,
                runner=runner,
            )
            _finish_pending(
                pending,
                receipts,
                restored,
                status="upgrade_failed_cache_restored",
                installed_version=consistent["installed_version"],
                error=f"{type(error).__name__}: {error}",
            )
        except BaseException as restore_error:
            raise PluginCacheUpgradeError(
                f"plugin upgrade failed: {error}; cache restoration also failed: "
                f"{restore_error}; pending receipt retained at {pending}"
            ) from error
        if isinstance(error, KeyboardInterrupt):
            raise
        raise PluginCacheUpgradeError(str(error)) from error
    return {
        "ok": True,
        "mode": "apply",
        "status": status,
        "plugin_id": plan["plugin_id"],
        "previous_version": plan["installed_version"],
        "installed_version": installed_version,
        "retained_versions": [
            entry["version"] for entry in plan["cached_versions"]
        ],
        "receipt": str(completed_receipt),
        "recovered_receipt": recovered_receipt,
        "runs_hook": False,
        "mutates_trust": False,
        "prunes_old_cache": False,
    }


def safe_upgrade(
    *,
    codex_home: str | Path,
    codex_binary: str,
    marketplace: str = "codex-workbench",
    plugin: str = "codex-workbench",
    apply: bool = False,
    confirm_active_hook_cache_retention: bool = False,
    confirm_host_stopped: bool = False,
    recover_only: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    marketplace = _safe_name(marketplace, "marketplace")
    plugin = _safe_name(plugin, "plugin")
    home = _absolute(codex_home)
    if not home.is_dir():
        raise PluginCacheUpgradeError(f"Codex home is not a directory: {home}")
    if recover_only and not apply:
        raise PluginCacheUpgradeError("--recover-only requires --apply")
    if not apply:
        _, retention_root, pending, _ = _paths(
            home,
            marketplace=marketplace,
            plugin=plugin,
        )
        if pending.is_dir() and not pending.is_symlink():
            receipt, retained = _validated_pending_snapshot(
                pending,
                codex_home=home,
                marketplace=marketplace,
                plugin=plugin,
            )
            return {
                "schema_version": PLUGIN_CACHE_UPGRADE_SCHEMA_VERSION,
                "plugin_id": receipt["plugin_id"],
                "mode": "dry-run",
                "pending_recovery": True,
                "upgrade_required": False,
                "would_retain_active_cache": True,
                "would_run": ["restore pending transaction before any upgrade"],
                "pending": str(pending),
                "retained_versions": [entry["version"] for entry in retained],
                "online_zero_downtime": False,
                "mutates_trust": False,
                "runs_hook": False,
            }
        if pending.exists() or pending.is_symlink():
            raise PluginCacheUpgradeError(
                f"pending plugin cache path is unsafe: {pending}"
            )
        _reject_prepublish_staging(retention_root)
        plan = inspect_upgrade(
            codex_home=home,
            codex_binary=codex_binary,
            marketplace=marketplace,
            plugin=plugin,
            runner=runner,
        )
        upgrade_required = (
            plan["marketplace_source_type"] == "git"
            or plan["source_version"] != plan["installed_version"]
        )
        return {
            **plan,
            "mode": "dry-run",
            "upgrade_required": upgrade_required,
            "would_retain_active_cache": upgrade_required,
            "would_run": (
                ["codex plugin marketplace upgrade", "codex plugin add"]
                if plan["marketplace_source_type"] == "git"
                else (["codex plugin add"] if upgrade_required else [])
            ),
            "online_zero_downtime": False,
        }
    if not confirm_active_hook_cache_retention:
        raise PluginCacheUpgradeError(
            "--apply requires --confirm-active-hook-cache-retention because restored "
            "Hook code may continue the already-configured context data flow"
        )
    if not confirm_host_stopped:
        raise PluginCacheUpgradeError(
            "--apply requires --confirm-host-stopped; native Codex installation may "
            "briefly remove the old Hook path before restoration"
        )
    lock_path = _lock_path(home, marketplace=marketplace, plugin=plugin)
    with _exclusive_upgrade_lock(lock_path):
        return _apply_upgrade_locked(
            codex_home=home,
            codex_binary=codex_binary,
            marketplace=marketplace,
            plugin=plugin,
            recover_only=recover_only,
            runner=runner,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Offline-upgrade one Codex plugin while retaining exact old version "
            "caches that older hosts may still reference after restart."
        )
    )
    parser.add_argument("--codex-home", default="~/.codex")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--marketplace", default="codex-workbench")
    parser.add_argument("--plugin", default="codex-workbench")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument(
        "--confirm-host-stopped",
        action="store_true",
        help="confirm that Codex App and interactive hosts using this plugin are stopped",
    )
    parser.add_argument(
        "--confirm-active-hook-cache-retention",
        action="store_true",
        help=(
            "confirm that retaining the old Hook cache may continue its already "
            "configured context data flow until the host reloads"
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = safe_upgrade(
            codex_home=args.codex_home,
            codex_binary=args.codex_binary,
            marketplace=args.marketplace,
            plugin=args.plugin,
            apply=bool(args.apply),
            confirm_active_hook_cache_retention=bool(
                args.confirm_active_hook_cache_retention
            ),
            confirm_host_stopped=bool(args.confirm_host_stopped),
            recover_only=bool(args.recover_only),
        )
    except PluginCacheUpgradeError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
