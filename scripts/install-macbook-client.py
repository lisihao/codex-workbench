#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid


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


def absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving away symlink evidence."""

    path = path.expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    """Reject direct, broken, and ancestor symlinks before any write."""

    path = absolute_path(path)
    current = path
    while True:
        if current.is_symlink():
            raise SystemExit(f"{label} has a symlink ancestor: {current}")
        if current.parent == current:
            return
        current = current.parent


def assert_directory_target(path: Path, label: str) -> None:
    path = absolute_path(path)
    assert_no_symlink_ancestors(path, label=label)
    if path.exists() and not path.is_dir():
        raise SystemExit(f"{label} is not a directory: {path}")
    ancestor = path
    while not ancestor.exists():
        if ancestor.is_symlink():
            raise SystemExit(f"{label} has a symlink ancestor: {ancestor}")
        ancestor = ancestor.parent
    assert_no_symlink_ancestors(ancestor, label=label)
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} parent is not writable: {ancestor}")
    if path.exists() and not os.access(path, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} is not writable: {path}")


def assert_file_target(path: Path, label: str) -> None:
    path = absolute_path(path)
    assert_no_symlink_ancestors(path, label=label)
    if path.exists() and not path.is_file():
        raise SystemExit(f"{label} is not a file: {path}")
    assert_directory_target(path.parent, f"{label} parent")
    if path.exists() and not os.access(path, os.W_OK):
        raise SystemExit(f"{label} is not writable: {path}")


def remove_path(path: Path) -> None:
    """Remove only the named path; never follow a symlink during rollback."""

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        if not path.is_dir():
            raise SystemExit(f"rollback target is unsupported: {path}")
        shutil.rmtree(path)


class InstallTransaction:
    """Reversible local transaction for cockpit files and agent projections."""

    def __init__(self, parent: Path) -> None:
        parent = absolute_path(parent)
        assert_no_symlink_ancestors(parent, label="installer transaction parent")
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            raise SystemExit(f"installer transaction parent is not a directory: {parent}")
        self.root = Path(tempfile.mkdtemp(prefix=".codex-workbench-client-", dir=parent))
        self.entries: list[tuple[Path, Path, bool, str | None]] = []
        self.created_directories: list[Path] = []

    def snapshot(self, path: Path, label: str) -> None:
        path = absolute_path(path)
        assert_no_symlink_ancestors(path, label=label)
        existed = path.exists() or path.is_symlink()
        backup = self.root / f"entry-{len(self.entries)}"
        link_target: str | None = None
        if existed:
            if path.is_symlink():
                link_target = os.readlink(path)
            elif path.is_dir():
                shutil.copytree(path, backup, symlinks=True)
            elif path.is_file():
                shutil.copy2(path, backup)
            else:
                raise SystemExit(f"{label} is not a supported filesystem target: {path}")
        self.entries.append((path, backup, existed, link_target))

    def track_created_directory(self, path: Path) -> None:
        self.created_directories.append(absolute_path(path))

    def rollback(self) -> None:
        errors: list[str] = []
        for path, backup, existed, link_target in reversed(self.entries):
            try:
                assert_no_symlink_ancestors(path, label="rollback target")
                if path.exists() or path.is_symlink():
                    remove_path(path)
                if not existed:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if link_target is not None:
                    path.symlink_to(link_target)
                elif backup.is_dir():
                    shutil.copytree(backup, path, symlinks=True)
                else:
                    shutil.copy2(backup, path)
            except Exception as error:  # pragma: no cover - catastrophic filesystem fault
                errors.append(f"{path}: {error}")
        for path in sorted(self.created_directories, key=lambda value: len(value.parts), reverse=True):
            try:
                if not path.exists() or not path.is_dir():
                    continue
                for child in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                    if child.is_dir() and not child.is_symlink() and not any(child.iterdir()):
                        child.rmdir()
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass
        self.cleanup()
        if errors:
            raise SystemExit("installer rollback failed: " + "; ".join(errors))

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def commit(self) -> None:
        self.cleanup()


def install_code_as_harness(source: Path) -> None:
    installer = source / "scripts" / "install-code-as-harness.py"
    if not installer.is_file():
        raise SystemExit(f"Code-as-Harness installer is missing: {installer}")
    result = run(
        sys.executable,
        str(installer),
        "--source",
        str(source),
        "--adopt-compatible",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown installer failure"
        raise SystemExit(f"Code-as-Harness installation failed: {detail}")


def install_archify(source: Path) -> None:
    installer = source / "scripts" / "install-archify.py"
    if not installer.is_file():
        raise SystemExit(f"Archify installer is missing: {installer}")
    result = run(sys.executable, str(installer), "--source", str(source), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown installer failure"
        raise SystemExit(f"Archify installation failed: {detail}")


def preflight_managed_agent_skills(source: Path) -> None:
    """Preflight both global agent projections before either installer writes."""

    harness_installer = source / "scripts" / "install-code-as-harness.py"
    archify_installer = source / "scripts" / "install-archify.py"
    if not harness_installer.is_file():
        raise SystemExit(f"Code-as-Harness installer is missing: {harness_installer}")
    if not archify_installer.is_file():
        raise SystemExit(f"Archify installer is missing: {archify_installer}")
    checks = (
        (harness_installer, ("--check", "--adopt-compatible"), "Code-as-Harness"),
        (archify_installer, ("--dry-run",), "Archify"),
    )
    for installer, check_flags, label in checks:
        result = run(
            sys.executable,
            str(installer),
            "--source",
            str(source),
            *check_flags,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown preflight failure"
            raise SystemExit(f"{label} preflight failed: {detail}")


def render_client_plist(
    source: Path,
    log_root: Path,
    client_id: str,
    authority_ssh_alias: str,
    label: str,
    transport_arguments: tuple[str, ...],
    authority_mcp_path: str | None = None,
) -> dict[str, object]:
    template = (source / "launchd" / f"{label}.plist.in").read_text()
    rendered = (
        template.replace("__LOG_ROOT__", str(log_root))
        .replace("__CLIENT_ID__", client_id)
        .replace("__AUTHORITY_SSH_ALIAS__", authority_ssh_alias)
    )
    payload = plistlib.loads(rendered.encode())
    program_arguments = payload["ProgramArguments"]
    if not isinstance(program_arguments, list):
        raise SystemExit(f"LaunchAgent ProgramArguments is invalid: {label}")
    if authority_ssh_alias not in program_arguments:
        raise SystemExit(
            f"LaunchAgent template does not contain authority SSH destination: {label}"
        )
    authority_index = program_arguments.index(authority_ssh_alias)
    program_arguments[authority_index:authority_index] = list(transport_arguments)
    if authority_mcp_path is not None and label == HEARTBEAT_LABEL:
        heartbeat_command = (
            f"exec {remote_shell_quote(authority_mcp_path)} client heartbeat "
            f"--client-id {shlex.quote(client_id)} --kind macbook"
        )
        heartbeat_index = next(
            (
                index
                for index, argument in enumerate(program_arguments)
                if isinstance(argument, str) and " client heartbeat " in argument
            ),
            None,
        )
        if heartbeat_index is None:
            raise SystemExit(f"LaunchAgent template does not contain heartbeat command: {label}")
        program_arguments[heartbeat_index] = heartbeat_command
    return payload


def preflight_client_plists(
    source: Path,
    log_root: Path,
    client_id: str,
    authority_ssh_alias: str,
    transport_arguments: tuple[str, ...],
    authority_mcp_path: str | None = None,
) -> None:
    for label in (TUNNEL_LABEL, HEARTBEAT_LABEL):
        render_client_plist(
            source,
            log_root,
            client_id,
            authority_ssh_alias,
            label,
            transport_arguments,
            authority_mcp_path,
        )


def preflight_global_agent_targets(home: Path) -> None:
    """Check every global target before either managed Skill installer writes."""

    home = absolute_path(home)
    targets = (
        (home / ".codex" / "skills" / "code-as-harness" / "SKILL.md", "Codex Code-as-Harness skill"),
        (home / ".codex" / "AGENTS.md", "Codex policy"),
        (home / ".codex" / "skills" / "archify", "Codex Archify skill"),
        (home / ".claude" / "skills" / "code-as-harness" / "SKILL.md", "Claude Code-as-Harness skill"),
        (home / ".claude" / "CLAUDE.md", "Claude policy"),
        (home / ".claude" / "skills" / "archify", "Claude Archify skill"),
    )
    for path, label in targets:
        if path.name.endswith(".md"):
            assert_file_target(path, label)
        else:
            assert_directory_target(path, label)


def remote_state_root(value: str) -> str:
    """Validate and preserve the authority's remote state-root spelling."""

    value = value.strip()
    if not value or value.startswith("-") or any(character in value for character in "\r\n\x00"):
        raise SystemExit("--authority-state-root must be one non-empty remote path")
    if value == "~":
        return "$HOME"
    if value.startswith("~/"):
        return "$HOME/" + value[2:]
    if not value.startswith("/"):
        raise SystemExit("--authority-state-root must be absolute or start with ~/ ")
    return value.rstrip("/")


def remote_shell_quote(path: str) -> str:
    """Quote a remote path while retaining expansion for the explicit $HOME form."""

    if path == "$HOME" or path.startswith("$HOME/"):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return shlex.quote(path)


def authority_mcp_binary(state_root: str) -> str:
    return remote_state_root(state_root).rstrip("/") + "/app/bin/codex-workbench"


def preflight_remote_mcp(
    authority_ssh_alias: str,
    transport_arguments: tuple[str, ...],
    state_root: str,
) -> str:
    """Verify the exact remote MCP executable before local writes begin."""

    binary = authority_mcp_binary(state_root)
    run(
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        *transport_arguments,
        authority_ssh_alias,
        "test -x " + remote_shell_quote(binary),
    )
    return binary


def read_mcp_registration(codex: str, name: str) -> dict[str, object] | None:
    result = run(codex, "mcp", "get", name, "--json", check=False)
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Codex MCP registration preflight returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit("Codex MCP registration preflight did not return an object")
    return value


def mcp_add_command(codex: str, registration: dict[str, object]) -> list[str]:
    transport = registration.get("transport")
    if not isinstance(transport, dict):
        raise SystemExit("existing Codex MCP registration has no transport")
    transport_type = transport.get("type")
    command = [codex, "mcp", "add", "codex-workbench"]
    if transport_type == "stdio":
        raw_env = transport.get("env")
        if isinstance(raw_env, dict):
            for key, value in raw_env.items():
                if isinstance(key, str) and isinstance(value, str):
                    command.extend(("--env", f"{key}={value}"))
        raw_command = transport.get("command")
        raw_args = transport.get("args", [])
        if not isinstance(raw_command, str) or not isinstance(raw_args, list) or not all(
            isinstance(value, str) for value in raw_args
        ):
            raise SystemExit("existing Codex stdio MCP registration is malformed")
        return [*command, "--", raw_command, *raw_args]
    if transport_type in {"streamable_http", "sse"}:
        url = transport.get("url")
        if not isinstance(url, str):
            raise SystemExit("existing Codex HTTP MCP registration is malformed")
        return [*command, "--url", url]
    raise SystemExit(f"unsupported existing Codex MCP transport: {transport_type}")


def ssh_transport_arguments(
    destination: str,
    transport: str,
    native_ssh_port: int = DEFAULT_TAILSCALE_NATIVE_SSH_PORT,
    *,
    probe_config: bool = True,
) -> tuple[str, ...]:
    if transport == "system":
        return ()
    hostname = configured_ssh_hostname(destination) if probe_config else destination
    if transport == "auto":
        if not probe_config:
            # Dry-run is local-only; the real install performs auto detection
            # with the user's SSH configuration after all local preflight.
            return ()
        try:
            if ipaddress.ip_address(hostname) not in TAILSCALE_CGNAT:
                return ()
        except ValueError:
            return ()
        transport = "tailscale-native-ssh"
    configured_proxy = configured_ssh_proxycommand(destination) if probe_config else None
    if transport == "tailscale-userspace" and configured_proxy:
        return ("-o", f"ProxyCommand={configured_proxy}")
    if transport == "tailscale-userspace":
        tailscale = shutil.which("tailscale")
        if not tailscale:
            raise SystemExit(
                "Tailscale userspace SSH transport was selected, but the tailscale CLI is unavailable"
            )
        return ("-o", f"ProxyCommand={tailscale} nc %h %p")
    native_proxy = tailscale_proxy_command(configured_proxy, native_ssh_port)
    if native_proxy is None:
        tailscale = shutil.which("tailscale")
        if not tailscale:
            raise SystemExit(
                "Tailscale native SSH transport was selected, but no configured ProxyCommand or tailscale CLI is available"
            )
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
        "--authority-state-root",
        "--state-root",
        dest="authority_state_root",
        default="~/Library/Application Support/Codex Workbench",
        help="state-root used by the remote authority (absolute path or ~/...)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run local preflight and print the plan without SSH, writes, launchctl, or MCP changes",
    )
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
    if not source.is_dir():
        raise SystemExit(f"Workbench source is not a directory: {source}")
    authority_ssh_alias = args.authority_ssh_alias.strip()
    if not authority_ssh_alias or authority_ssh_alias.startswith("-") or any(
        character.isspace() for character in authority_ssh_alias
    ):
        raise SystemExit("--authority-ssh-alias must be one non-option SSH destination")
    authority_state_root = remote_state_root(args.authority_state_root)
    remote_binary = authority_mcp_binary(args.authority_state_root)
    transport_arguments = ssh_transport_arguments(
        authority_ssh_alias,
        args.ssh_transport,
        args.tailscale_native_ssh_port,
        probe_config=not args.dry_run,
    )

    log_root = absolute_path(Path.home() / "Library" / "Logs" / "Codex Workbench")
    launch_agents = absolute_path(Path.home() / "Library" / "LaunchAgents")
    assert_directory_target(log_root, "log root")
    assert_directory_target(launch_agents, "LaunchAgents root")
    domain = f"gui/{run('id', '-u').stdout.strip()}"
    client_id = "macbook-" + "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in socket.gethostname()
    )
    for label in (TUNNEL_LABEL, HEARTBEAT_LABEL):
        assert_file_target(
            launch_agents / f"{label}.plist",
            f"{label} LaunchAgent",
        )
    preflight_client_plists(
        source,
        log_root,
        client_id,
        authority_ssh_alias,
        transport_arguments,
        remote_binary,
    )
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Codex CLI is required to register the Workbench MCP entry")
    preflight_global_agent_targets(Path.home())
    preflight_managed_agent_skills(source)
    mcp_before = read_mcp_registration(codex, "codex-workbench")
    if not args.dry_run:
        preflight_remote_mcp(authority_ssh_alias, transport_arguments, args.authority_state_root)
    if args.dry_run:
        print("Codex Workbench MacBook dry-run: no filesystem writes, SSH, launchctl, or MCP changes")
        print(f"plan: source={source}")
        print(f"plan: authority={authority_ssh_alias}")
        print(f"plan: authority state-root={authority_state_root}")
        print(f"plan: remote MCP executable={remote_binary}")
        print(
            "plan: SSH transport="
            + ("local-only auto detection deferred" if args.ssh_transport == "auto" else args.ssh_transport)
        )
        print(f"plan: local log root={log_root}")
        print(f"plan: LaunchAgents={launch_agents}")
        print("plan: managed Code-as-Harness and Archify projections for Codex and Claude Code")
        print(f"plan: MCP registration={'replace existing' if mcp_before else 'create'} codex-workbench")
        return 0

    service_paths = {
        label: launch_agents / f"{label}.plist"
        for label in (TUNNEL_LABEL, HEARTBEAT_LABEL)
    }
    service_was_loaded = {
        label: run("launchctl", "print", f"{domain}/{label}", check=False).returncode == 0
        for label in service_paths
    }
    transaction = InstallTransaction(log_root)
    if not log_root.exists():
        transaction.track_created_directory(log_root)
    if not launch_agents.exists():
        transaction.track_created_directory(launch_agents)
    for path, label in (
        *[(path, f"{label} LaunchAgent") for label, path in service_paths.items()],
        (Path.home() / ".codex" / "skills" / "code-as-harness", "Codex Code-as-Harness skill"),
        (Path.home() / ".codex" / "AGENTS.md", "Codex policy"),
        (Path.home() / ".codex" / "skills" / "archify", "Codex Archify skill"),
        (Path.home() / ".claude" / "skills" / "code-as-harness", "Claude Code-as-Harness skill"),
        (Path.home() / ".claude" / "CLAUDE.md", "Claude policy"),
        (Path.home() / ".claude" / "skills" / "archify", "Claude Archify skill"),
    ):
        transaction.snapshot(path, label)
    services_touched = False
    mcp_touched = False
    try:
        install_code_as_harness(source)
        install_archify(source)

        log_root.mkdir(parents=True, exist_ok=True)
        launch_agents.mkdir(parents=True, exist_ok=True)
        for label, plist_path in service_paths.items():
            payload = render_client_plist(
                source,
                log_root,
                client_id,
                authority_ssh_alias,
                label,
                transport_arguments,
                remote_binary,
            )
            plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
            plist_path.chmod(0o600)
            services_touched = True
            run("launchctl", "bootout", domain, str(plist_path), check=False)
            run("launchctl", "bootstrap", domain, str(plist_path))
            run("launchctl", "enable", f"{domain}/{label}")
            run("launchctl", "kickstart", "-k", f"{domain}/{label}")
        remote_command = f"exec {remote_shell_quote(remote_binary)} mcp"
        mcp_touched = True
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
    except BaseException as error:
        rollback_errors: list[str] = []
        if mcp_touched:
            try:
                run(codex, "mcp", "remove", "codex-workbench", check=False)
                if mcp_before is not None:
                    run(*mcp_add_command(codex, mcp_before))
            except BaseException as rollback_error:
                rollback_errors.append(f"MCP registration: {rollback_error}")
        if services_touched:
            for label, path in service_paths.items():
                try:
                    run("launchctl", "bootout", domain, str(path), check=False)
                except BaseException as rollback_error:
                    rollback_errors.append(f"{label} service: {rollback_error}")
        try:
            transaction.rollback()
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        if services_touched:
            for label, path in service_paths.items():
                if not service_was_loaded[label] or not path.is_file():
                    continue
                try:
                    run("launchctl", "bootstrap", domain, str(path))
                    run("launchctl", "enable", f"{domain}/{label}")
                except BaseException as rollback_error:
                    rollback_errors.append(f"{label} service restore: {rollback_error}")
        if rollback_errors:
            raise SystemExit(f"MacBook installation failed: {error}; rollback failed: {'; '.join(rollback_errors)}") from error
        raise
    else:
        transaction.commit()
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
