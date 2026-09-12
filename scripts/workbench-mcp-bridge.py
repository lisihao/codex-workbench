#!/usr/bin/env python3
"""Supervise one reconnectable stdio MCP child without replaying writes.

The bridge is deliberately a local transport owner.  It never starts the
Authority itself: the configured command is normally an SSH stdio command and
is the only child process this program may create or terminate.
"""

from __future__ import annotations

import argparse
import copy
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, TextIO


SCHEMA_VERSION = 1
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.2
DEFAULT_MAX_BACKOFF_SECONDS = 1.0
MAX_COMMAND_ARGUMENTS = 64
MAX_COMMAND_ARGUMENT_CHARS = 4096
MAX_REQUEST_ID_CHARS = 200
MAX_STORED_EVENT_DIGESTS = 512
MAX_STDERR_TAIL_LINES = 16
MAX_STDERR_LINE_BYTES = 4096
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
_RESPONSE_TOO_LARGE = "response_too_large"


class ConfigError(ValueError):
    """Raised when the bridge configuration cannot be used safely."""


class StateError(RuntimeError):
    """Raised when the private state file cannot be written safely."""


class TransportError(RuntimeError):
    """A finite child-process transport failure with no untrusted detail."""

    def __init__(self, kind: str, *, exit_code: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.exit_code = exit_code


@dataclass(frozen=True)
class BridgeConfig:
    """Validated local bridge configuration.

    ``command`` is executed directly as an argv list; a shell is never used.
    """

    command: tuple[str, ...]
    state_file: Path
    connect_timeout_seconds: float
    request_timeout_seconds: float
    max_reconnect_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _number(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ConfigError(f"{field} must be between {minimum:g} and {maximum:g}")
    return normalized


def _optional_number(
    raw: Mapping[str, object],
    field: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    return default if field not in raw else _number(
        raw[field], field, minimum=minimum, maximum=maximum
    )


def validate_config(raw: object) -> BridgeConfig:
    """Validate the fixed bridge config schema without resolving user homes."""

    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")
    command_raw = raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ConfigError("command must be a non-empty argv list")
    if len(command_raw) > MAX_COMMAND_ARGUMENTS:
        raise ConfigError("command contains too many arguments")
    command: list[str] = []
    for index, value in enumerate(command_raw):
        argument = _required_string(value, f"command[{index}]")
        if len(argument) > MAX_COMMAND_ARGUMENT_CHARS:
            raise ConfigError(f"command[{index}] is too long")
        command.append(argument)

    state_file = Path(_required_string(raw.get("state_file"), "state_file"))
    connect_timeout = _optional_number(
        raw,
        "connect_timeout_seconds",
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        minimum=0.05,
        maximum=60.0,
    )
    request_timeout = _optional_number(
        raw,
        "request_timeout_seconds",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        minimum=0.05,
        maximum=300.0,
    )
    attempts_raw = raw.get("max_reconnect_attempts", DEFAULT_MAX_RECONNECT_ATTEMPTS)
    if isinstance(attempts_raw, bool) or not isinstance(attempts_raw, int):
        raise ConfigError("max_reconnect_attempts must be an integer")
    if not 1 <= attempts_raw <= 5:
        raise ConfigError("max_reconnect_attempts must be between 1 and 5")
    initial_backoff = _optional_number(
        raw,
        "initial_backoff_seconds",
        DEFAULT_INITIAL_BACKOFF_SECONDS,
        minimum=0.001,
        maximum=10.0,
    )
    max_backoff = _optional_number(
        raw,
        "max_backoff_seconds",
        DEFAULT_MAX_BACKOFF_SECONDS,
        minimum=0.001,
        maximum=60.0,
    )
    if max_backoff < initial_backoff:
        raise ConfigError("max_backoff_seconds must be at least initial_backoff_seconds")
    return BridgeConfig(
        command=tuple(command),
        state_file=state_file,
        connect_timeout_seconds=connect_timeout,
        request_timeout_seconds=request_timeout,
        max_reconnect_attempts=attempts_raw,
        initial_backoff_seconds=initial_backoff,
        max_backoff_seconds=max_backoff,
    )


def load_config(path: str | os.PathLike[str]) -> BridgeConfig:
    """Load one private JSON configuration file."""

    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            return validate_config(json.load(stream))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError("cannot read valid bridge configuration") from error


def _digest(value: object) -> str:
    """Return a stable digest without retaining the source value in state."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_key(request_id: str) -> str:
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()


class PrivateState:
    """Persist only replay-safety metadata, never MCP arguments or responses."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "connection": {"state": "idle"},
            "server": {},
            "writes": {},
            "event_cursors": {},
            "event_dedup": {},
            "initialize_recorded": False,
        }

    def _load(self) -> dict[str, object]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except FileNotFoundError:
            if self.path.is_symlink():
                raise StateError("cannot read existing private bridge state")
            return self._empty()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StateError("cannot read existing private bridge state") from error
        if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
            raise StateError("existing private bridge state has an unsupported schema")
        required_mappings = ("connection", "server", "writes", "event_cursors", "event_dedup")
        if any(not isinstance(raw.get(field), Mapping) for field in required_mappings):
            raise StateError("existing private bridge state has invalid records")
        if type(raw.get("initialize_recorded")) is not bool:
            raise StateError("existing private bridge state has invalid records")
        writes = raw["writes"]
        cursors = raw["event_cursors"]
        dedup = raw["event_dedup"]
        if not isinstance(writes, Mapping) or not isinstance(cursors, Mapping) or not isinstance(dedup, Mapping):
            raise StateError("existing private bridge state has invalid records")
        if any(
            not isinstance(key, str)
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or not isinstance(value, Mapping)
            or value.get("state") != "sent"
            for key, value in writes.items()
        ):
            raise StateError("existing private bridge state has invalid write records")
        if any(
            not isinstance(key, str)
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or type(value) is not int
            or value < 0
            for key, value in cursors.items()
        ):
            raise StateError("existing private bridge state has invalid cursor records")
        if any(
            not isinstance(key, str)
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
            for key, value in dedup.items()
        ):
            raise StateError("existing private bridge state has invalid event records")
        data = dict(raw)
        for field in required_mappings:
            value = raw[field]
            assert isinstance(value, Mapping)
            data[field] = dict(value)
        return data

    def _write(self) -> None:
        parent = self.path.parent
        temporary: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=str(parent)
            )
            temporary = Path(temporary_name)
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as error:
            raise StateError("cannot persist private bridge state") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def record_initialize(self) -> None:
        self.data["initialize_recorded"] = True
        self._write()

    def connection(
        self,
        state: str,
        *,
        layer: str | None = None,
        kind: str | None = None,
        exit_code: int | None = None,
        stderr_tail: list[dict[str, int | str]] | None = None,
    ) -> None:
        previous = self.data.get("connection")
        payload: dict[str, object] = {"state": state}
        if layer is not None:
            payload["error"] = {
                "layer": layer,
                "kind": kind or "unknown",
                "exit_code": exit_code,
                # The tail is represented by bounded fingerprints rather than
                # source text, so prompts, tokens, paths, and account names do
                # not become local diagnostics.
                "stderr_tail": stderr_tail or [],
            }
        if state == "circuit_open" and isinstance(previous, Mapping):
            previous_error = previous.get("error")
            if isinstance(previous_error, Mapping):
                # Preserve the child-layer exit detail while recording that the
                # bridge—not the Authority—has stopped automatic retries.
                payload["last_error"] = dict(previous_error)
        self.data["connection"] = payload
        self._write()

    def server(self) -> tuple[str | None, str | None]:
        raw = self.data.get("server")
        if not isinstance(raw, Mapping):
            return None, None
        version = raw.get("version")
        digest = raw.get("canonical_tools_sha256")
        return (
            version if isinstance(version, str) else None,
            digest if isinstance(digest, str) else None,
        )

    def record_server(self, version: str | None, tools_digest: str) -> None:
        self.data["server"] = {
            "version": version,
            "canonical_tools_sha256": tools_digest,
        }
        self._write()

    def has_write(self, request_id: str) -> bool:
        writes = self.data.get("writes")
        return isinstance(writes, Mapping) and _request_key(request_id) in writes

    def mark_write_sent(self, request_id: str) -> None:
        writes_raw = self.data.get("writes")
        writes = dict(writes_raw) if isinstance(writes_raw, Mapping) else {}
        key = _request_key(request_id)
        writes.pop(key, None)
        writes[key] = {"state": "sent"}
        # A write identity is a safety record, not a cache: evicting it could
        # make an old host retry re-send an already executed operation.
        self.data["writes"] = writes
        self._write()

    @staticmethod
    def _task_key(task_id: object) -> str:
        return _digest(task_id if isinstance(task_id, str) else "__all_tasks__")

    def cursor_for(self, task_id: object) -> int | None:
        cursors = self.data.get("event_cursors")
        value = cursors.get(self._task_key(task_id)) if isinstance(cursors, Mapping) else None
        return value if type(value) is int and value >= 0 else None

    def record_events(
        self,
        task_id: object,
        cursor: object,
        events: list[object] | None,
        *,
        deduplicate: bool,
    ) -> list[object] | None:
        cursors_raw = self.data.get("event_cursors")
        cursors = dict(cursors_raw) if isinstance(cursors_raw, Mapping) else {}
        dedup_raw = self.data.get("event_dedup")
        dedup = dict(dedup_raw) if isinstance(dedup_raw, Mapping) else {}

        def record_cursor(event_task_id: object, candidate: object) -> None:
            if type(candidate) is not int or candidate < 0:
                return
            task_key = self._task_key(event_task_id)
            prior = cursors.get(task_key)
            cursors[task_key] = max(prior, candidate) if type(prior) is int else candidate

        def event_task_id(event: object) -> object:
            if isinstance(task_id, str):
                return task_id
            if isinstance(event, Mapping) and isinstance(event.get("task_id"), str):
                return event["task_id"]
            return "__all_tasks__"

        record_cursor(task_id, cursor)
        known_by_task: dict[str, tuple[list[str], set[str]]] = {}

        def known_for(event_task_id: object) -> tuple[list[str], set[str]]:
            task_key = self._task_key(event_task_id)
            existing = known_by_task.get(task_key)
            if existing is not None:
                return existing
            known_raw = dedup.get(task_key, [])
            known = [item for item in known_raw if isinstance(item, str)][-MAX_STORED_EVENT_DIGESTS:]
            existing = (known, set(known))
            known_by_task[task_key] = existing
            return existing

        def replay_key(event: object, authoritative_cursor: object) -> str | None:
            if type(authoritative_cursor) is int and authoritative_cursor >= 0:
                return f"cursor:{authoritative_cursor}"
            if not isinstance(event, Mapping):
                return None
            # The payload may be a large artifact or a model transcript.  For
            # legacy un-cursored events, use only stable metadata; if none is
            # available, preserve the event rather than risking a false match.
            identity = {
                key: event[key]
                for key in ("event_id", "id", "event_type", "task_id", "node_id", "created_at")
                if key in event
            }
            return f"metadata:{_digest(identity)}" if identity else None

        filtered: list[object] | None = None
        if events is not None:
            filtered = []
            for event in events:
                current_task_id = event_task_id(event)
                authoritative_cursor: object = None
                if isinstance(event, Mapping):
                    authoritative_cursor = event.get("cursor")
                    record_cursor(current_task_id, authoritative_cursor)
                known, known_set = known_for(current_task_id)
                # Event cursors are Authority-issued, globally ordered identity
                # values.  Prefer them to avoid retaining or hashing a large
                # event payload merely to recognize a reconnect replay.
                event_digest = replay_key(event, authoritative_cursor)
                duplicate = event_digest is not None and event_digest in known_set
                if not duplicate or not deduplicate:
                    filtered.append(event)
                if event_digest is not None and not duplicate:
                    known.append(event_digest)
                    known_set.add(event_digest)
        for task_key, (known, _) in known_by_task.items():
            dedup[task_key] = known[-MAX_STORED_EVENT_DIGESTS:]
        self.data["event_cursors"] = cursors
        self.data["event_dedup"] = dedup
        self._write()
        return filtered


class _StderrTail:
    """Bound stderr retention without retaining child text itself."""

    def __init__(self) -> None:
        self._items: deque[dict[str, int | str]] = deque(maxlen=MAX_STDERR_TAIL_LINES)
        self._lock = threading.Lock()

    def add(self, raw: bytes) -> None:
        fingerprint = hashlib.sha256(raw).hexdigest()[:16]
        with self._lock:
            self._items.append({"sha256_prefix": fingerprint, "bytes": len(raw)})

    def snapshot(self) -> list[dict[str, int | str]]:
        with self._lock:
            return list(self._items)


@dataclass(frozen=True)
class _ChildEvent:
    kind: str
    message: dict[str, object] | None = None


class ChildTransport:
    """One child JSONL transport with bounded queue readers and cleanup."""

    def __init__(self, command: tuple[str, ...]):
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=64 * 1024,
            )
        except OSError as error:
            raise TransportError("start_failed") from error
        self._events: queue.Queue[_ChildEvent] = queue.Queue(maxsize=64)
        self._closed = threading.Event()
        self._stderr_tail = _StderrTail()
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _put(self, event: _ChildEvent) -> None:
        while not self._closed.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    @staticmethod
    def _discard_to_newline(stream: Any) -> None:
        while True:
            extra = stream.readline(MAX_STDERR_LINE_BYTES + 1)
            if not extra or extra.endswith(b"\n"):
                return

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        stream = self.process.stdout
        while not self._closed.is_set():
            line = stream.readline(MAX_JSONL_LINE_BYTES + 1)
            if not line:
                self._put(_ChildEvent("eof"))
                return
            if len(line) > MAX_JSONL_LINE_BYTES:
                # The child is closed as soon as the bounded reader reports
                # this event.  Draining an arbitrarily long line would turn a
                # protocol error into an unbounded wait before the host gets
                # its explicit tool error.
                self._put(_ChildEvent(_RESPONSE_TOO_LARGE))
                return
            try:
                decoded = line.decode("utf-8")
                message = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._put(_ChildEvent("protocol"))
                continue
            if not isinstance(message, dict):
                self._put(_ChildEvent("protocol"))
                continue
            self._put(_ChildEvent("message", message))

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        stream = self.process.stderr
        while not self._closed.is_set():
            line = stream.readline(MAX_STDERR_LINE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_STDERR_LINE_BYTES:
                if not line.endswith(b"\n"):
                    self._discard_to_newline(stream)
                line = line[:MAX_STDERR_LINE_BYTES]
            self._stderr_tail.add(line)

    @property
    def exit_code(self) -> int | None:
        return self.process.poll()

    @property
    def stderr_tail(self) -> list[dict[str, int | str]]:
        return self._stderr_tail.snapshot()

    def settled_stderr_tail(self) -> list[dict[str, int | str]]:
        """Give the draining stderr reader a short bounded chance to finish."""

        self._stderr_thread.join(timeout=0.05)
        return self._stderr_tail.snapshot()

    def _eof_exit_code(self) -> int | None:
        """Reap a naturally exiting child briefly so its exit code is retained."""

        code = self.process.poll()
        if code is not None:
            return code
        try:
            return self.process.wait(timeout=0.05)
        except (OSError, subprocess.TimeoutExpired):
            return self.process.poll()

    def send(self, message: Mapping[str, object]) -> None:
        if self._closed.is_set():
            raise TransportError("closed", exit_code=self.exit_code)
        if self.process.poll() is not None:
            raise TransportError("child_exited", exit_code=self.exit_code)
        assert self.process.stdin is not None
        try:
            encoded = (
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise TransportError("write_failed", exit_code=self.exit_code) from error

    def wait_for(
        self,
        request_id: object,
        timeout_seconds: float,
        notification: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        """Wait for one response while forwarding valid child notifications."""

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError("request_timeout", exit_code=self.exit_code)
            try:
                event = self._events.get(timeout=remaining)
            except queue.Empty as error:
                raise TransportError("request_timeout", exit_code=self.exit_code) from error
            if event.kind == "eof":
                raise TransportError("eof", exit_code=self._eof_exit_code())
            if event.kind == _RESPONSE_TOO_LARGE:
                raise TransportError(_RESPONSE_TOO_LARGE, exit_code=self.exit_code)
            if event.kind != "message" or event.message is None:
                raise TransportError("protocol", exit_code=self.exit_code)
            message = event.message
            if "method" in message and "id" not in message:
                notification(message)
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            raise TransportError("unexpected_response", exit_code=self.exit_code)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        self._stdout_thread.join(timeout=0.5)
        self._stderr_thread.join(timeout=0.5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass


def _tool_error(request_id: object, code: str, *, request_token: str | None = None) -> dict[str, object]:
    """Return a safe MCP tool result without child diagnostics."""

    payload: dict[str, object] = {"error": code}
    if request_token is not None:
        payload["request_id"] = request_token
        payload["state"] = "indeterminate"
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": True,
        },
    }


def _rpc_error(request_id: object, code: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": code},
    }


class MCPConnectionBridge:
    """Bridge host JSONL to one recoverable MCP child process.

    Read-only requests can be retried after a bounded reconnect.  A write is
    journaled locally before send and is never sent a second time; any later
    answer is obtained exclusively through ``workbench_get_service_request``.
    """

    _INTRINSIC_READ_ONLY_TOOLS = {"workbench_get_service_request"}

    def __init__(
        self,
        config: BridgeConfig,
        output_stream: TextIO,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.output_stream = output_stream
        self.sleep = sleep
        self.state = PrivateState(config.state_file)
        self.transport: ChildTransport | None = None
        self.initialize_message: dict[str, object] | None = None
        self.initialized_message: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        self.host_initialized = False
        self.tool_read_only: dict[str, bool] = {
            name: True for name in self._INTRINSIC_READ_ONLY_TOOLS
        }
        self.server_version, self.tools_digest = self.state.server()
        self.catalog_refresh_required = False
        self.resume_events_after_reconnect = False
        self._notification_keys: set[str] = set()
        self._counter = 0

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def _emit(self, message: Mapping[str, object]) -> None:
        self.output_stream.write(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.output_stream.flush()

    def _diagnostic_once(self, key: str, state: str) -> None:
        # MCP servers should not emit asynchronous notifications before the
        # host has completed its initialize/initialized exchange.
        if not self.host_initialized:
            return
        if key in self._notification_keys:
            return
        self._notification_keys.add(key)
        self._emit(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "warning", "data": f"workbench bridge {state}"},
            }
        )

    def _tools_changed_once(self, key: str) -> None:
        if key in self._notification_keys:
            return
        self._notification_keys.add(key)
        self._emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def _next_internal_id(self, label: str) -> str:
        self._counter += 1
        return f"bridge-{label}-{self._counter}-{uuid.uuid4().hex}"

    @staticmethod
    def _server_version_from(response: Mapping[str, object]) -> str | None:
        result = response.get("result")
        if not isinstance(result, Mapping):
            return None
        server_info = result.get("serverInfo")
        if not isinstance(server_info, Mapping):
            return None
        version = server_info.get("version")
        return version if isinstance(version, str) else None

    @staticmethod
    def _tools_from(response: Mapping[str, object]) -> list[object]:
        result = response.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
            raise TransportError("invalid_tools_catalog")
        return list(result["tools"])

    @staticmethod
    def _catalog_read_only(tools: list[object]) -> dict[str, bool]:
        values = {name: True for name in MCPConnectionBridge._INTRINSIC_READ_ONLY_TOOLS}
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            name = tool.get("name")
            annotations = tool.get("annotations")
            if isinstance(name, str):
                values[name] = (
                    isinstance(annotations, Mapping)
                    and annotations.get("readOnlyHint") is True
                )
        return values

    def _observe_initialize(self, response: Mapping[str, object]) -> None:
        if "error" in response or not isinstance(response.get("result"), Mapping):
            raise TransportError("initialize_rejected")
        self.server_version = self._server_version_from(response)

    @staticmethod
    def _initialize_for_host(response: dict[str, object]) -> dict[str, object]:
        """Advertise list-change support only where this supervisor supplies it."""

        result = response.get("result")
        if not isinstance(result, Mapping):
            return response
        adjusted = copy.deepcopy(response)
        adjusted_result = adjusted.get("result")
        assert isinstance(adjusted_result, dict)
        capabilities = adjusted_result.get("capabilities")
        adjusted_capabilities = dict(capabilities) if isinstance(capabilities, Mapping) else {}
        tools = adjusted_capabilities.get("tools")
        adjusted_tools = dict(tools) if isinstance(tools, Mapping) else {}
        adjusted_tools["listChanged"] = True
        adjusted_capabilities["tools"] = adjusted_tools
        adjusted_result["capabilities"] = adjusted_capabilities
        return adjusted

    def _observe_catalog(
        self,
        response: Mapping[str, object],
        *,
        reconnect: bool,
        host_requested: bool,
    ) -> None:
        if "error" in response:
            raise TransportError("tools_list_rejected")
        tools = self._tools_from(response)
        digest = _digest(tools)
        prior_version, prior_digest = self.state.server()
        version = self.server_version
        changed = (
            prior_digest is not None
            and (prior_digest != digest or prior_version != version)
        )
        self.tools_digest = digest
        self.tool_read_only = self._catalog_read_only(tools)
        self.state.record_server(version, digest)
        if reconnect and changed:
            self.catalog_refresh_required = True
            self._tools_changed_once(
                f"tools:{prior_version}:{prior_digest}:{version}:{digest}"
            )
        if host_requested:
            # The host's actual list request—not this bridge's internal
            # directory check—is the only confirmation that it has refreshed.
            self.catalog_refresh_required = False

    def _on_child_notification(self, message: dict[str, object]) -> None:
        if message.get("method") == "notifications/tools/list_changed":
            self.catalog_refresh_required = True
            self._tools_changed_once("tools:child-notification")
            return
        self._emit(message)

    def _start_transport(self) -> None:
        self.close()
        self.transport = ChildTransport(self.config.command)

    def _record_transport_failure(self, error: TransportError) -> None:
        transport = self.transport
        exit_code = error.exit_code
        tail: list[dict[str, int | str]] = []
        if transport is not None:
            if exit_code is None:
                exit_code = transport.exit_code
            tail = transport.settled_stderr_tail()
        self.state.connection(
            "disconnected",
            layer="child",
            kind=error.kind,
            exit_code=exit_code,
            stderr_tail=tail,
        )
        diagnostic_state = (
            _RESPONSE_TOO_LARGE
            if error.kind == _RESPONSE_TOO_LARGE
            else "connection unavailable"
        )
        self._diagnostic_once(
            f"disconnected:{error.kind}:{exit_code}", diagnostic_state
        )
        self.close()

    def _send_request(
        self,
        message: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        if self.transport is None:
            raise TransportError("not_connected")
        if "id" not in message:
            raise TransportError("invalid_request")
        self.transport.send(message)
        return self.transport.wait_for(
            message["id"], timeout_seconds, self._on_child_notification
        )

    def _send_notification(self, message: Mapping[str, object]) -> None:
        if self.transport is None:
            raise TransportError("not_connected")
        self.transport.send(message)

    def _backoff(self, attempt: int) -> None:
        delay = min(
            self.config.initial_backoff_seconds * (2**attempt),
            self.config.max_backoff_seconds,
        )
        self.sleep(delay)

    def _connect_for_initialize(self, message: dict[str, object]) -> dict[str, object] | None:
        self.initialize_message = copy.deepcopy(message)
        self.state.record_initialize()
        for attempt in range(self.config.max_reconnect_attempts):
            self._backoff(attempt)
            try:
                self._start_transport()
                response = self._send_request(message, self.config.connect_timeout_seconds)
                if "error" not in response:
                    self._observe_initialize(response)
                self.state.connection("connected")
                return self._initialize_for_host(response)
            except TransportError as error:
                self._record_transport_failure(error)
        self.state.connection("circuit_open", layer="bridge", kind="connect_exhausted")
        self._diagnostic_once("circuit:connect", "reconnect circuit open")
        return None

    def _recover_connection(self) -> bool:
        if self.initialize_message is None:
            return False
        for attempt in range(self.config.max_reconnect_attempts):
            self._backoff(attempt)
            try:
                self._start_transport()
                initialize = copy.deepcopy(self.initialize_message)
                initialize["id"] = self._next_internal_id("initialize")
                response = self._send_request(initialize, self.config.connect_timeout_seconds)
                self._observe_initialize(response)
                if self.host_initialized:
                    self._send_notification(self.initialized_message)
                    listing = {
                        "jsonrpc": "2.0",
                        "id": self._next_internal_id("tools-list"),
                        "method": "tools/list",
                        "params": {},
                    }
                    catalog = self._send_request(listing, self.config.connect_timeout_seconds)
                    self._observe_catalog(catalog, reconnect=True, host_requested=False)
                self.state.connection("connected")
                self.resume_events_after_reconnect = True
                return True
            except TransportError as error:
                self._record_transport_failure(error)
        self.state.connection("circuit_open", layer="bridge", kind="reconnect_exhausted")
        self._diagnostic_once("circuit:reconnect", "reconnect circuit open")
        return False

    def _forward_read_only(self, message: dict[str, object]) -> dict[str, object] | None:
        for retry in range(2):
            try:
                if self.transport is None and not self._recover_connection():
                    return None
                return self._send_request(message, self.config.request_timeout_seconds)
            except TransportError as error:
                self._record_transport_failure(error)
                if error.kind == _RESPONSE_TOO_LARGE:
                    if message.get("method") == "tools/call":
                        return _tool_error(message.get("id"), _RESPONSE_TOO_LARGE)
                    return _rpc_error(message.get("id"), _RESPONSE_TOO_LARGE)
                if retry == 0 and self._recover_connection():
                    continue
                return None
        return None

    @staticmethod
    def _tool_call_parts(message: Mapping[str, object]) -> tuple[str | None, Mapping[str, object]]:
        params = message.get("params")
        if not isinstance(params, Mapping):
            return None, {}
        name = params.get("name")
        arguments = params.get("arguments")
        return name if isinstance(name, str) else None, arguments if isinstance(arguments, Mapping) else {}

    def _is_read_only(self, tool_name: str | None) -> bool:
        return tool_name is not None and self.tool_read_only.get(tool_name, False)

    def _prepare_event_read(
        self,
        message: dict[str, object],
        tool_name: str | None,
        arguments: Mapping[str, object],
    ) -> tuple[dict[str, object], bool]:
        if tool_name != "workbench_read_events" or "after" in arguments:
            return message, False
        task_id = arguments.get("task_id")
        cursor = self.state.cursor_for(task_id)
        if not self.resume_events_after_reconnect or cursor is None:
            return message, False
        outgoing = copy.deepcopy(message)
        params = outgoing.get("params")
        if not isinstance(params, dict):
            return message, False
        outgoing_arguments = dict(arguments)
        outgoing_arguments["after"] = cursor
        params["arguments"] = outgoing_arguments
        return outgoing, True

    def _record_event_response(
        self,
        response: dict[str, object],
        arguments: Mapping[str, object],
        *,
        resumed: bool,
    ) -> dict[str, object]:
        result = response.get("result")
        if not isinstance(result, Mapping):
            return response
        content = result.get("content")
        if not isinstance(content, list):
            return response
        for index, block in enumerate(content):
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events = payload.get("events")
                raw_events = list(events) if isinstance(events, list) else None
                cursor = payload.get("cursor")
                render = lambda filtered: json.dumps(
                    {**payload, "events": filtered}, ensure_ascii=False
                )
            elif isinstance(payload, list):
                # The established Workbench reply is a JSON list whose entries
                # each carry the Authority cursor.  Empty lists intentionally
                # provide no new cursor and therefore cannot advance replay.
                raw_events = list(payload)
                cursors = [
                    event.get("cursor")
                    for event in raw_events
                    if isinstance(event, Mapping) and type(event.get("cursor")) is int
                    and event["cursor"] >= 0
                ]
                cursor = max(cursors) if cursors else None
                render = lambda filtered: json.dumps(filtered, ensure_ascii=False)
            else:
                continue
            filtered = self.state.record_events(
                arguments.get("task_id"),
                cursor,
                raw_events,
                deduplicate=resumed,
            )
            if resumed and raw_events is not None and filtered is not None and filtered != raw_events:
                adjusted = copy.deepcopy(response)
                adjusted_result = adjusted.get("result")
                assert isinstance(adjusted_result, dict)
                adjusted_content = adjusted_result.get("content")
                assert isinstance(adjusted_content, list)
                adjusted_block = adjusted_content[index]
                assert isinstance(adjusted_block, dict)
                adjusted_block["text"] = render(filtered)
                return adjusted
            return response
        return response

    @staticmethod
    def _valid_request_id(value: object) -> str | None:
        if not isinstance(value, str) or not value or len(value) > MAX_REQUEST_ID_CHARS:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return None
        return value

    @staticmethod
    def _query_payload(response: Mapping[str, object]) -> dict[str, object] | None:
        result = response.get("result")
        if not isinstance(result, Mapping) or result.get("isError") is True:
            return None
        content = result.get("content")
        if not isinstance(content, list):
            return None
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _resolve_write(self, original_id: object, request_id: str) -> dict[str, object]:
        if self.transport is None and not self._recover_connection():
            return _tool_error(original_id, "write_indeterminate", request_token=request_id)
        query = {
            "jsonrpc": "2.0",
            "id": self._next_internal_id("service-request"),
            "method": "tools/call",
            "params": {
                "name": "workbench_get_service_request",
                "arguments": {"request_id": request_id},
            },
        }
        try:
            response = self._send_request(query, self.config.request_timeout_seconds)
        except TransportError as error:
            self._record_transport_failure(error)
            return _tool_error(original_id, "write_indeterminate", request_token=request_id)
        payload = self._query_payload(response)
        if (
            payload is not None
            and payload.get("request_id") == request_id
            and payload.get("state") == "completed"
            and isinstance(payload.get("result"), dict)
        ):
            return {"jsonrpc": "2.0", "id": original_id, "result": payload["result"]}
        return _tool_error(original_id, "write_indeterminate", request_token=request_id)

    def _handle_tool_call(self, message: dict[str, object]) -> dict[str, object]:
        request_id = message.get("id")
        name, arguments = self._tool_call_parts(message)
        operation_read = (
            name in self.tool_read_only
            and (
                (name == "workbench_handoff_lockfile" and arguments.get("op") in {"preview", "status"})
                or (name == "workbench_responsibility" and arguments.get("op") in {"list", "inspect"})
            )
        )
        if self._is_read_only(name) or operation_read:
            outgoing, resumed = self._prepare_event_read(message, name, arguments)
            response = self._forward_read_only(outgoing)
            if response is None:
                return _tool_error(request_id, "connection_unavailable")
            if name == "workbench_read_events":
                response = self._record_event_response(response, arguments, resumed=resumed)
                if resumed:
                    self.resume_events_after_reconnect = False
            return response

        identity_field = (
            "operation_id"
            if name == "workbench_handoff_lockfile" and arguments.get("op") in {"cancel", "reconcile"}
            else "request_id"
        )
        stable_request_id = self._valid_request_id(arguments.get(identity_field))
        if stable_request_id is None:
            return _tool_error(request_id, "stable_request_id_required")
        if self.state.has_write(stable_request_id):
            return self._resolve_write(request_id, stable_request_id)
        if self.catalog_refresh_required:
            return _tool_error(request_id, "tools_list_refresh_required")
        if self.transport is None and not self._recover_connection():
            return _tool_error(request_id, "connection_unavailable")
        if self.catalog_refresh_required:
            return _tool_error(request_id, "tools_list_refresh_required")
        # Persist before the first byte is sent.  A process failure after this
        # point is always resolved through the Authority journal, never replay.
        self.state.mark_write_sent(stable_request_id)
        try:
            return self._send_request(message, self.config.request_timeout_seconds)
        except TransportError as error:
            self._record_transport_failure(error)
            return self._resolve_write(request_id, stable_request_id)

    def handle(self, message: dict[str, object]) -> dict[str, object] | None:
        method = message.get("method")
        has_id = "id" in message
        if method == "initialize" and has_id:
            response = self._connect_for_initialize(message)
            return response if response is not None else _rpc_error(message.get("id"), "connection_unavailable")
        if not has_id:
            # A notification has no result channel for an indeterminate write.
            # Never forward a tool call without an id, even if a malformed host
            # attempts to use it as a fire-and-forget write.
            if method == "tools/call":
                self._diagnostic_once("invalid:tool-notification", "ignored invalid tool notification")
                return None
            if method == "notifications/initialized":
                self.host_initialized = True
                self.initialized_message = copy.deepcopy(message)
            if self.transport is not None:
                try:
                    self._send_notification(message)
                except TransportError as error:
                    self._record_transport_failure(error)
            return None
        if method == "tools/call":
            return self._handle_tool_call(message)
        if method in {"tools/list", "ping"}:
            response = self._forward_read_only(message)
            if response is None:
                return _rpc_error(message.get("id"), "connection_unavailable")
            if (
                isinstance(response.get("error"), Mapping)
                and response["error"].get("message") == _RESPONSE_TOO_LARGE
            ):
                return response
            if method == "tools/list":
                try:
                    self._observe_catalog(response, reconnect=False, host_requested=True)
                except TransportError:
                    return _rpc_error(message.get("id"), "invalid_tools_catalog")
            return response
        return _rpc_error(message.get("id"), "method_not_supported_by_bridge")

    def run(self, input_stream: TextIO) -> None:
        """Serve host JSONL until EOF, retaining a reportable circuit state."""

        try:
            for line in input_stream:
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    self._emit(_rpc_error(None, "parse_error"))
                    continue
                if not isinstance(raw, dict):
                    self._emit(_rpc_error(None, "invalid_request"))
                    continue
                try:
                    response = self.handle(raw)
                except StateError:
                    response = _tool_error(raw.get("id"), "private_state_unavailable") if raw.get("method") == "tools/call" else _rpc_error(raw.get("id"), "private_state_unavailable")
                if response is not None:
                    self._emit(response)
        finally:
            self.close()


def run_bridge(
    config: BridgeConfig,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    """Run the configured bridge; exported for deterministic local fixtures."""

    MCPConnectionBridge(config, output_stream).run(input_stream)


def main(argv: list[str] | None = None) -> int:
    """Run the JSONL bridge CLI."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="private bridge JSON configuration")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError:
        print("workbench-mcp-bridge: invalid configuration", file=sys.stderr)
        return 2
    try:
        run_bridge(config)
    except StateError:
        print("workbench-mcp-bridge: private state is unavailable", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
