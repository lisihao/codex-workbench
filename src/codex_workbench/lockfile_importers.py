"""Deterministic, byte-preserving repair of pnpm workspace importer links.

This is deliberately a narrow editor for the pnpm v9 layout generated in this
repository, not a general YAML parser. It only inserts missing
``workspace:*``, ``workspace:^``, or ``workspace:~`` importer records.
"""

from __future__ import annotations

from hashlib import sha256
import glob
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
from typing import Any


LOCKFILE = "pnpm-lock.yaml"
WORKSPACE_FILE = "pnpm-workspace.yaml"
SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
WORKSPACE_SPECS = frozenset({"workspace:*", "workspace:^", "workspace:~"})
SCHEMA_VERSION = 1


class WorkspaceImporterRepairError(ValueError):
    """The input is not safe for the restricted workspace importer editor."""


def plan_workspace_importer_repair(root: Path) -> dict[str, Any]:
    """Read a workspace and return a fully bound pnpm importer repair plan.

    The plan contains the original lockfile bytes for artifact storage and the
    exact UTF-8 replacement text. It never writes the workspace.
    """

    root = _root(root)
    _, workspace_bytes = _read(root, WORKSPACE_FILE)
    manifests = _manifests(root, _workspace_dirs(root, _patterns(_text(workspace_bytes))))
    by_importer = {item["importer"]: item for item in manifests}
    names: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        if manifest["name"] is None:
            continue
        if manifest["name"] in names:
            raise WorkspaceImporterRepairError("workspace package names must be unique")
        names[manifest["name"]] = manifest

    _, before = _read(root, LOCKFILE)
    lock = _lockfile(_text(before))
    changed, summary, inserts = _repair(by_importer, names, lock)
    new_lockfile = _insert(lock, inserts)
    prefix = "".join(lock["lines"][: lock["start"] + 1])
    suffix = "".join(lock["lines"][lock["end"] :])
    if not new_lockfile.startswith(prefix) or not new_lockfile.endswith(suffix):
        raise WorkspaceImporterRepairError("repair would modify bytes outside pnpm importers")

    before_sha256 = _digest(before)
    after_sha256 = _digest(new_lockfile.encode("utf-8"))
    manifest_files = {manifest["path"]: _digest(manifest["bytes"]) for manifest in manifests}
    config_files = {WORKSPACE_FILE: _digest(workspace_bytes)}
    input_files = dict(sorted({**manifest_files, **config_files, LOCKFILE: before_sha256}.items()))
    manifest_files = dict(sorted(manifest_files.items()))
    fingerprint_data = {
        "schema_version": SCHEMA_VERSION,
        "input_files": input_files,
        "manifest_files": manifest_files,
        "workspace_config_files": config_files,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "new_lockfile_sha256": after_sha256,
        "changed_importers": changed,
        "workspace_entries": summary,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "lockfile": LOCKFILE,
        "input_files": input_files,
        "manifest_files": manifest_files,
        "workspace_config_files": config_files,
        "before_lockfile": before,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "new_lockfile": new_lockfile,
        "changed_importers": changed,
        "workspace_entries": summary,
        "fingerprint": _digest(_json(fingerprint_data).encode("utf-8")),
    }


def apply_workspace_importer_repair(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Replan and apply exactly one unchanged plan, writing only pnpm-lock.yaml."""

    if not isinstance(plan, dict):
        raise WorkspaceImporterRepairError("workspace importer repair plan must be a dictionary")
    actual = plan_workspace_importer_repair(root)
    if plan != actual:
        raise WorkspaceImporterRepairError(
            "workspace importer repair plan no longer matches the complete current input"
        )
    if not actual["changed_importers"]:
        raise WorkspaceImporterRepairError("workspace importer repair plan has no changes to apply")
    output = actual["new_lockfile"].encode("utf-8")
    if _digest(output) != actual["after_sha256"]:
        raise WorkspaceImporterRepairError("recomputed pnpm lockfile digest is invalid")
    root = _root(root)
    path, current = _read(root, LOCKFILE)
    if current != actual["before_lockfile"]:
        raise WorkspaceImporterRepairError("pnpm lockfile changed before the repair could apply")
    _write_lockfile(root, path, current, output)
    return {
        "schema_version": SCHEMA_VERSION,
        "lockfile": LOCKFILE,
        "before_sha256": actual["before_sha256"],
        "after_sha256": actual["after_sha256"],
        "fingerprint": actual["fingerprint"],
        "changed_importers": actual["changed_importers"],
    }


def _root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise WorkspaceImporterRepairError("workspace root must be a pathlib.Path")
    supplied = Path(os.path.abspath(os.fspath(root)))
    try:
        mode = os.lstat(supplied).st_mode
    except OSError as error:
        raise WorkspaceImporterRepairError("workspace root is unavailable") from error
    if stat.S_ISLNK(mode):
        raise WorkspaceImporterRepairError("workspace root must not be a symbolic link")
    try:
        physical = supplied.resolve(strict=True)
    except OSError as error:
        raise WorkspaceImporterRepairError("workspace root cannot be resolved") from error
    if not physical.is_dir():
        raise WorkspaceImporterRepairError("workspace root must be a directory")
    return physical


def _path(
    root: Path, value: str | Path, *, directory: bool | None = False, missing: bool = False
) -> Path | None:
    if isinstance(value, Path):
        path = Path(os.path.abspath(os.fspath(value)))
    else:
        parsed = PurePosixPath(value)
        if not value or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
            raise WorkspaceImporterRepairError("workspace path escapes the workspace root")
        path = root.joinpath(*parsed.parts)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise WorkspaceImporterRepairError("workspace path escapes the workspace root") from error
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError as error:
            if missing and index == len(relative.parts) - 1:
                return None
            raise WorkspaceImporterRepairError("workspace input is unavailable") from error
        except OSError as error:
            raise WorkspaceImporterRepairError("workspace input is unavailable") from error
        if stat.S_ISLNK(mode):
            raise WorkspaceImporterRepairError("workspace inputs must not use symbolic links")
    if directory is True and not stat.S_ISDIR(mode):
        raise WorkspaceImporterRepairError("workspace package glob must resolve to directories")
    if directory is False and not stat.S_ISREG(mode):
        raise WorkspaceImporterRepairError("workspace input must be a regular file")
    return path


def _read(root: Path, value: str | Path) -> tuple[Path, bytes]:
    path = _path(root, value)
    assert path is not None
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise WorkspaceImporterRepairError("workspace input cannot be read") from error


def _text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceImporterRepairError("workspace inputs must be UTF-8") from error
    if "\r" in text or not text.endswith("\n"):
        raise WorkspaceImporterRepairError("workspace inputs must use LF and end with LF")
    return text


def _patterns(text: str) -> tuple[str, ...]:
    lines = text.splitlines(keepends=True)
    indexes = [index for index, line in enumerate(lines) if line == "packages:\n"]
    if len(indexes) != 1:
        raise WorkspaceImporterRepairError("pnpm-workspace.yaml needs one top-level packages list")
    patterns: list[str] = []
    for line in lines[indexes[0] + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) == 0:
            break
        if _indent(line) != 2 or not line.startswith("  - "):
            raise WorkspaceImporterRepairError("workspace packages need standard two-space list syntax")
        raw = line[4:-1]
        pattern = raw if raw.startswith("!") and not raw.startswith("!!") else _scalar(raw)
        patterns.append(_pattern(pattern))
    return tuple(patterns)


def _pattern(pattern: str) -> str:
    if not pattern or "\x00" in pattern or "\\" in pattern:
        raise WorkspaceImporterRepairError("workspace package pattern is invalid")
    body = pattern[1:] if pattern.startswith("!") else pattern
    parsed = PurePosixPath(body)
    if not body or parsed.is_absolute() or any(part == ".." for part in parsed.parts):
        raise WorkspaceImporterRepairError("workspace package pattern escapes the workspace root")
    if body != "." and any(part in {"", "."} for part in parsed.parts):
        raise WorkspaceImporterRepairError("workspace package pattern is invalid")
    return pattern


def _workspace_dirs(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    if _path(root, root / "package.json", missing=True) is None:
        raise WorkspaceImporterRepairError("workspace root needs a regular package.json")
    included: set[Path] = {root}
    excluded: set[Path] = set()
    for pattern in patterns:
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        matches = [root] if body == "." else [Path(item) for item in glob.glob(str(root / body), recursive=True)]
        target = excluded if negated else included
        for match in matches:
            try:
                mode = os.lstat(match).st_mode
            except OSError as error:
                raise WorkspaceImporterRepairError("workspace package glob is unavailable") from error
            if stat.S_ISLNK(mode):
                try:
                    linked_mode = os.stat(match).st_mode
                except OSError as error:
                    raise WorkspaceImporterRepairError("workspace package symbolic link is unavailable") from error
                if stat.S_ISDIR(linked_mode):
                    raise WorkspaceImporterRepairError("workspace inputs must not use symbolic links")
                continue
            checked = _path(root, match, directory=None)
            assert checked is not None
            if checked.is_dir():
                target.add(checked)
    directories = sorted(included - excluded, key=lambda item: item.relative_to(root).as_posix())
    return tuple(directory for directory in directories if _path(root, directory / "package.json", missing=True) is not None)


def _manifests(root: Path, directories: tuple[Path, ...]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for directory in directories:
        path, raw = _read(root, directory / "package.json")
        try:
            data = json.loads(_text(raw))
        except json.JSONDecodeError as error:
            raise WorkspaceImporterRepairError("workspace package.json is invalid JSON") from error
        if not isinstance(data, dict):
            raise WorkspaceImporterRepairError("workspace package.json must be an object")
        name = data.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise WorkspaceImporterRepairError("workspace package name must be a non-empty string")
        dependencies: dict[str, dict[str, str]] = {}
        for section in SECTIONS:
            values = data.get(section, {})
            if not isinstance(values, dict):
                raise WorkspaceImporterRepairError(f"package.json {section} must be an object")
            normalized: dict[str, str] = {}
            for dependency, specifier in values.items():
                if not isinstance(dependency, str) or not dependency or not isinstance(specifier, str) or not specifier:
                    raise WorkspaceImporterRepairError("package.json dependency is invalid")
                if specifier.startswith("workspace:") and specifier not in WORKSPACE_SPECS:
                    raise WorkspaceImporterRepairError("only workspace:*, workspace:^, and workspace:~ are repairable")
                normalized[dependency] = specifier
            dependencies[section] = normalized
        relative = directory.relative_to(root).as_posix()
        result.append({
            "importer": "." if relative == "." else relative,
            "path": path.relative_to(root).as_posix(),
            "bytes": raw,
            "name": name,
            "dependencies": dependencies,
        })
    return tuple(sorted(result, key=lambda item: _sort_key(item["importer"])))


def _lockfile(text: str) -> dict[str, Any]:
    lines = tuple(text.splitlines(keepends=True))
    if "\t" in text or "\r" in text or sum(line == "lockfileVersion: '9.0'\n" for line in lines) != 1:
        raise WorkspaceImporterRepairError("only standard pnpm lockfileVersion '9.0' is supported")
    indexes = [index for index, line in enumerate(lines) if line == "importers:\n"]
    if len(indexes) != 1:
        raise WorkspaceImporterRepairError("pnpm lockfile needs one top-level importers mapping")
    start = indexes[0]
    end = next((index for index in range(start + 1, len(lines)) if lines[index].strip() and _indent(lines[index]) == 0), len(lines))
    headers = [index for index in range(start + 1, end) if lines[index].strip() and _indent(lines[index]) == 2]
    if not headers and any(line.strip() for line in lines[start + 1 : end]):
        raise WorkspaceImporterRepairError("pnpm importers must use two-space keys")
    importers: dict[str, dict[str, Any]] = {}
    for offset, header in enumerate(headers):
        importer_end = headers[offset + 1] if offset + 1 < len(headers) else end
        importer = _importer(lines, header, importer_end)
        if importer["key"] in importers:
            raise WorkspaceImporterRepairError("pnpm importer keys must be unique")
        importers[importer["key"]] = importer
    return {"lines": lines, "start": start, "end": end, "importers": importers}


def _importer(lines: tuple[str, ...], start: int, end: int) -> dict[str, Any]:
    header = lines[start]
    if header.endswith(": {}\n"):
        key = _importer_key(header[2:-5])
        if any(line.strip() for line in lines[start + 1 : end]):
            raise WorkspaceImporterRepairError("empty pnpm importer has nested content")
        return {"key": key, "start": start, "sections": {}}
    if not header.endswith(":\n"):
        raise WorkspaceImporterRepairError("pnpm importer header is malformed")
    key = _importer_key(header[2:-2])
    if any(line.strip() and _indent(line) < 4 for line in lines[start + 1 : end]):
        raise WorkspaceImporterRepairError("pnpm importer indentation is malformed")
    starts = [index for index in range(start + 1, end) if lines[index].strip() and _indent(lines[index]) == 4]
    sections: dict[str, dict[str, Any]] = {}
    for offset, section_start in enumerate(starts):
        section_end = starts[offset + 1] if offset + 1 < len(starts) else end
        section = _section(lines, section_start, section_end)
        if section is not None:
            if section["name"] in sections:
                raise WorkspaceImporterRepairError("pnpm importer dependency section is duplicated")
            sections[section["name"]] = section
    return {"key": key, "start": start, "sections": sections}


def _importer_key(raw: str) -> str:
    key = _scalar(raw)
    if key == ".":
        return key
    parsed = PurePosixPath(key)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise WorkspaceImporterRepairError("pnpm importer key escapes the workspace root")
    return key


def _section(lines: tuple[str, ...], start: int, end: int) -> dict[str, Any] | None:
    header = lines[start]
    if not header.endswith(":\n"):
        raise WorkspaceImporterRepairError("pnpm importer section is malformed")
    name = header[4:-2]
    if name not in SECTIONS:
        return None
    entries: dict[str, dict[str, Any]] = {}
    index = start + 1
    while index < end:
        if not lines[index].strip():
            index += 1
            continue
        line = lines[index]
        if _indent(line) != 6 or not line.endswith(":\n"):
            raise WorkspaceImporterRepairError("pnpm importer dependency record is malformed")
        entry_start, dependency = index, _scalar(line[6:-2])
        if not dependency or dependency in entries:
            raise WorkspaceImporterRepairError("pnpm importer dependency names must be unique")
        index += 1
        fields: dict[str, str] = {}
        while index < end:
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if _indent(line) == 6:
                break
            match = re.fullmatch(r"        (specifier|version): (.+)\n", line)
            if match is None or match.group(1) in fields:
                raise WorkspaceImporterRepairError("pnpm importer dependency fields are malformed")
            fields[match.group(1)] = _scalar(match.group(2))
            index += 1
        if set(fields) != {"specifier", "version"}:
            raise WorkspaceImporterRepairError("pnpm importer dependency needs specifier and version")
        entries[dependency] = {"specifier": fields["specifier"], "version": fields["version"], "start": entry_start}
    insertion = end
    while insertion > start + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    return {"name": name, "entries": entries, "insertion": insertion}


def _scalar(raw: str) -> str:
    if not raw or raw != raw.strip() or "\x00" in raw:
        raise WorkspaceImporterRepairError("pnpm YAML scalar is unsupported")
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WorkspaceImporterRepairError("pnpm YAML scalar is unsupported") from error
        if isinstance(value, str):
            return value
    if raw.startswith(("'", '"', "[", "{", "&", "*", "!", "|", ">")) or " #" in raw:
        raise WorkspaceImporterRepairError("pnpm YAML scalar is unsupported")
    return raw


def _repair(
    manifests: dict[str, dict[str, Any]], names: dict[str, dict[str, Any]], lock: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, list[str]]]:
    changed: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    inserts: dict[int, list[str]] = {}
    new_importers: dict[int, list[tuple[str, str]]] = {}
    for importer_name in sorted(manifests, key=_sort_key):
        manifest = manifests[importer_name]
        expected = _workspace_dependencies(manifest, names)
        importer = lock["importers"].get(importer_name)
        missing: dict[str, list[dict[str, str]]] = {}
        if importer is None:
            if expected:
                if any(
                    not specifier.startswith("workspace:")
                    for values in manifest["dependencies"].values()
                    for specifier in values.values()
                ):
                    raise WorkspaceImporterRepairError(
                        "cannot create an importer while a third-party entry is absent from the lockfile"
                    )
                additions = _flatten(expected)
                changed.append({"importer": importer_name, "manifest": manifest["path"], "created": True, "added": additions})
                new_importers.setdefault(_new_importer_index(lock, importer_name), []).append(
                    (importer_name, _new_importer(importer_name, expected))
                )
                missing = expected
        else:
            for section in SECTIONS:
                wanted = expected.get(section, [])
                if not wanted:
                    continue
                current = importer["sections"].get(section)
                for entry in wanted:
                    present = (
                        current["entries"].get(entry["name"])
                        if current is not None
                        else None
                    )
                    if present is None:
                        present = next(
                            (
                                other["entries"][entry["name"]]
                                for other in importer["sections"].values()
                                if entry["name"] in other["entries"]
                            ),
                            None,
                        )
                    if present is None:
                        if current is None:
                            raise WorkspaceImporterRepairError(
                                "pnpm importer is missing a dependency section and cannot be synthesized safely"
                            )
                        missing.setdefault(section, []).append(entry)
                    elif present["version"] != entry["version"]:
                        raise WorkspaceImporterRepairError(
                            "existing workspace importer link differs and will not be rewritten"
                        )
            if missing:
                additions = _flatten(missing)
                changed.append({"importer": importer_name, "manifest": manifest["path"], "created": False, "added": additions})
                for section, entries in missing.items():
                    current = importer["sections"][section]
                    records = sorted(current["entries"].items())
                    for entry in entries:
                        index = next((record[1]["start"] for record in records if record[0] > entry["name"]), current["insertion"])
                        inserts.setdefault(index, []).append(_entry(entry))
        for section in SECTIONS:
            missing_names = {entry["name"] for entry in missing.get(section, [])}
            for entry in expected.get(section, []):
                summary.append({
                    "importer": importer_name,
                    "manifest": manifest["path"],
                    "package_name": manifest["name"],
                    "section": section,
                    **entry,
                    "status": "missing" if entry["name"] in missing_names else "present",
                })
    for index, importers in new_importers.items():
        ordered = [text for _, text in sorted(importers)]
        prefix = "" if index and not lock["lines"][index - 1].strip() else "\n"
        inserts.setdefault(index, []).append(prefix + "\n".join(ordered) + "\n")
    changed.sort(key=lambda item: _sort_key(item["importer"]))
    summary.sort(key=lambda item: (_sort_key(item["importer"]), item["section"], item["name"]))
    return changed, summary, inserts


def _workspace_dependencies(manifest: dict[str, Any], names: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    expected: dict[str, list[dict[str, str]]] = {}
    for section in SECTIONS:
        values: list[dict[str, str]] = []
        for name, specifier in sorted(manifest["dependencies"][section].items()):
            if not specifier.startswith("workspace:"):
                continue
            target = names.get(name)
            if target is None:
                raise WorkspaceImporterRepairError("workspace dependency does not name a configured workspace package")
            target_name = target["importer"]
            link = posixpath.relpath(target_name, start=manifest["importer"])
            values.append({"name": name, "specifier": specifier, "version": f"link:{link}", "target_importer": target_name})
        if values:
            expected[section] = values
    return expected


def _flatten(entries: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    return [{"section": section, **entry} for section in SECTIONS for entry in entries.get(section, [])]


def _new_importer_index(lock: dict[str, Any], importer: str) -> int:
    desired = _sort_key(importer)
    for value in sorted(lock["importers"].values(), key=lambda item: _sort_key(item["key"])):
        if _sort_key(value["key"]) > desired:
            return value["start"]
    return lock["end"]


def _entry(entry: dict[str, str]) -> str:
    return (
        f"      {_key(entry['name'])}:\n"
        f"        specifier: {_value(entry['specifier'])}\n"
        f"        version: {_value(entry['version'])}\n"
    )


def _new_importer(name: str, entries: dict[str, list[dict[str, str]]]) -> str:
    output = [f"  {_importer_name(name)}:\n"]
    for section in SECTIONS:
        if entries.get(section):
            output.append(f"    {section}:\n")
            output.extend(_entry(entry) for entry in entries[section])
    return "".join(output)


def _key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) else "'" + value.replace("'", "''") + "'"


def _importer_name(value: str) -> str:
    return value if value == "." or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) else _value(value)


def _value(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9@._~^*+<>=|:/()\-]+", value) else "'" + value.replace("'", "''") + "'"


def _insert(lock: dict[str, Any], inserts: dict[int, list[str]]) -> str:
    output: list[str] = []
    for index, line in enumerate(lock["lines"]):
        output.extend(inserts.get(index, []))
        output.append(line)
    output.extend(inserts.get(len(lock["lines"]), []))
    return "".join(output)


def _write_lockfile(root: Path, path: Path, before: bytes, output: bytes) -> None:
    checked = _path(root, path)
    assert checked == path
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkspaceImporterRepairError("pnpm lockfile cannot be opened safely for repair") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _read_open(descriptor, info.st_size) != before:
            raise WorkspaceImporterRepairError("pnpm lockfile changed before the repair could apply")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        written = 0
        while written < len(output):
            count = os.write(descriptor, output[written:])
            if count <= 0:
                raise OSError("short pnpm lockfile write")
            written += count
        os.fsync(descriptor)
    except OSError as error:
        raise WorkspaceImporterRepairError("pnpm lockfile cannot be written safely") from error
    finally:
        os.close(descriptor)


def _read_open(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while size:
        chunk = os.read(descriptor, min(size, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _sort_key(value: str) -> tuple[int, str]:
    return (0 if value == "." else 1, value)


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
