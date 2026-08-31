#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys


TUNNEL_LABEL = "com.lisihao.codex-workbench-tunnel"
HEARTBEAT_LABEL = "com.lisihao.codex-workbench-heartbeat"
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_TAILSCALE_NATIVE_SSH_PORT = 10022


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


def configured_ssh_hostname(destination: str) -> str:
    result = run("ssh", "-G", destination, check=False)
    if result.returncode != 0:
        return destination
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key == "hostname" and value.strip():
            return value.strip()
    return destination


def configured_ssh_proxycommand(destination: str) -> str | None:
    result = run("ssh", "-G", destination, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key == "proxycommand" and value.strip() not in {"", "none"}:
            return value.strip()
    return None


def tailscale_proxy_command(command: str | None, port: int) -> str | None:
    if not command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not any(Path(token).name == "tailscale" for token in tokens) or "nc" not in tokens:
        return None
    if "%p" not in tokens:
        return None
    return shlex.join(str(port) if token == "%p" else token for token in tokens)


def network_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def ssh_transport_arguments(
    destination: str,
    transport: str,
    native_ssh_port: int = DEFAULT_TAILSCALE_NATIVE_SSH_PORT,
) -> tuple[str, ...]:
    if transport == "system":
        return ()
    hostname = configured_ssh_hostname(destination)
    if transport == "auto":
        try:
            if ipaddress.ip_address(hostname) not in TAILSCALE_CGNAT:
                return ()
        except ValueError:
            return ()
        transport = "tailscale-native-ssh"
    configured_proxy = configured_ssh_proxycommand(destination)
    if transport == "tailscale-userspace" and configured_proxy:
        return ("-o", f"ProxyCommand={configured_proxy}")
    tailscale = shutil.which("tailscale")
    if not tailscale:
        raise SystemExit(
            "Tailscale userspace SSH transport was selected, but the tailscale CLI is unavailable"
        )
    if transport == "tailscale-userspace":
        return ("-o", f"ProxyCommand={tailscale} nc %h %p")
    native_proxy = tailscale_proxy_command(configured_proxy, native_ssh_port)
    if native_proxy is None:
        native_proxy = f"{tailscale} nc %h {native_ssh_port}"
    host_key_alias = "codex-workbench-" + "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in hostname
    )
    return (
        "-o",
        f"ProxyCommand={native_proxy}",
        "-o",
        f"HostKeyAlias={host_key_alias}",
        "-o",
        "StrictHostKeyChecking=accept-new",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--authority-ssh-alias",
        default="macmini",
        help="SSH config alias or user@host for the Mac mini authority (default: macmini)",
    )
    parser.add_argument(
        "--ssh-transport",
        choices=("auto", "system", "tailscale-native-ssh", "tailscale-userspace"),
        default="auto",
        help=(
            "SSH data path. auto uses tailnet-only Tailscale Serve plus the authority's "
            "native SSH key when the configured host is in 100.64.0.0/10; "
            "tailscale-userspace retains built-in Tailscale SSH as an explicit legacy option"
        ),
    )
    parser.add_argument(
        "--tailscale-native-ssh-port",
        type=network_port,
        default=DEFAULT_TAILSCALE_NATIVE_SSH_PORT,
        help="tailnet-only TCP Serve port forwarding to authority sshd (default: 10022)",
    )
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    authority_ssh_alias = args.authority_ssh_alias.strip()
    if not authority_ssh_alias or authority_ssh_alias.startswith("-") or any(
        character.isspace() for character in authority_ssh_alias
    ):
        raise SystemExit("--authority-ssh-alias must be one non-option SSH destination")
    transport_arguments = ssh_transport_arguments(
        authority_ssh_alias,
        args.ssh_transport,
        args.tailscale_native_ssh_port,
    )
    run(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        *transport_arguments, authority_ssh_alias, "true",
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
    for label in (TUNNEL_LABEL, HEARTBEAT_LABEL):
        plist_path = launch_agents / f"{label}.plist"
        template = (source / "launchd" / f"{label}.plist.in").read_text()
        rendered = (
            template.replace("__LOG_ROOT__", str(log_root))
            .replace("__CLIENT_ID__", client_id)
            .replace("__AUTHORITY_SSH_ALIAS__", authority_ssh_alias)
        )
        payload = plistlib.loads(rendered.encode())
        program_arguments = payload["ProgramArguments"]
        authority_index = program_arguments.index(authority_ssh_alias)
        program_arguments[authority_index:authority_index] = list(transport_arguments)
        plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
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
        *transport_arguments,
        authority_ssh_alias,
        remote_command,
    )
    print("Codex Workbench cockpit: http://127.0.0.1:18766")
    print("Codex native entry: MCP server 'codex-workbench'")
    print(f"Authority SSH destination: {authority_ssh_alias}")
    transport_label = args.ssh_transport
    if transport_label == "auto":
        transport_label = "tailscale-native-ssh" if transport_arguments else "system"
    print(f"SSH transport: {transport_label}")
    print(f"MacBook acceptance heartbeat: {client_id} every 5 minutes over the cockpit tunnel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
