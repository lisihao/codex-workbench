#!/usr/bin/env python3
"""Install the Workbench canonical Code-as-Harness skill without replacing user policy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any, Mapping


SKILL_NAME = "code-as-harness"
PROFILE = "code-as-harness/v1"
SKILL_MARKER = 'codex_workbench_managed: "true"'
ARTIFACT_KIND = "workbench-canonical-compatible-skill"
POLICY_START = "<!-- CODEX-WORKBENCH-CODE-AS-HARNESS:START -->"
POLICY_END = "<!-- CODEX-WORKBENCH-CODE-AS-HARNESS:END -->"
REQUIRED_SKILL_TEXT = (
    "## Operating contract",
    "Fill all safe independent work slots",
    "A matching L3 fingerprint has one full gate",
    "A later user message continues the active objective",
)
LEGACY_COMPATIBLE_SKILL_TEXT = (
    "# Code as Harness",
    "## 1. Classify once",
    "## 5. Completion receipt",
    "references/aegis-integration.md",
    "references/tier-examples.md",
)
CANONICAL_SKILL_ROOT_RELATIVE_PATH = Path("skills") / SKILL_NAME
CANONICAL_SKILL_RELATIVE_PATH = CANONICAL_SKILL_ROOT_RELATIVE_PATH / "SKILL.md"
CANONICAL_SKILL_FILES = (
    Path("SKILL.md"),
    Path("references/aegis-integration.md"),
    Path("references/tier-examples.md"),
    Path("agents/openai.yaml"),
)
TRANSACTION_RECORD_FILENAME = ".code-as-harness.transaction.json"
TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_MANAGED_BY = "codex-workbench-code-as-harness"

TARGETS = {
    "codex": {
        "skill": Path(".codex") / "skills" / SKILL_NAME / "SKILL.md",
        "policy": Path(".codex") / "AGENTS.md",
    },
    "claude-code": {
        "skill": Path(".claude") / "skills" / SKILL_NAME / "SKILL.md",
        "policy": Path(".claude") / "CLAUDE.md",
    },
}
_SKILL_ENDPOINT_SUFFIXES = {
    Path("SKILL.md"): "skill",
    Path("references/aegis-integration.md"): "aegis-reference",
    Path("references/tier-examples.md"): "tier-reference",
    Path("agents/openai.yaml"): "agent-metadata",
}
_ENDPOINT_ORDER = tuple(
    endpoint
    for agent in TARGETS
    for endpoint in (
        *(f"{agent}-{suffix}" for suffix in _SKILL_ENDPOINT_SUFFIXES.values()),
        f"{agent}-policy",
    )
)
_TRANSACTION_FIELDS = frozenset({"schema_version", "managed_by", "state", "endpoints"})
_ENDPOINT_FIELDS = frozenset({"target", "stage", "backup", "had_target", "phase"})


def _normalized_home(home: Path) -> Path:
    home = home.expanduser()
    return home if home.is_absolute() else Path.cwd() / home


def policy_block(agent: str) -> str:
    return "\n".join(
        (
            POLICY_START,
            "## Codex Workbench Code-as-Harness (managed)",
            f"Profile: `{PROFILE}`. Canonical skill: `{SKILL_NAME}`.",
            "- Define the acceptance boundary and affected-path scope before editing.",
            "- Maximize useful safe parallelism; independent work may run together, conflicting writes may not.",
            "- Reuse passing evidence only for the same complete Evidence fingerprint; do not repeat an L3 full gate for that fingerprint.",
            "- Treat a later user message as steering for the active objective. Preserve it unless an explicit pause, cancel, or replacement is requested.",
            "- Confirm repeated friction with evidence, then prefer a code-level harness fix over a reminder-only rule.",
            f"- Target agent: `{agent}`.",
            POLICY_END,
        )
    )


def _frontmatter_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, str) else value
    return value


def _skill_frontmatter(text: str) -> dict[str, Any] | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", normalized, re.DOTALL)
    if match is None:
        return None
    frontmatter: dict[str, Any] = {}
    metadata: dict[str, str] | None = None
    for raw_line in match.group("frontmatter").splitlines():
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            return None
        if indent == 0:
            metadata = {} if key == "metadata" and not value.strip() else None
            if key in frontmatter:
                return None
            frontmatter[key] = metadata if metadata is not None else _frontmatter_scalar(value)
            continue
        if indent == 2 and metadata is not None and key not in metadata:
            metadata[key] = _frontmatter_scalar(value)
            continue
        return None
    return frontmatter


def _skill_body(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)\Z", normalized, re.DOTALL)
    return match.group("body") if match is not None else ""


def _visible_text(text: str) -> str:
    if text.count("<!--") != text.count("-->"):
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _is_managed_skill(text: str) -> bool:
    frontmatter = _skill_frontmatter(text)
    metadata = frontmatter.get("metadata") if frontmatter else None
    return bool(isinstance(metadata, dict) and metadata.get("codex_workbench_managed") == "true")


def _is_compatible_legacy_skill(text: str) -> bool:
    """Recognize the exact prior capability family without adopting arbitrary user content."""

    frontmatter = _skill_frontmatter(text)
    body = _visible_text(_skill_body(text))
    return bool(
        frontmatter
        and frontmatter.get("name") == SKILL_NAME
        and "metadata" not in frontmatter
        and all(marker in body for marker in LEGACY_COMPATIBLE_SKILL_TEXT)
    )


def _policy_marker_matches(text: str, marker: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"(?m)^{re.escape(marker)}[ \t\r]*$", text))


def canonical_skill(source: Path) -> Path:
    skill = source / CANONICAL_SKILL_RELATIVE_PATH
    if not skill.is_file():
        raise SystemExit(f"Canonical Code-as-Harness skill is missing: {skill}")
    text = skill.read_text(encoding="utf-8")
    frontmatter = _skill_frontmatter(text)
    metadata = frontmatter.get("metadata") if frontmatter else None
    body = _visible_text(_skill_body(text))
    required = (
        *REQUIRED_SKILL_TEXT,
    )
    if (
        frontmatter is None
        or frontmatter.get("name") != SKILL_NAME
        or not isinstance(metadata, dict)
        or metadata.get("codex_workbench_managed") != "true"
        or metadata.get("profile") != PROFILE
        or metadata.get("artifact_kind") != ARTIFACT_KIND
        or not all(marker in body for marker in required)
    ):
        raise SystemExit(f"Canonical Code-as-Harness skill is invalid: {skill}")
    root = source / CANONICAL_SKILL_ROOT_RELATIVE_PATH
    for relative in CANONICAL_SKILL_FILES:
        resource = root / relative
        _assert_no_symlink_ancestors(resource, label="Canonical Code-as-Harness resource")
        if not resource.is_file():
            raise SystemExit(f"Canonical Code-as-Harness resource is missing: {resource}")
    return skill


def _assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    """Reject target paths before resolving away live or broken symlinks."""

    current = path
    while True:
        if current.is_symlink():
            raise SystemExit(f"{label} has a symlink ancestor: {current}")
        if current.parent == current:
            return
        current = current.parent


def assert_skill_installable(destination: Path, *, adopt_compatible: bool = False) -> None:
    _assert_no_symlink_ancestors(destination, label="Code-as-Harness skill target")
    if destination.exists():
        if not destination.is_file():
            raise SystemExit(f"Refusing to replace non-file skill target: {destination}")
        current = destination.read_text(encoding="utf-8")
        if not _is_managed_skill(current) and not (
            adopt_compatible and _is_compatible_legacy_skill(current)
        ):
            raise SystemExit(
                "Refusing to replace an unmanaged Code-as-Harness skill; reconcile it manually first: "
                f"{destination}"
            )


def assert_write_target(path: Path) -> None:
    _assert_no_symlink_ancestors(path, label="Code-as-Harness target")
    if path.exists() and not path.is_file():
        raise SystemExit(f"Refusing to replace non-file target: {path}")
    if path.exists() and not os.access(path, os.W_OK):
        raise SystemExit(f"Target is not writable: {path}")
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise SystemExit(f"Target parent is not a directory: {ancestor}")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise SystemExit(f"Target parent is not writable: {ancestor}")


def install_skill(source_skill: Path, destination: Path) -> None:
    """Compatibility helper for a single verified target.

    The two-agent installer below stages every file together; keeping this
    helper safe prevents callers from reintroducing direct symlink-following
    copies outside that transaction.
    """

    assert_skill_installable(destination)
    assert_write_target(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_ancestors(destination, label="Code-as-Harness skill target")
    stage = _stage_bytes(destination, source_skill.read_bytes())
    _commit_single_staged_file(destination, stage)


def updated_policy(path: Path, block: str) -> str:
    _assert_no_symlink_ancestors(path, label="Code-as-Harness policy target")
    if path.exists() and not path.is_file():
        raise SystemExit(f"Refusing to replace non-file policy target: {path}")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    start_matches = _policy_marker_matches(current, POLICY_START)
    end_matches = _policy_marker_matches(current, POLICY_END)
    starts = len(start_matches)
    ends = len(end_matches)
    if starts != ends or starts > 1:
        raise SystemExit(
            "Refusing to update an ambiguous Code-as-Harness policy block: "
            f"{path}"
    )
    if starts == 1:
        start = start_matches[0].start()
        end_match = end_matches[0]
        if end_match.start() < start:
            raise SystemExit(
                "Refusing to update an out-of-order Code-as-Harness policy block: "
                f"{path}"
            )
        end = end_match.end()
        return current[:start] + block + current[end:]
    elif not current:
        return block + "\n"
    separator = "" if current.endswith("\n") else "\n"
    return current + separator + "\n" + block + "\n"


def preflight_code_as_harness(
    source: Path,
    home: Path,
    *,
    adopt_compatible: bool = False,
) -> tuple[Path, dict[str, dict[str, Any]]]:
    home = _normalized_home(home)
    source_skill = canonical_skill(source)
    source_root = source_skill.parent
    prepared: dict[str, dict[str, Any]] = {}
    for agent, target in TARGETS.items():
        skill = home / target["skill"]
        skill_root = skill.parent
        prepared[agent] = {
            "skill": skill,
            "skill_files": {
                skill_root / relative: source_root / relative
                for relative in CANONICAL_SKILL_FILES
            },
            "policy": home / target["policy"],
            "policy_content": updated_policy(home / target["policy"], policy_block(agent)),
        }
    for target in prepared.values():
        assert_skill_installable(target["skill"], adopt_compatible=adopt_compatible)
        for destination in target["skill_files"]:
            assert_write_target(destination)
        assert_write_target(target["policy"])
    return source_skill, prepared


def _stage_bytes(target: Path, content: bytes) -> Path:
    """Write a sibling stage file only after its target ancestry is safe."""

    _assert_no_symlink_ancestors(target, label="Code-as-Harness target")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_ancestors(target, label="Code-as-Harness target")
    descriptor, raw_stage = tempfile.mkstemp(
        prefix=f".{target.name}.stage-",
        dir=target.parent,
    )
    stage = Path(raw_stage)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        return stage
    except Exception:
        stage.unlink(missing_ok=True)
        raise


def _remove_transaction_file(path: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"transaction path unexpectedly became a symlink: {path}")
    if path.exists():
        if not path.is_file():
            raise SystemExit(f"transaction path unexpectedly is not a file: {path}")
        path.unlink()


def _sibling_backup(target: Path) -> Path:
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    if backup.exists() or backup.is_symlink():
        raise SystemExit(f"transaction backup path is unexpectedly occupied: {backup}")
    return backup


def _commit_single_staged_file(target: Path, stage: Path) -> None:
    """Keep the legacy one-file helper atomic without inventing a dual-endpoint record."""

    backup: Path | None = None
    try:
        _assert_no_symlink_ancestors(target, label="Code-as-Harness target")
        if target.exists():
            backup = _sibling_backup(target)
            os.replace(target, backup)
        os.replace(stage, target)
    except Exception as error:
        try:
            if target.exists() or target.is_symlink():
                _remove_transaction_file(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        except Exception as rollback_error:  # pragma: no cover - catastrophic filesystem fault
            raise SystemExit(
                f"Code-as-Harness single-file install failed: {error}; rollback also failed: {rollback_error}"
            ) from error
        raise SystemExit(f"Code-as-Harness single-file install failed: {error}") from error
    else:
        if backup is not None:
            _remove_transaction_file(backup)
    finally:
        if stage.exists() or stage.is_symlink():
            _remove_transaction_file(stage)


def _transaction_targets(home: Path) -> dict[str, Path]:
    targets: dict[str, Path] = {}
    for agent, configured in TARGETS.items():
        skill = home / configured["skill"]
        for relative, suffix in _SKILL_ENDPOINT_SUFFIXES.items():
            targets[f"{agent}-{suffix}"] = skill.parent / relative
        targets[f"{agent}-policy"] = home / configured["policy"]
    return targets


def _transaction_record_path(home: Path) -> Path:
    return home / TRANSACTION_RECORD_FILENAME


def _write_transaction_record(path: Path, value: Mapping[str, Any]) -> None:
    _assert_no_symlink_ancestors(path, label="Code-as-Harness transaction record")
    if path.is_symlink():
        raise SystemExit(f"Code-as-Harness transaction record must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_ancestors(path, label="Code-as-Harness transaction record")
    temporary = path.with_name(f".{path.name}.write-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except Exception:
        if temporary.exists() or temporary.is_symlink():
            _remove_transaction_file(temporary)
        raise


def _remove_transaction_record(path: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"Code-as-Harness transaction record became a symlink: {path}")
    _remove_transaction_file(path)


def _transaction_entry(target: Path, stage: Path) -> dict[str, object]:
    had_target = target.exists()
    backup = _sibling_backup(target) if had_target else None
    return {
        "target": str(target),
        "stage": str(stage),
        "backup": str(backup) if backup is not None else None,
        "had_target": had_target,
        "phase": "prepared",
    }


def _new_transaction(home: Path, staged: Mapping[Path, Path]) -> dict[str, Any]:
    targets = _transaction_targets(home)
    if set(staged) != set(targets.values()):
        raise SystemExit("Code-as-Harness transaction endpoints do not match the dual-agent install plan")
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "managed_by": TRANSACTION_MANAGED_BY,
        "state": "prepared",
        "endpoints": {
            name: _transaction_entry(targets[name], staged[targets[name]])
            for name in _ENDPOINT_ORDER
        },
    }


def _load_transaction_record(home: Path) -> tuple[Path, dict[str, Any]] | None:
    targets = _transaction_targets(home)
    record_path = _transaction_record_path(home)
    if not record_path.exists() and not record_path.is_symlink():
        return None
    if record_path.is_symlink() or not record_path.is_file():
        raise SystemExit(f"Code-as-Harness transaction record is invalid: {record_path}")
    try:
        transaction = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"Code-as-Harness transaction record is unreadable: {record_path}: {error}"
        ) from error
    if (
        not isinstance(transaction, dict)
        or set(transaction) != _TRANSACTION_FIELDS
        or transaction.get("schema_version") != TRANSACTION_SCHEMA_VERSION
        or transaction.get("managed_by") != TRANSACTION_MANAGED_BY
        or transaction.get("state") not in {"prepared", "swapping", "committed"}
        or not isinstance(transaction.get("endpoints"), dict)
        or set(transaction["endpoints"]) != set(_ENDPOINT_ORDER)
    ):
        raise SystemExit(f"Code-as-Harness transaction record is invalid: {record_path}")
    for name in _ENDPOINT_ORDER:
        endpoint = transaction["endpoints"][name]
        target = targets[name]
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != _ENDPOINT_FIELDS
            or endpoint.get("target") != str(target)
            or not isinstance(endpoint.get("had_target"), bool)
            or endpoint.get("phase") not in {"prepared", "backed_up", "installed"}
        ):
            raise SystemExit(f"Code-as-Harness transaction endpoint is invalid: {name}")
        _assert_no_symlink_ancestors(target, label="Code-as-Harness transaction target")
        for field, prefix in (("stage", f".{target.name}.stage-"), ("backup", f".{target.name}.backup-")):
            raw = endpoint[field]
            if raw is None and field == "backup" and not endpoint["had_target"]:
                continue
            if not isinstance(raw, str):
                raise SystemExit(f"Code-as-Harness transaction {field} is invalid: {name}")
            candidate = Path(raw)
            if candidate.parent != target.parent or not candidate.name.startswith(prefix):
                raise SystemExit(
                    f"Code-as-Harness transaction {field} escaped its target parent: {name}"
                )
            _assert_no_symlink_ancestors(candidate, label="Code-as-Harness transaction path")
    return record_path, transaction


def _endpoint_paths(endpoint: Mapping[str, object]) -> tuple[Path, Path, Path | None]:
    target = Path(str(endpoint["target"]))
    stage = Path(str(endpoint["stage"]))
    raw_backup = endpoint["backup"]
    return target, stage, Path(str(raw_backup)) if raw_backup is not None else None


def _rollback_transaction(record_path: Path, transaction: Mapping[str, Any]) -> None:
    errors: list[str] = []
    endpoints = transaction["endpoints"]
    assert isinstance(endpoints, Mapping)
    for name in reversed(_ENDPOINT_ORDER):
        endpoint = endpoints[name]
        assert isinstance(endpoint, Mapping)
        target, stage, backup = _endpoint_paths(endpoint)
        try:
            if backup is not None and (backup.exists() or backup.is_symlink()):
                if target.exists() or target.is_symlink():
                    _remove_transaction_file(target)
                os.replace(backup, target)
            elif bool(endpoint["had_target"]):
                if not target.exists() or target.is_symlink():
                    raise SystemExit(f"missing both live and backup target for {name}")
            elif target.exists() or target.is_symlink():
                _remove_transaction_file(target)
            if stage.exists() or stage.is_symlink():
                _remove_transaction_file(stage)
        except Exception as error:  # pragma: no cover - catastrophic filesystem fault
            errors.append(f"{name}: {error}")
    if errors:
        raise SystemExit(
            "Code-as-Harness transaction rollback failed; recovery record retained: "
            + "; ".join(errors)
        )
    _remove_transaction_record(record_path)


def _finalize_committed_transaction(record_path: Path, transaction: Mapping[str, Any]) -> None:
    endpoints = transaction["endpoints"]
    assert isinstance(endpoints, Mapping)
    for name in _ENDPOINT_ORDER:
        endpoint = endpoints[name]
        assert isinstance(endpoint, Mapping)
        target, stage, backup = _endpoint_paths(endpoint)
        if target.is_symlink() or not target.is_file():
            raise SystemExit(f"committed Code-as-Harness target is not a regular file: {target}")
        if backup is not None and (backup.exists() or backup.is_symlink()):
            _remove_transaction_file(backup)
        if stage.exists() or stage.is_symlink():
            _remove_transaction_file(stage)
    _remove_transaction_record(record_path)


def recover_code_as_harness_transaction(home: Path) -> bool:
    """Restore both agent configurations after a prior interrupted swap."""

    home = _normalized_home(home)
    loaded = _load_transaction_record(home)
    if loaded is None:
        return False
    record_path, transaction = loaded
    if transaction["state"] == "committed":
        _finalize_committed_transaction(record_path, transaction)
    else:
        _rollback_transaction(record_path, transaction)
    return True


def _commit_staged_files(home: Path, staged: Mapping[Path, Path]) -> None:
    """Swap all Codex/Claude endpoints with durable SIGKILL recovery."""

    record_path = _transaction_record_path(home)
    transaction = _new_transaction(home, staged)
    try:
        _write_transaction_record(record_path, transaction)
    except Exception:
        for stage in staged.values():
            if stage.exists() or stage.is_symlink():
                _remove_transaction_file(stage)
        raise
    try:
        transaction["state"] = "swapping"
        _write_transaction_record(record_path, transaction)
        endpoints = transaction["endpoints"]
        assert isinstance(endpoints, dict)
        for name in _ENDPOINT_ORDER:
            endpoint = endpoints[name]
            assert isinstance(endpoint, dict)
            target, stage, backup = _endpoint_paths(endpoint)
            _assert_no_symlink_ancestors(target, label="Code-as-Harness target")
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
            loaded = _load_transaction_record(home)
            if loaded is not None:
                _rollback_transaction(*loaded)
        except Exception as rollback_error:  # pragma: no cover - catastrophic filesystem fault
            raise SystemExit(
                f"Code-as-Harness atomic install failed: {error}; rollback also failed: {rollback_error}"
            ) from error
        raise SystemExit(f"Code-as-Harness atomic install failed: {error}") from error


def install_code_as_harness(
    source: Path,
    home: Path,
    *,
    adopt_compatible: bool = False,
) -> dict[str, dict[str, str]]:
    home = _normalized_home(home)
    recover_code_as_harness_transaction(home)
    source_skill, prepared = preflight_code_as_harness(
        source,
        home,
        adopt_compatible=adopt_compatible,
    )
    staged: dict[Path, Path] = {}
    try:
        for agent in TARGETS:
            target = prepared[agent]
            skill = target["skill"]
            skill_files = target["skill_files"]
            policy = target["policy"]
            policy_content = target["policy_content"]
            assert isinstance(skill, Path)
            assert isinstance(skill_files, dict)
            assert isinstance(policy, Path)
            assert isinstance(policy_content, str)
            for destination, source_file in skill_files.items():
                assert isinstance(destination, Path)
                assert isinstance(source_file, Path)
                staged[destination] = _stage_bytes(destination, source_file.read_bytes())
            staged[policy] = _stage_bytes(policy, policy_content.encode("utf-8"))
    except Exception:
        for stage in staged.values():
            if stage.exists() or stage.is_symlink():
                _remove_transaction_file(stage)
        raise
    _commit_staged_files(home, staged)
    installed: dict[str, dict[str, str]] = {}
    for agent, target in prepared.items():
        skill = target["skill"]
        policy = target["policy"]
        assert isinstance(skill, Path)
        assert isinstance(policy, Path)
        installed[agent] = {"skill": str(skill), "policy": str(policy)}
    return installed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install Workbench Code-as-Harness for Codex and Claude Code."
    )
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate both agent targets without writing any files",
    )
    parser.add_argument(
        "--adopt-compatible",
        action="store_true",
        help="upgrade the recognized prior Code-as-Harness skill while preserving its capabilities",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    home = _normalized_home(Path(args.home))
    if args.check:
        preflight_code_as_harness(source, home, adopt_compatible=args.adopt_compatible)
        print("Code-as-Harness preflight: ok")
        return 0
    installed = install_code_as_harness(
        source,
        home,
        adopt_compatible=args.adopt_compatible,
    )
    for agent, paths in installed.items():
        print(f"{agent}: skill={paths['skill']} policy={paths['policy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
