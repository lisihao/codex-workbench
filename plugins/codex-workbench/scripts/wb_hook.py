from __future__ import annotations

import ast
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from typing import Optional, Tuple


ACTIVATION_MARKER = "WB_ACTIVATE_V1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 128 * 1024 * 1024
SECRET_NAME = re.compile(
    r"(^|/)(\.env(?:\..*)?|.*(?:credential|secret|private[-_]?key).*)$",
    re.IGNORECASE,
)
ABSOLUTE_PATH = re.compile(r"/(?:Users|Volumes|private|tmp)/[^\s\"'<>]+")


def _json_output(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _redact(text: str) -> str:
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return re.sub(
        r"\b(?:sk|xox[baprs]|gh[pousr])[-_][A-Za-z0-9_-]{12,}\b",
        "[REDACTED_TOKEN]",
        text,
    )


def _message_text(payload: dict) -> str:
    values = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            value = item.get("text")
            if isinstance(value, str):
                values.append(value)
    return "\n".join(values)


def _normalize_transcript(path: Optional[str], prompt: str) -> Tuple[bytes, list[str]]:
    records = []
    path_candidates = []
    if path and Path(path).is_file():
        with Path(path).open(errors="replace") as transcript:
            for line in transcript:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = item.get("type")
                payload = item.get("payload") or {}
                if kind == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role not in {"user", "assistant"}:
                        continue
                    text = _redact(_message_text(payload))
                    if not text:
                        continue
                    records.append(
                        {
                            "timestamp": item.get("timestamp"),
                            "kind": "message",
                            "role": role,
                            "phase": payload.get("phase"),
                            "text": text,
                        }
                    )
                    path_candidates.extend(ABSOLUTE_PATH.findall(text))
                elif kind == "response_item" and payload.get("type") in {
                    "function_call",
                    "custom_tool_call",
                }:
                    records.append(
                        {
                            "timestamp": item.get("timestamp"),
                            "kind": "tool_call",
                            "name": payload.get("name") or payload.get("type"),
                        }
                    )
                elif kind == "event_msg" and payload.get("type") in {
                    "task_started",
                    "task_complete",
                    "turn_aborted",
                }:
                    records.append(
                        {
                            "timestamp": item.get("timestamp"),
                            "kind": "turn_status",
                            "status": payload.get("type"),
                        }
                    )
    current = _redact(prompt)
    records.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "message",
            "role": "user",
            "text": current,
            "current_prompt": True,
        }
    )
    path_candidates.extend(ABSOLUTE_PATH.findall(current))
    output = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    return output.encode(), path_candidates


def _git(repository: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=not binary,
        timeout=60,
        check=False,
    )
    if result.returncode:
        message = result.stderr if not binary else result.stderr.decode(errors="replace")
        raise RuntimeError(message.strip() or "git command failed")
    return result.stdout


def _repository_context(cwd: Path):
    root = Path(_git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    branch = _git(root, "branch", "--show-current").strip() or None
    origin_result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    origin = origin_result.stdout.strip() if origin_result.returncode == 0 else None
    patch = _git(root, "diff", "--binary", "HEAD", binary=True)
    changed = set(_git(root, "diff", "--name-only", "HEAD").splitlines())
    untracked_raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    untracked = {
        value.decode(errors="surrogateescape")
        for value in untracked_raw.split(b"\0")
        if value
    }
    return root, head, branch, origin, patch, changed, untracked


def _allowed_file(path: Path) -> bool:
    try:
        relative = str(path)
        if SECRET_NAME.search(relative):
            return False
        size = path.stat().st_size
    except OSError:
        return False
    return path.is_file() and 0 <= size <= MAX_FILE_BYTES


def _tar_bytes(members: list[Tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in sorted(members):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _bundle(event: dict) -> Tuple[bytes, dict, bool]:
    cwd = Path(event["cwd"]).expanduser().resolve()
    transcript, mentioned_paths = _normalize_transcript(
        event.get("transcript_path"), event.get("prompt") or ""
    )
    root, head, branch, origin, patch, changed, untracked = _repository_context(cwd)
    file_entries = []
    members = [("transcript.jsonl", transcript)]
    if patch:
        members.append(("git.patch", patch))
    total = 0
    candidates = []
    for logical in sorted(untracked):
        candidates.append((root / logical, logical, "repository"))
    for raw in mentioned_paths:
        cleaned = raw.rstrip(".,;:!?)]}，。；：！？）】")
        path = Path(cleaned).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        try:
            logical = resolved.relative_to(root).as_posix()
            if logical in untracked or logical in changed:
                continue
            kind = "repository"
        except ValueError:
            logical = str(resolved)
            kind = "attachment"
        candidates.append((resolved, logical, kind))
    seen = set()
    for index, (path, logical, kind) in enumerate(candidates, 1):
        key = str(path)
        if key in seen or not _allowed_file(path):
            continue
        seen.add(key)
        data = path.read_bytes()
        if total + len(data) > MAX_TOTAL_FILE_BYTES:
            break
        total += len(data)
        archive_path = "files/{:04d}-{}".format(index, sha256(key.encode()).hexdigest()[:12])
        members.append((archive_path, data))
        file_entries.append(
            {
                "archive_path": archive_path,
                "logical_path": logical,
                "kind": kind,
                "sha256": sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    imported_repository_files = {
        entry["logical_path"]
        for entry in file_entries
        if entry["kind"] == "repository"
    }
    scopes = sorted(changed | imported_repository_files)
    if not scopes:
        scopes = ["."]
    prior_user_messages = sum(
        1
        for line in transcript.decode(errors="replace").splitlines()
        if '"role": "user"' in line and '"current_prompt": true' not in line
    )
    manifest = {
        "schema_version": 1,
        "source_thread_id": event["session_id"],
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_host": os.uname().nodename,
        "cwd": str(cwd),
        "repository": {
            "root": str(root),
            "name": root.name,
            "origin": origin,
            "branch": branch,
            "head": head,
            "dirty": bool(patch or untracked),
        },
        "suggested_scopes": scopes,
        "files": file_entries,
        "transcript": {"format": "normalized-jsonl", "raw_tool_outputs": False},
        "redaction_applied": True,
    }
    members.append(
        (
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode() + b"\n",
        )
    )
    return _tar_bytes(members), manifest, prior_user_messages > 0


def _mcp_ssh_command(command_id: str) -> list[str]:
    config = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
    lines = config.read_text().splitlines()
    section = "[mcp_servers.codex-workbench]"
    try:
        start = lines.index(section) + 1
    except ValueError as error:
        raise RuntimeError("codex-workbench MCP is not configured") from error
    values = {}
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if "=" not in stripped or stripped.startswith("#"):
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() in {"command", "args"}:
            values[key.strip()] = ast.literal_eval(raw.strip())
    command = values.get("command")
    arguments = values.get("args")
    if not isinstance(command, str) or not isinstance(arguments, list) or not arguments:
        raise RuntimeError("codex-workbench MCP transport is incomplete")
    remote = arguments[-1]
    if not isinstance(remote, str) or not remote.endswith(" mcp"):
        raise RuntimeError("codex-workbench MCP remote command is unsupported")
    arguments[-1] = (
        remote[:-4]
        + " context import --archive - --command-id "
        + shlex.quote(command_id)
    )
    return [command] + arguments


def _send(bundle: bytes, command_id: str) -> dict:
    result = subprocess.run(
        _mcp_ssh_command(command_id),
        input=bundle,
        capture_output=True,
        timeout=75,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(message[-2000:] or "Workbench transport exited {}".format(result.returncode))
    try:
        return json.loads(result.stdout.decode())
    except json.JSONDecodeError as error:
        raise RuntimeError("Workbench returned an invalid context receipt") from error


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if event.get("hook_event_name") != "UserPromptSubmit":
        return 0
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0
    data_root = Path(
        os.environ.get("PLUGIN_DATA", "~/.codex/plugin-data/codex-workbench")
    ).expanduser()
    binding_file = data_root / "bindings" / "{}.json".format(session_id)
    prompt = event.get("prompt") or ""
    normalized_prompt = prompt.strip().lower()
    activating = (
        ACTIVATION_MARKER in prompt
        or normalized_prompt == "wb"
        or normalized_prompt.startswith("wb ")
        or normalized_prompt.startswith("$wb")
        or normalized_prompt.startswith("/wb")
        or normalized_prompt.startswith("启动工作台")
    )
    if not activating and not binding_file.is_file():
        return 0
    if not activating and binding_file.is_file():
        try:
            existing = json.loads(binding_file.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("state") == "active":
            _json_output(
                "WB_SYNC_RECEIPT "
                + json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
                + "\nThis session is already bound. Reuse the durable receipt and route executable work through the Workbench MCP tools; do not create a duplicate task for ordinary conversation."
            )
            return 0
    try:
        bundle, manifest, existing_conversation = _bundle(event)
        digest = sha256(bundle).hexdigest()
        command_id = "wb-{}-{}".format(session_id, digest[:20])
        receipt = _send(bundle, command_id)
        state = {
            "state": "active",
            "source_thread_id": session_id,
            "context_ref": receipt["context_ref"],
            "repository": receipt["repository"],
            "base_sha": receipt["base_sha"],
            "existing_conversation": existing_conversation,
            "active_task_id": receipt.get("active_task_id"),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(binding_file, state)
        _json_output(
            "WB_SYNC_RECEIPT "
            + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            + "\nThe user activated Workbench. The authority accepted and durably bound this context. Route executable work through the Workbench MCP tools and continue the latest unfinished request when one exists."
        )
    except Exception as error:
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            outbox = data_root / "outbox"
            outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
            bundle_path = outbox / "{}.tar.gz".format(session_id)
            if "bundle" in locals():
                bundle_path.write_bytes(bundle)
                os.chmod(bundle_path, 0o600)
        except OSError:
            pass
        state = {
            "state": "degraded",
            "source_thread_id": session_id,
            "execution_host": "macbook-local",
            "error": str(error)[-1200:],
            "retry": "next prompt",
        }
        _atomic_json(binding_file, state)
        _json_output(
            "WB_SYNC_RECEIPT "
            + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            + "\nWorkbench takeover is not active. Use the current MacBook checkout as the explicit fallback and do not claim remote execution."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
