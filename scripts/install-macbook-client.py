#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys


TUNNEL_LABEL = "com.lisihao.codex-workbench-tunnel"
LEGACY_HEARTBEAT_LABEL = "com.lisihao.codex-workbench-heartbeat"


def relaunch_with_supported_runtime() -> None:
    if sys.version_info >= (3, 11):
        return
    selector = Path(__file__).resolve().with_name("python-runtime")
    if not selector.is_file():
        raise SystemExit(
            "Codex Workbench requires Python 3.11 or newer; "
            f"runtime selector is missing: {selector}"
        )
    print(
        "Current Python is incompatible; relaunching with the Workbench Python runtime selector.",
        file=sys.stderr,
    )
    os.execv(str(selector), [str(selector), str(Path(__file__).resolve()), *sys.argv[1:]])


relaunch_with_supported_runtime()


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--authority-ssh-alias",
        default="macmini",
        help="SSH config alias or user@host for the Mac mini authority (default: macmini)",
    )
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    authority_ssh_alias = args.authority_ssh_alias.strip()
    if not authority_ssh_alias or authority_ssh_alias.startswith("-") or any(
        character.isspace() for character in authority_ssh_alias
    ):
        raise SystemExit("--authority-ssh-alias must be one non-option SSH destination")
    run(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        authority_ssh_alias, "true",
    )

    log_root = Path.home() / "Library" / "Logs" / "Codex Workbench"
    log_root.mkdir(parents=True, exist_ok=True)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{run('id', '-u').stdout.strip()}"
    client_id = "macbook-" + "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in socket.gethostname()
    )
    legacy_heartbeat_path = launch_agents / f"{LEGACY_HEARTBEAT_LABEL}.plist"
    run("launchctl", "bootout", domain, str(legacy_heartbeat_path), check=False)
    legacy_heartbeat_path.unlink(missing_ok=True)

    for label in (TUNNEL_LABEL,):
        plist_path = launch_agents / f"{label}.plist"
        template = (source / "launchd" / f"{label}.plist.in").read_text()
        rendered = (
            template.replace("__LOG_ROOT__", str(log_root))
            .replace("__CLIENT_ID__", client_id)
            .replace("__AUTHORITY_SSH_ALIAS__", authority_ssh_alias)
        )
        plistlib.loads(rendered.encode())
        plist_path.write_text(rendered)
        plist_path.chmod(0o600)
        run("launchctl", "bootout", domain, str(plist_path), check=False)
        run("launchctl", "bootstrap", domain, str(plist_path))
        run("launchctl", "enable", f"{domain}/{label}")
        run("launchctl", "kickstart", "-k", f"{domain}/{label}")
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Codex CLI is required to register the Workbench MCP entry")
    remote_command = 'exec "$HOME/Library/Application Support/Codex Workbench/app/bin/codex-workbench" mcp'
    run(codex, "mcp", "remove", "codex-workbench", check=False)
    run(
        codex,
        "mcp",
        "add",
        "codex-workbench",
        "--",
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        authority_ssh_alias,
        remote_command,
    )
    print("Codex Workbench cockpit: http://127.0.0.1:18766")
    print("Codex native entry: MCP server 'codex-workbench'")
    print(f"Authority SSH destination: {authority_ssh_alias}")
    print(f"MacBook acceptance heartbeat: {client_id} every 5 minutes over the cockpit tunnel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
