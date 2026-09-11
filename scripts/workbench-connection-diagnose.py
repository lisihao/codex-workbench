#!/usr/bin/env python3
"""Read-only, bounded diagnostics for the installed Workbench connection.

The input is the private JSON written by the connection supervisor.  The
diagnostic never discovers a host, performs authentication, starts a service,
or invokes a model.  It may replace only the final, explicitly recognised
``mcp`` command with the corresponding ``service status`` command and reports
the result as four deliberately separate layers.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2
MAX_STDERR_SAMPLE = 240
LAYERS = (
    "configured_transport",
    "ssh_or_local_process",
    "authority_http",
    "mcp_child",
)
SAFE_STATE_SUMMARIES = {
    "bridge_state_connected",
    "bridge_state_disconnected",
}

_EMAIL_RE = re.compile(r"\b[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\b(?:https?|ssh|file)://[^\s\"']+")
_HOST_RE = re.compile(
    r"(?i)\b(?:macmini|localhost|[a-z0-9-]+\.(?:local|home|tailnet|invalid|internal|lan))\b"
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:\$HOME|~|/Users/[^\s\"']+|/home/[^\s\"']+|/private/var/[^\s\"']+|/var/folders/[^\s\"']+|/tmp/[^\s\"']+)"
)
_SECRET_RE = re.compile(
    r"(?i)(?:\bbearer\s+[^\s,;]+|\b(?:token|secret|password|api[_-]?key|authorization)\b"
    r"\s*[:=]?\s*(?:bearer\s+)?[^\s,;]+)"
)
_SESSION_RE = re.compile(
    r"(?i)\b(?:session|cookie|transcript|conversation|prompt|payload|request[_ -]?id|trace[_ -]?id)"
    r"(?:\s+(?:id|token|key))?(?:\s*[:=]\s*|\s+)[^\r\n]*"
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


class DiagnosticConfigError(ValueError):
    """Raised when the private supervisor configuration is not safe to use."""


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _safe_text(value: object, *, limit: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    if any(character in value for character in "\x00\r\n"):
        return None
    return value


def _safe_observed_text(value: object, *, limit: int = 120) -> str | None:
    """Keep short service metadata while applying output redaction."""

    text = _safe_text(value, limit=limit)
    return redact_sample(text) if text is not None else None


def redact_sample(value: object) -> str | None:
    """Return a short sample with credentials, paths, URLs, and identities removed."""

    if not isinstance(value, str) or not value:
        return None
    sample = value.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    sample = _SECRET_RE.sub("[REDACTED_SECRET]", sample)
    sample = _SESSION_RE.sub("[REDACTED_SESSION]", sample)
    sample = _URL_RE.sub("[REDACTED_URL]", sample)
    sample = _EMAIL_RE.sub("[REDACTED_ACCOUNT]", sample)
    sample = _PRIVATE_PATH_RE.sub("[REDACTED_PATH]", sample)
    sample = _HOST_RE.sub("[REDACTED_HOST]", sample)
    sample = _UUID_RE.sub("[REDACTED_ID]", sample)
    sample = " ".join(sample.split())
    return sample[:MAX_STDERR_SAMPLE] or None


def _normalise_timeout(value: object) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosticConfigError("invalid timeout")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= DEFAULT_TIMEOUT_SECONDS:
        raise DiagnosticConfigError("invalid timeout")
    return value


def load_config(path: Path) -> dict[str, object]:
    """Load and validate the supervisor's private connection configuration."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DiagnosticConfigError("config_unreadable") from error
    if not _is_mapping(raw) or raw.get("schema_version") != SCHEMA_VERSION:
        raise DiagnosticConfigError("unsupported_schema")
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(argument, str) or not argument for argument in command)
    ):
        raise DiagnosticConfigError("invalid_command")
    state_file = raw.get("state_file")
    if not isinstance(state_file, str) or not state_file.strip():
        raise DiagnosticConfigError("invalid_state_file")
    state_path = Path(state_file).expanduser()
    if not state_path.is_absolute():
        state_path = path.parent / state_path
    return {
        "schema_version": SCHEMA_VERSION,
        "command": list(command),
        "state_file": state_path,
        "diagnostic_timeout_seconds": _normalise_timeout(
            raw.get("diagnostic_timeout_seconds")
        ),
    }


def _ssh_command(command: Sequence[str]) -> bool:
    return bool(command) and Path(command[0]).name in {"ssh", "ssh.exe"}


def _remote_mcp_tokens(remote: str) -> tuple[str, str] | None:
    try:
        tokens = shlex.split(remote, posix=True)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[0] != "exec" or tokens[2] != "mcp":
        return None
    binary = _safe_text(tokens[1], limit=500)
    return (tokens[1], binary) if binary is not None else None


def _quote_remote_token(value: str) -> str:
    # Preserve the explicit environment expansion used by the installed
    # command while quoting all other binary paths as ordinary shell words.
    if value == "$HOME" or value.startswith("$HOME/"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return shlex.quote(value)


def replace_mcp_with_service_status(command: Sequence[str]) -> tuple[list[str], str]:
    """Replace only a recognised final ``mcp`` command.

    SSH registrations use one final shell argument of the exact form
    ``exec <binary> mcp``.  Local registrations use a final argv item equal to
    ``mcp``.  Everything else is rejected so this diagnostic cannot become a
    general remote-shell probe.
    """

    original = list(command)
    if not original:
        raise DiagnosticConfigError("invalid_command")
    if _ssh_command(original):
        remote = original[-1]
        parsed = _remote_mcp_tokens(remote)
        if parsed is None:
            raise DiagnosticConfigError("unrecognised_ssh_mcp_command")
        binary, _ = parsed
        replacement = f"exec {_quote_remote_token(binary)} service status"
        return [*original[:-1], replacement], "ssh"
    if original[-1] != "mcp" or len(original) < 2:
        raise DiagnosticConfigError("unrecognised_local_mcp_command")
    return [*original[:-1], "service", "status"], "local"


def _state_layers(state: Mapping[str, object] | None) -> dict[str, Mapping[str, object]]:
    if state is None:
        return {}
    candidates: list[object] = [
        state.get("layers"),
        state.get("components"),
        state.get("connection"),
        state,
    ]
    result: dict[str, Mapping[str, object]] = {}
    for candidate in candidates:
        if not _is_mapping(candidate):
            continue
        for layer in LAYERS:
            value = candidate.get(layer)
            if layer not in result and _is_mapping(value):
                result[layer] = value
    connection = state.get("connection")
    if _is_mapping(connection):
        connection_state = connection.get("state")
        if connection_state == "connected":
            result.setdefault(
                "ssh_or_local_process",
                {"status": "ready", "summary": "bridge_state_connected"},
            )
        elif connection_state in {"disconnected", "circuit_open"}:
            result.setdefault(
                "ssh_or_local_process",
                {"status": "error", "summary": "bridge_state_disconnected"},
            )
        error = connection.get("error")
        if _is_mapping(error) and error.get("layer") in {"child", "bridge"}:
            child: dict[str, object] = {}
            for key in ("exit_code", "child_exit_code"):
                value = error.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    child["exit_code"] = value
                    break
            stderr_tail = error.get("stderr_tail")
            if isinstance(stderr_tail, list):
                child["stderr_tail"] = list(stderr_tail)
            if child:
                result.setdefault("mcp_child", child)
    return result


def read_state(path: Path) -> tuple[Mapping[str, object] | None, str]:
    """Read only the structured state plane; return a stable read outcome."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "unreadable"
    if not _is_mapping(raw):
        return None, "invalid"
    return raw, "ok"


def _status_from_state(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {"status": "unknown", "summary": "state_unknown"}
    raw_status = value.get("status", value.get("state"))
    if raw_status in {"ready", "ok", "healthy"} or value.get("ok") is True:
        status = "ready"
    elif raw_status in {"error", "failed", "unhealthy"} or value.get("ok") is False:
        status = "error"
    elif raw_status in {"degraded", "warning"}:
        status = "degraded"
    else:
        status = "unknown"
    state_summary = value.get("summary")
    summary = (
        state_summary
        if isinstance(state_summary, str) and state_summary in SAFE_STATE_SUMMARIES
        else "state_ready"
        if status == "ready"
        else "state_error"
        if status == "error"
        else "state_unknown"
    )
    result: dict[str, object] = {"status": status, "summary": summary}
    for source, target in (
        ("version", "version"),
        ("build", "build"),
        ("service_instance", "service_instance"),
        ("http_status", "http_status"),
    ):
        raw = value.get(source)
        if source == "http_status":
            if isinstance(raw, int) and not isinstance(raw, bool):
                result[target] = raw
        else:
            text = _safe_observed_text(raw)
            if text is not None:
                result[target] = text
    return result


def _child_state(value: Mapping[str, object] | None) -> dict[str, object]:
    """Require explicit child exit evidence; a ready marker alone is unknown."""

    if value is None:
        return {"status": "unknown", "summary": "child_exit_stderr_unknown"}
    exit_code = value.get("exit_code", value.get("child_exit_code"))
    stderr_key = "stderr" if "stderr" in value else "child_stderr" if "child_stderr" in value else None
    stderr = value.get(stderr_key) if stderr_key is not None else None
    stderr_tail = value.get("stderr_tail")
    if stderr_key is not None:
        stderr_present = isinstance(stderr, str)
        sample = redact_sample(stderr)
    elif isinstance(stderr_tail, list):
        stderr_present = all(
            _is_mapping(item)
            and isinstance(item.get("sha256_prefix"), str)
            and isinstance(item.get("bytes"), int)
            and not isinstance(item.get("bytes"), bool)
            for item in stderr_tail
        )
        sample = f"redacted_stderr_tail_count:{len(stderr_tail)}" if stderr_present else None
    else:
        stderr_present = False
        sample = None
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not stderr_present
    ):
        result: dict[str, object] = {
            "status": "unknown",
            "summary": "child_exit_stderr_unknown",
        }
        if sample is not None:
            result["stderr_sample"] = sample
        return result
    result = {
        "status": "ready" if exit_code == 0 else "error",
        "summary": "child_exit_zero" if exit_code == 0 else "child_exit_nonzero",
        "exit_code": exit_code,
    }
    if sample is not None:
        result["stderr_sample"] = sample
    return result


def _service_status(stdout: str) -> dict[str, object] | None:
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(value) if _is_mapping(value) else None


def _service_components(report: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    process: dict[str, object] = {"status": "ready", "summary": "service_status_exit_zero"}
    http_status = report.get("http_status")
    valid_http_status = isinstance(http_status, int) and not isinstance(http_status, bool)
    healthy = report.get("ok") is True and valid_http_status and http_status == 200
    http: dict[str, object] = {
        "status": "ready" if healthy else "error" if report.get("ok") is False or valid_http_status else "unknown",
        "summary": "authority_http_ok" if healthy else "authority_http_unhealthy" if report.get("ok") is False or valid_http_status else "service_status_incomplete",
    }
    if valid_http_status:
        http["http_status"] = http_status
    for source, target in (("version", "version"), ("build", "build"), ("service_instance", "service_instance")):
        text = _safe_observed_text(report.get(source))
        if text is not None:
            http[target] = text
    return process, http


def _failure_key(components: Mapping[str, Mapping[str, object]], state_result: str) -> str:
    process = components["ssh_or_local_process"]
    http = components["authority_http"]
    child = components["mcp_child"]
    if process.get("summary") == "transport_timeout":
        return "transport-timeout"
    if process.get("summary") == "ssh_exit_255":
        return "ssh-exit-255"
    if process.get("summary") == "auth_or_hostkey_error":
        return "ssh-auth-hostkey-error"
    if process.get("summary") == "permission_error":
        return "ssh-permission-error"
    if http.get("status") == "error":
        return "authority-http-unhealthy"
    if child.get("status") == "error":
        return f"mcp-child-exit-{child.get('exit_code')}"
    if process.get("summary") == "bridge_state_disconnected":
        return "bridge-disconnected"
    if state_result in {"missing", "unreadable", "invalid"}:
        return f"state-{state_result}"
    if process.get("status") == "error":
        return "process-error"
    if child.get("status") == "unknown":
        return "mcp-child-unknown"
    if http.get("status") == "unknown":
        return "authority-http-unknown"
    if process.get("status") == "unknown":
        return "process-unknown"
    return "ok"


def _recovery_steps(fault_key: str) -> list[str]:
    if fault_key in {
        "transport-timeout",
        "ssh-exit-255",
        "ssh-auth-hostkey-error",
        "ssh-permission-error",
        "bridge-disconnected",
    }:
        return ["reconnect the existing MCP bridge", "check the existing SSH transport and host-key configuration"]
    if fault_key == "process-error":
        return ["reconnect the existing MCP bridge", "check the existing SSH transport"]
    if fault_key == "authority-http-unhealthy":
        return ["check the existing Authority service and its /health endpoint"]
    if fault_key == "authority-http-unknown":
        return ["check the existing Authority service and its /health endpoint"]
    if fault_key.startswith("mcp-child-"):
        return ["reconnect the existing MCP bridge", "inspect the existing MCP child exit and stderr evidence"]
    if fault_key in {"state-missing", "state-unreadable", "state-invalid"}:
        return ["check the existing bridge state file", "reconnect the existing MCP bridge"]
    return []


def diagnose(config_path: Path, *, status_only: bool = False) -> dict[str, object]:
    """Run a bounded diagnostic without restarting or discovering anything."""

    config = load_config(config_path)
    command = config["command"]
    assert isinstance(command, list)
    probe_command, transport_kind = replace_mcp_with_service_status(command)
    state_path = config["state_file"]
    assert isinstance(state_path, Path)
    state, state_result = read_state(state_path)
    layers = _state_layers(state)
    configured_state = _status_from_state(layers.get("configured_transport"))
    process_state = _status_from_state(layers.get("ssh_or_local_process"))
    http_state = _status_from_state(layers.get("authority_http"))
    components: dict[str, dict[str, object]] = {
        "configured_transport": {
            "status": "configured",
            "transport": transport_kind,
            "summary": "configured_existing_transport",
        },
        "ssh_or_local_process": {"status": "unknown", "summary": "probe_not_run"},
        "authority_http": {"status": "unknown", "summary": "probe_not_run"},
        "mcp_child": _child_state(layers.get("mcp_child")),
    }
    if configured_state["status"] != "unknown":
        components["configured_transport"]["state_status"] = configured_state["status"]
        components["configured_transport"]["state_summary"] = configured_state["summary"]
    attempts = 0
    if not status_only:
        timeout = config["diagnostic_timeout_seconds"]
        assert isinstance(timeout, float)
        last_failure: tuple[str, str | None, int | None] | None = None
        for attempt in range(MAX_ATTEMPTS):
            attempts += 1
            try:
                completed = subprocess.run(
                    probe_command,
                    text=True,
                    capture_output=True,
                    check=False,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                last_failure = ("transport_timeout", redact_sample(error.stderr), None)
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(min(0.25 * (2**attempt), 0.5))
                    continue
                break
            except PermissionError as error:
                last_failure = ("permission_error", redact_sample(str(error)), None)
                break
            except OSError as error:
                message = str(error)
                lowered = message.lower()
                summary = "auth_or_hostkey_error" if any(
                    marker in lowered for marker in ("host key", "hostkey", "permission denied", "authentication")
                ) else "process_spawn_error"
                last_failure = (summary, redact_sample(message), None)
                if summary in {"auth_or_hostkey_error", "permission_error"}:
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(min(0.25 * (2**attempt), 0.5))
                    continue
                break
            stderr_sample = redact_sample(completed.stderr)
            if completed.returncode == 0:
                report = _service_status(
                    completed.stdout if isinstance(completed.stdout, str) else ""
                )
                if report is None:
                    components["ssh_or_local_process"] = {"status": "ready", "summary": "service_status_exit_zero"}
                    components["authority_http"] = {"status": "unknown", "summary": "invalid_service_status_json"}
                else:
                    process, http = _service_components(report)
                    if stderr_sample is not None:
                        process["stderr_sample"] = stderr_sample
                    components["ssh_or_local_process"] = process
                    components["authority_http"] = http
                last_failure = None
                break
            summary = "ssh_exit_255" if completed.returncode == 255 else "probe_exit_nonzero"
            lowered_stderr = (
                completed.stderr.lower() if isinstance(completed.stderr, str) else ""
            )
            if any(marker in lowered_stderr for marker in ("host key", "hostkey", "permission denied", "authentication")):
                summary = "auth_or_hostkey_error"
            last_failure = (summary, stderr_sample, completed.returncode)
            if summary == "auth_or_hostkey_error" or attempt + 1 >= MAX_ATTEMPTS:
                break
            time.sleep(min(0.25 * (2**attempt), 0.5))
        if last_failure is not None:
            summary, sample, exit_code = last_failure
            components["ssh_or_local_process"] = {
                "status": "unknown" if summary == "transport_timeout" else "error",
                "summary": summary,
            }
            if exit_code is not None:
                components["ssh_or_local_process"]["exit_code"] = exit_code
            if sample is not None:
                components["ssh_or_local_process"]["stderr_sample"] = sample
            # An SSH/process failure is not evidence that the HTTP service crashed.
            components["authority_http"] = {"status": "unknown", "summary": "authority_http_unverified"}
    else:
        components["ssh_or_local_process"] = process_state
        components["authority_http"] = http_state
        if process_state["status"] == "unknown":
            components["ssh_or_local_process"] = {
                "status": "unknown",
                "summary": "status_only_state_unknown",
            }
        if http_state["status"] == "unknown":
            components["authority_http"] = {
                "status": "unknown",
                "summary": "status_only_state_unknown",
            }
    fault_key = _failure_key(components, state_result)
    all_ready = all(components[layer].get("status") == "ready" for layer in LAYERS[1:])
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all_ready,
        "fault_key": fault_key,
        "status_only": status_only,
        "probe": {
            "performed": not status_only,
            "attempts": attempts,
            "transport": transport_kind,
        },
        "state": {"status": state_result},
        "components": components,
        "recovery_steps": _recovery_steps(fault_key),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.config, status_only=args.status_only)
    except DiagnosticConfigError:
        result = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "fault_key": "invalid-config",
            "components": {layer: {"status": "unknown", "summary": "invalid_config"} for layer in LAYERS},
            "recovery_steps": ["check the existing diagnostic configuration"],
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
