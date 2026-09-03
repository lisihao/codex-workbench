#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import shlex
import subprocess


def remote_path(value: str) -> str:
    value = value.rstrip("/")
    if value == "~":
        value = "$HOME"
    elif value.startswith("~/"):
        value = "$HOME/" + value[2:]
    executable = value + "/app/bin/codex-workbench"
    if executable.startswith("$HOME/"):
        return '"' + executable.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return shlex.quote(executable)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--location-proxy", required=True)
    parser.add_argument("--transport-config", required=True)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--authority-state-root", required=True)
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args(argv)

    proxy = Path(args.location_proxy).expanduser().absolute()
    config = Path(args.transport_config).expanduser().absolute()
    selection_result = subprocess.run(
        [str(proxy), "--config", str(config), "--select"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if selection_result.returncode:
        raise SystemExit(selection_result.stderr.strip() or "location selection failed")
    try:
        selection = json.loads(selection_result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"location selection returned invalid JSON: {error}") from error
    required = ("route", "reason", "observed_at")
    if not all(isinstance(selection.get(key), str) and selection[key] for key in required):
        raise SystemExit("location selection omitted heartbeat evidence")

    proxy_command = f"{shlex.quote(str(proxy))} --config {shlex.quote(str(config))}"
    remote_command = " ".join(
        (
            "exec",
            remote_path(args.authority_state_root),
            "client",
            "heartbeat",
            "--client-id",
            shlex.quote(args.client_id),
            "--kind",
            "macbook",
            "--route",
            shlex.quote(selection["route"]),
            "--reason",
            shlex.quote(selection["reason"]),
            "--observed-at",
            shlex.quote(selection["observed_at"]),
        )
    )
    command = [
        "/usr/bin/ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        f"ProxyCommand={proxy_command}",
        "-o",
        "HostKeyAlias=codex-workbench-authority",
        args.authority,
        remote_command,
    ]
    completed = subprocess.run(command, timeout=60, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
