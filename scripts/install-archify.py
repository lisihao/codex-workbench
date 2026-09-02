#!/usr/bin/env python3
"""Install the vendored Archify Skill for Codex and Claude Code.

The installer is deliberately two-phase: it validates the pinned vendor and
both agent targets before creating or replacing anything.  A same-name Skill
is replaced only when its directory contains this installer's ownership
marker; an unmanaged Skill is left untouched and causes the whole operation
to stop.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from codex_workbench.archify import (  # noqa: E402
    ARCHIFY_COMMIT,
    ARCHIFY_LICENSE,
    ARCHIFY_REPOSITORY,
    ARCHIFY_TAG,
    ARCHIFY_VERSION,
    SKILL_NAME,
    ArchifyContractError,
    verify_skill_projection,
    verify_vendor,
)


MANAGED_MARKER_FILENAME = ".codex-workbench-archify.json"
MANAGED_BY = "codex-workbench"
TRANSACTION_RECORD_FILENAME = ".archify.transaction.json"
TRANSACTION_SCHEMA_VERSION = 1
TARGETS = {
    "codex": Path(".codex") / "skills" / SKILL_NAME,
    "claude": Path(".claude") / "skills" / SKILL_NAME,
}


class ArchifyInstallError(RuntimeError):
    """Raised when an Archify installation cannot be safely prepared."""


class InstallPlan:
    """A no-write installation plan produced by :func:`preflight_install`."""

    __slots__ = ("source_root", "vendor_root", "skill_source", "targets", "owned_targets")

    def __init__(
        self,
        source_root: Path,
        vendor_root: Path,
        skill_source: Path,
        targets: dict[str, Path],
        owned_targets: frozenset[str],
    ) -> None:
        self.source_root = source_root
        self.vendor_root = vendor_root
        self.skill_source = skill_source
        self.targets = targets
        self.owned_targets = owned_targets

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InstallPlan):
            return NotImplemented
        return (
            self.source_root == other.source_root
            and self.vendor_root == other.vendor_root
            and self.skill_source == other.skill_source
            and self.targets == other.targets
            and self.owned_targets == other.owned_targets
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_root": str(self.source_root),
            "vendor_root": str(self.vendor_root),
            "skill_source": str(self.skill_source),
            "targets": {agent: str(path) for agent, path in self.targets.items()},
            "owned_targets": sorted(self.owned_targets),
            "tag": ARCHIFY_TAG,
            "commit": ARCHIFY_COMMIT,
            "version": ARCHIFY_VERSION,
            "license": ARCHIFY_LICENSE,
        }


def _as_root(value: str | Path | None, fallback: Path) -> Path:
    """Return an absolute target path without resolving away symlink evidence."""

    path = Path(value).expanduser() if value is not None else fallback.expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _read_marker(path: Path) -> Mapping[str, Any] | None:
    marker = path / MANAGED_MARKER_FILENAME
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    return value


def _is_owned_target(path: Path) -> bool:
    marker = _read_marker(path)
    return bool(
        marker
        and marker.get("schema_version") == 1
        and marker.get("managed_by") == MANAGED_BY
        and marker.get("skill") == SKILL_NAME
    )


def _assert_no_symlinks(path: Path, *, label: str) -> None:
    """Reject symlinks so copying never follows an unowned path."""

    if path.is_symlink():
        raise ArchifyInstallError(f"{label} must not be a symlink: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise ArchifyInstallError(f"{label} must be a directory: {path}")
    for child in path.rglob("*"):
        if child.is_symlink():
            raise ArchifyInstallError(f"{label} contains a symlink: {child}")


def _assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    """Reject a target whose name or any ancestor is a (including broken) link."""

    current = path
    while True:
        # Path.is_symlink() is an lstat check and remains true for a broken
        # link, unlike exists().  Do not resolve first: that would erase the
        # very escape route this preflight is meant to reject.
        if current.is_symlink():
            raise ArchifyInstallError(f"{label} has a symlink ancestor: {current}")
        if current.parent == current:
            return
        current = current.parent


def _assert_source(source_root: Path) -> tuple[Path, Path]:
    vendor_root = source_root / "vendor" / "archify"
    skill_source = vendor_root / "SKILL.md"
    try:
        verify_vendor(vendor_root)
        verify_skill_projection(vendor_root, source_root / "skills" / SKILL_NAME / "SKILL.md")
    except ArchifyContractError as error:
        raise ArchifyInstallError(str(error)) from error
    _assert_no_symlinks(vendor_root, label="Archify vendor")
    if skill_source.is_symlink():
        raise ArchifyInstallError(f"Archify Skill source must not be a symlink: {skill_source}")
    return vendor_root, skill_source


def _nearest_existing_parent(path: Path) -> Path:
    parent = path
    while not parent.exists():
        next_parent = parent.parent
        if next_parent == parent:
            break
        parent = next_parent
    return parent


def _assert_write_target(path: Path) -> None:
    """Check a target without creating it or any of its parent directories."""

    _assert_no_symlink_ancestors(path, label="Archify Skill target")
    if path.exists() and not path.is_dir():
        raise ArchifyInstallError(f"Refusing a non-directory Skill target: {path}")
    if path.exists() and not _is_owned_target(path):
        raise ArchifyInstallError(
            "Refusing to replace an unmanaged Archify Skill; reconcile it manually first: "
            f"{path}"
        )
    if path.exists():
        _assert_no_symlinks(path, label="Archify Skill target")
    parent = _nearest_existing_parent(path)
    if not parent.is_dir():
        raise ArchifyInstallError(f"Skill target parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ArchifyInstallError(f"Skill target parent is not writable: {parent}")
    if path.exists() and not os.access(path, os.W_OK | os.X_OK):
        raise ArchifyInstallError(f"Owned Archify Skill target is not writable: {path}")


def _target_paths(
    *,
    home: str | Path | None,
    codex_root: str | Path | None,
    claude_root: str | Path | None,
) -> dict[str, Path]:
    default_home = _as_root(home, Path.home())
    roots = {
        "codex": _as_root(codex_root, default_home),
        "claude": _as_root(claude_root, default_home),
    }
    return {agent: roots[agent] / relative for agent, relative in TARGETS.items()}


def preflight_install(
    source: str | Path = REPOSITORY_ROOT,
    *,
    home: str | Path | None = None,
    codex_root: str | Path | None = None,
    claude_root: str | Path | None = None,
) -> InstallPlan:
    """Validate source and both Codex/Claude endpoints without writing."""

    source_root = _as_root(source, REPOSITORY_ROOT)
    vendor_root, skill_source = _assert_source(source_root)
    targets = _target_paths(home=home, codex_root=codex_root, claude_root=claude_root)
    owned_targets: set[str] = set()
    # Validate both endpoints before the first mkdir/copy.  Ordering is fixed
    # to make a failing Claude preflight leave Codex completely untouched.
    for agent in ("codex", "claude"):
        target = targets[agent]
        _assert_write_target(target)
        if target.exists():
            owned_targets.add(agent)
    return InstallPlan(
        source_root=source_root,
        vendor_root=vendor_root,
        skill_source=skill_source,
        targets=targets,
        owned_targets=frozenset(owned_targets),
    )


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_symlink():
            raise ArchifyInstallError(f"Refusing to copy a symlink from vendor: {source_path}")
        if source_path.is_dir():
            if destination_path.exists() and not destination_path.is_dir():
                raise ArchifyInstallError(f"Skill destination is not a directory: {destination_path}")
            destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        if not source_path.is_file():
            raise ArchifyInstallError(f"Unsupported vendor entry: {source_path}")
        if destination_path.is_symlink():
            raise ArchifyInstallError(f"Refusing to replace a symlink in Skill target: {destination_path}")
        if destination_path.exists() and not destination_path.is_file():
            raise ArchifyInstallError(f"Skill destination is not a file: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source_path, destination_path)


def _remove_owned_tree(path: Path) -> None:
    """Remove only a transaction-created directory after a verified swap."""

    if path.is_symlink():
        raise ArchifyInstallError(f"transaction path unexpectedly became a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise ArchifyInstallError(f"transaction path unexpectedly is not a directory: {path}")
        shutil.rmtree(path)


def _prepare_staging(plan: InstallPlan) -> dict[str, Path]:
    """Copy both new endpoint trees before touching either live endpoint."""

    staged: dict[str, Path] = {}
    try:
        for agent in ("codex", "claude"):
            target = plan.targets[agent]
            _assert_no_symlink_ancestors(target, label="Archify Skill target")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _assert_no_symlink_ancestors(target, label="Archify Skill target")
            stage = Path(
                tempfile.mkdtemp(
                    prefix=f".{target.name}.stage-",
                    dir=target.parent,
                )
            )
            staged[agent] = stage
            _copy_tree(plan.vendor_root, stage)
            (stage / MANAGED_MARKER_FILENAME).write_text(_marker(agent), encoding="utf-8")
        return staged
    except Exception:
        for stage in staged.values():
            _remove_owned_tree(stage)
        raise


def _sibling_backup(target: Path) -> Path:
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    if backup.exists() or backup.is_symlink():
        raise ArchifyInstallError(f"transaction backup path is unexpectedly occupied: {backup}")
    return backup


def _transaction_record_path(targets: Mapping[str, Path]) -> Path:
    return targets["codex"].parent / TRANSACTION_RECORD_FILENAME


def _write_transaction_record(path: Path, value: Mapping[str, Any]) -> None:
    _assert_no_symlink_ancestors(path, label="Archify transaction record")
    if path.is_symlink():
        raise ArchifyInstallError(f"Archify transaction record must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.write-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except Exception:
        if temporary.exists() or temporary.is_symlink():
            if temporary.is_symlink() or not temporary.is_file():
                raise ArchifyInstallError(
                    f"Archify transaction temporary is invalid: {temporary}"
                )
            temporary.unlink()
        raise


def _remove_transaction_record(path: Path) -> None:
    if path.is_symlink():
        raise ArchifyInstallError(f"Archify transaction record became a symlink: {path}")
    if path.exists():
        if not path.is_file():
            raise ArchifyInstallError(f"Archify transaction record is not a file: {path}")
        path.unlink()


def _transaction_entry(target: Path, stage: Path) -> dict[str, object]:
    backup = _sibling_backup(target) if target.exists() else None
    return {
        "target": str(target),
        "stage": str(stage),
        "backup": str(backup) if backup is not None else None,
        "had_target": target.exists(),
        "phase": "prepared",
    }


def _new_transaction(plan: InstallPlan, staged: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "managed_by": MANAGED_BY,
        "state": "prepared",
        "endpoints": {
            agent: _transaction_entry(plan.targets[agent], staged[agent])
            for agent in ("codex", "claude")
        },
    }


def _load_transaction_record(targets: Mapping[str, Path]) -> tuple[Path, dict[str, Any]] | None:
    record_path = _transaction_record_path(targets)
    if not record_path.exists() and not record_path.is_symlink():
        return None
    if record_path.is_symlink() or not record_path.is_file():
        raise ArchifyInstallError(f"Archify transaction record is invalid: {record_path}")
    try:
        value = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArchifyInstallError(f"Archify transaction record is unreadable: {record_path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != TRANSACTION_SCHEMA_VERSION
        or value.get("managed_by") != MANAGED_BY
        or value.get("state") not in {"prepared", "swapping", "committed"}
        or not isinstance(value.get("endpoints"), dict)
        or set(value["endpoints"]) != set(TARGETS)
    ):
        raise ArchifyInstallError(f"Archify transaction record is invalid: {record_path}")
    for agent in ("codex", "claude"):
        endpoint = value["endpoints"][agent]
        target = targets[agent]
        if not isinstance(endpoint, dict):
            raise ArchifyInstallError(f"Archify transaction endpoint is invalid: {agent}")
        if endpoint.get("target") != str(target) or not isinstance(endpoint.get("had_target"), bool):
            raise ArchifyInstallError(f"Archify transaction target does not match: {agent}")
        for field, prefix in (("stage", f".{target.name}.stage-"), ("backup", f".{target.name}.backup-")):
            raw = endpoint.get(field)
            if raw is None and field == "backup" and not endpoint["had_target"]:
                continue
            if not isinstance(raw, str):
                raise ArchifyInstallError(f"Archify transaction {field} is invalid: {agent}")
            candidate = Path(raw)
            if candidate.parent != target.parent or not candidate.name.startswith(prefix):
                raise ArchifyInstallError(f"Archify transaction {field} escaped its target parent: {agent}")
            _assert_no_symlink_ancestors(candidate, label="Archify transaction path")
    return record_path, value


def _endpoint_paths(endpoint: Mapping[str, object]) -> tuple[Path, Path, Path | None]:
    target = Path(str(endpoint["target"]))
    stage = Path(str(endpoint["stage"]))
    raw_backup = endpoint.get("backup")
    return target, stage, Path(str(raw_backup)) if raw_backup is not None else None


def _rollback_transaction(record_path: Path, transaction: Mapping[str, Any]) -> None:
    errors: list[str] = []
    endpoints = transaction["endpoints"]
    for agent in ("claude", "codex"):
        endpoint = endpoints[agent]
        assert isinstance(endpoint, Mapping)
        target, stage, backup = _endpoint_paths(endpoint)
        try:
            if backup is not None and backup.exists():
                if target.exists() or target.is_symlink():
                    _remove_owned_tree(target)
                os.replace(backup, target)
            elif bool(endpoint["had_target"]):
                if not target.exists():
                    raise ArchifyInstallError(f"missing both live and backup target for {agent}")
            elif target.exists() or target.is_symlink():
                _remove_owned_tree(target)
            if stage.exists() or stage.is_symlink():
                _remove_owned_tree(stage)
        except Exception as error:  # pragma: no cover - catastrophic filesystem fault
            errors.append(f"{agent}: {error}")
    if errors:
        raise ArchifyInstallError(
            f"Archify transaction rollback failed; recovery record retained: {'; '.join(errors)}"
        )
    _remove_transaction_record(record_path)


def _finalize_committed_transaction(record_path: Path, transaction: Mapping[str, Any]) -> None:
    endpoints = transaction["endpoints"]
    for agent in ("codex", "claude"):
        endpoint = endpoints[agent]
        assert isinstance(endpoint, Mapping)
        target, stage, backup = _endpoint_paths(endpoint)
        if target.is_symlink() or not _is_owned_target(target):
            raise ArchifyInstallError(f"committed Archify target is not an owned Skill: {target}")
        if backup is not None and (backup.exists() or backup.is_symlink()):
            _remove_owned_tree(backup)
        if stage.exists() or stage.is_symlink():
            _remove_owned_tree(stage)
    _remove_transaction_record(record_path)


def recover_archify_transaction(
    *,
    home: str | Path | None = None,
    codex_root: str | Path | None = None,
    claude_root: str | Path | None = None,
) -> bool:
    """Recover an interrupted two-endpoint install before a later invocation."""

    targets = _target_paths(home=home, codex_root=codex_root, claude_root=claude_root)
    loaded = _load_transaction_record(targets)
    if loaded is None:
        return False
    record_path, transaction = loaded
    if transaction["state"] == "committed":
        _finalize_committed_transaction(record_path, transaction)
    else:
        _rollback_transaction(record_path, transaction)
    return True


def _commit_staging(plan: InstallPlan, staged: Mapping[str, Path]) -> None:
    """Swap both targets with a durable record recoverable after SIGKILL."""

    record_path = _transaction_record_path(plan.targets)
    transaction = _new_transaction(plan, staged)
    _write_transaction_record(record_path, transaction)
    try:
        transaction["state"] = "swapping"
        _write_transaction_record(record_path, transaction)
        endpoints = transaction["endpoints"]
        for agent in ("codex", "claude"):
            target = plan.targets[agent]
            endpoint = endpoints[agent]
            assert isinstance(endpoint, dict)
            stage = staged[agent]
            backup = Path(str(endpoint["backup"])) if endpoint["backup"] is not None else None
            _assert_no_symlink_ancestors(target, label="Archify Skill target")
            if backup is not None:
                os.replace(target, backup)
                endpoint["phase"] = "backed_up"
                _write_transaction_record(record_path, transaction)
            os.replace(stage, target)
            endpoint["phase"] = "installed"
            _write_transaction_record(record_path, transaction)
        transaction["state"] = "committed"
        _write_transaction_record(record_path, transaction)
        _finalize_committed_transaction(record_path, transaction)
    except Exception as error:
        try:
            loaded = _load_transaction_record(plan.targets)
            if loaded is not None:
                _rollback_transaction(*loaded)
        except Exception as rollback_error:  # pragma: no cover - catastrophic filesystem fault
            raise ArchifyInstallError(
                f"Archify atomic install failed: {error}; rollback also failed: {rollback_error}"
            ) from error
        raise ArchifyInstallError(f"Archify atomic install failed: {error}") from error


def _marker(agent: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "managed_by": MANAGED_BY,
            "skill": SKILL_NAME,
            "agent": agent,
            "repository": ARCHIFY_REPOSITORY,
            "tag": ARCHIFY_TAG,
            "commit": ARCHIFY_COMMIT,
            "version": ARCHIFY_VERSION,
            "license": ARCHIFY_LICENSE,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def install_archify(
    source: str | Path = REPOSITORY_ROOT,
    *,
    home: str | Path | None = None,
    codex_root: str | Path | None = None,
    claude_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install/update both owned targets after a complete no-write preflight."""

    recovered_transaction = False
    if not dry_run:
        recovered_transaction = recover_archify_transaction(
            home=home,
            codex_root=codex_root,
            claude_root=claude_root,
        )
    plan = preflight_install(
        source,
        home=home,
        codex_root=codex_root,
        claude_root=claude_root,
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "recovered_transaction": False, **plan.to_dict()}

    staged = _prepare_staging(plan)
    try:
        _commit_staging(plan, staged)
    except Exception:
        # A failed pre-record staging copy never touched live targets, but it
        # still owns its sibling temporary directories.
        for stage in staged.values():
            if stage.exists() or stage.is_symlink():
                _remove_owned_tree(stage)
        raise
    installed: dict[str, dict[str, str]] = {}
    for agent in ("codex", "claude"):
        target = plan.targets[agent]
        installed[agent] = {
            "skill": str(target / "SKILL.md"),
            "vendor_copy": str(target),
            "marker": str(target / MANAGED_MARKER_FILENAME),
        }
    return {
        "ok": True,
        "dry_run": False,
        "recovered_transaction": recovered_transaction,
        "tag": ARCHIFY_TAG,
        "commit": ARCHIFY_COMMIT,
        "version": ARCHIFY_VERSION,
        "license": ARCHIFY_LICENSE,
        "installed": installed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned Archify Skill and stable core for Codex and Claude Code."
    )
    parser.add_argument("--source", default=str(REPOSITORY_ROOT), help="Workbench checkout")
    parser.add_argument("--home", help="test/portable home root used by both agents")
    parser.add_argument("--codex-root", "--codex-home", dest="codex_root", help="Codex home root")
    parser.add_argument("--claude-root", "--claude-home", dest="claude_root", help="Claude home root")
    parser.add_argument(
        "--dry-run",
        "--check",
        dest="dry_run",
        action="store_true",
        help="preflight both endpoints and print the no-write plan",
    )
    args = parser.parse_args(argv)
    try:
        result = install_archify(
            args.source,
            home=args.home,
            codex_root=args.codex_root,
            claude_root=args.claude_root,
            dry_run=args.dry_run,
        )
    except (ArchifyInstallError, ArchifyContractError) as error:
        print(f"Archify preflight failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
