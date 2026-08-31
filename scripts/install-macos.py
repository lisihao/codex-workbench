#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "com.lisihao.codex-workbench"
QUOTA_LABEL = "com.lisihao.codex-workbench-quota"
DEFAULT_TAILSCALE_HTTPS_PORT = 10443
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


def macos_machine_id() -> str:
    result = run(
        "/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice",
        check=False,
    )
    import re
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', result.stdout)
    if result.returncode != 0 or match is None:
        raise SystemExit("macOS IOPlatformUUID is unavailable; refusing to bind authority")
    return "darwin:ioplatformuuid:" + match.group(1).lower()


def network_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def configure_tailscale_serve(
    tailscale: str,
    socket_path: str,
    *,
    https_port: int,
    native_ssh_port: int,
) -> None:
    socket_argument = f"--socket={socket_path}"
    run(
        tailscale,
        socket_argument,
        "serve",
        "--yes",
        "--bg",
        f"--https={https_port}",
        "http://127.0.0.1:8766",
    )
    run(
        tailscale,
        socket_argument,
        "serve",
        "--yes",
        "--bg",
        f"--tcp={native_ssh_port}",
        "tcp://127.0.0.1:22",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--state-root", default="~/Library/Application Support/Codex Workbench")
    parser.add_argument("--codex-binary", default="~/.codex/packages/standalone/current/codex")
    parser.add_argument(
        "--claude-binary",
        help="absolute or resolvable Claude CLI to use for the passive quota producer",
    )
    parser.add_argument("--quota-snapshot-file")
    parser.add_argument(
        "--tailscale-socket",
        help="active userspace tailscaled LocalAPI socket; when set, configure tailnet-only cockpit and native SSH Serve",
    )
    parser.add_argument(
        "--tailscale-https-port",
        type=network_port,
        default=DEFAULT_TAILSCALE_HTTPS_PORT,
    )
    parser.add_argument(
        "--tailscale-native-ssh-port",
        type=network_port,
        default=DEFAULT_TAILSCALE_NATIVE_SSH_PORT,
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    state_root = Path(args.state_root).expanduser().resolve()
    app_root = state_root / "app"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / f"{LABEL}.plist"
    quota_plist_path = launch_agents / f"{QUOTA_LABEL}.plist"
    logs = state_root / "logs"
    runtime_root = state_root / "runtime"
    codex_home = state_root / "codex-home"
    process_home = state_root / "codex-process-home"
    quota_snapshot_file = (
        Path(args.quota_snapshot_file).expanduser().resolve()
        if args.quota_snapshot_file
        else state_root / "claude-quota.json"
    )
    selected_claude = args.claude_binary or shutil.which("claude")
    if selected_claude and "/" not in selected_claude:
        selected_claude = shutil.which(selected_claude)
    claude_binary = None
    if selected_claude:
        candidate = Path(selected_claude).expanduser().resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise SystemExit(f"Claude CLI is not executable: {candidate}")
        claude_binary = candidate

    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    process_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_file = state_root / "config.json"
    config_raw = json.loads(config_file.read_text()) if config_file.exists() else {}
    config_raw.update(
        {
            "deployment_role": "authority",
            "authority_host": __import__("socket").gethostname(),
            "authority_machine_id": macos_machine_id(),
            "quota_snapshot_file": str(quota_snapshot_file),
            # Migrate the previous five-minute default to a Claude-friendly
            # one-minute passive refresh while preserving any faster custom value.
            "quota_refresh_seconds": min(
                int(config_raw.get("quota_refresh_seconds", 60)),
                60,
            ),
        }
    )
    config_file.write_text(json.dumps(config_raw, indent=2) + "\n")
    config_file.chmod(0o600)
    codex_source = Path(args.codex_binary).expanduser().resolve(strict=True)
    codex_host_source = codex_source.with_name("codex-code-mode-host")
    if not codex_host_source.is_file():
        raise SystemExit(
            f"Codex workspace tool host is missing beside the selected CLI: {codex_host_source}"
        )
    codex_binary = runtime_root / "codex"
    codex_host = runtime_root / "codex-code-mode-host"
    shutil.copy2(codex_source, codex_binary)
    shutil.copy2(codex_host_source, codex_host)
    codex_binary.chmod(0o755)
    codex_host.chmod(0o755)
    codex_version = run(str(codex_binary), "--version").stdout.strip()
    auth_source = Path.home() / ".codex" / "auth.json"
    if not auth_source.exists():
        raise SystemExit("Codex subscription auth is missing at ~/.codex/auth.json")
    auth_link = codex_home / "auth.json"
    if auth_link.is_symlink():
        auth_link.unlink()
    elif auth_link.exists():
        raise SystemExit(f"refusing to replace non-symlink auth file: {auth_link}")
    auth_link.symlink_to(auth_source)
    commit = run("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    tag_result = run("git", "-C", str(source), "describe", "--tags", "--exact-match", check=False)
    tag = tag_result.stdout.strip() if tag_result.returncode == 0 else None
    version_line = next(
        line for line in (source / "src" / "codex_workbench" / "__init__.py").read_text().splitlines()
        if line.startswith("__version__")
    )
    version = version_line.split("=", 1)[1].strip().strip('"')
    if app_root.exists():
        backup = state_root / "previous-app"
        if backup.exists():
            shutil.rmtree(backup)
        app_root.rename(backup)
    shutil.copytree(source, app_root, ignore=shutil.ignore_patterns(".git", "__pycache__", ".workbench"))
    manifest = app_root / "install-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "commit": commit,
                "tag": tag,
                "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "codex_version": codex_version,
            },
            indent=2,
        )
        + "\n"
    )
    manifest.chmod(0o600)
    bin_dir = app_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = bin_dir / "codex-workbench"
    runtime_selector = app_root / "scripts" / "python-runtime"
    if not runtime_selector.is_file():
        raise SystemExit(f"Workbench Python runtime selector is missing: {runtime_selector}")
    wrapper.write_text(
        (
        "#!/bin/zsh\n"
        f"export PYTHONPATH={str(app_root / 'src')!r}\n"
        f"export CODEX_HOME={str(codex_home)!r}\n"
        f"export CODEX_WORKBENCH_PROCESS_HOME={str(process_home)!r}\n"
        f"export CODEX_WORKBENCH_CODEX={str(codex_binary)!r}\n"
        f"export CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE={str(quota_snapshot_file)!r}\n"
        )
        + (f"export CODEX_WORKBENCH_CLAUDE={str(claude_binary)!r}\n" if claude_binary else "")
        + f"exec {str(runtime_selector)!r} -m codex_workbench \"$@\"\n"
    )
    wrapper.chmod(0o755)

    template = (source / "launchd" / f"{LABEL}.plist.in").read_text()
    rendered = (
        template.replace("__APP_ROOT__", str(app_root))
        .replace("__STATE_ROOT__", str(state_root))
        .replace("__CODEX_BINARY__", str(codex_binary))
        .replace("__CODEX_HOME__", str(codex_home))
        .replace("__PROCESS_HOME__", str(process_home))
        .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
        .replace("__CLAUDE_BINARY__", str(claude_binary) if claude_binary else "")
    )
    plistlib.loads(rendered.encode())
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(rendered)
    plist_path.chmod(0o600)

    domain = f"gui/{run('id', '-u').stdout.strip()}"
    run("launchctl", "bootout", domain, str(plist_path), check=False)
    run("launchctl", "bootstrap", domain, str(plist_path))
    run("launchctl", "enable", f"{domain}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{domain}/{LABEL}")
    if claude_binary is not None:
        quota_template = (source / "launchd" / f"{QUOTA_LABEL}.plist.in").read_text()
        quota_rendered = (
            quota_template.replace("__APP_ROOT__", str(app_root))
            .replace("__STATE_ROOT__", str(state_root))
            .replace("__USER_HOME__", str(Path.home()))
            .replace("__CLAUDE_BINARY__", str(claude_binary))
            .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
        )
        plistlib.loads(quota_rendered.encode())
        quota_plist_path.write_text(quota_rendered)
        quota_plist_path.chmod(0o600)
        run("launchctl", "bootout", domain, str(quota_plist_path), check=False)
        run("launchctl", "bootstrap", domain, str(quota_plist_path))
        run("launchctl", "enable", f"{domain}/{QUOTA_LABEL}")
        run("launchctl", "kickstart", "-k", f"{domain}/{QUOTA_LABEL}")
    else:
        run("launchctl", "bootout", domain, str(quota_plist_path), check=False)
        quota_plist_path.unlink(missing_ok=True)
    if args.tailscale_socket:
        socket_path = str(Path(args.tailscale_socket).expanduser().resolve(strict=True))
        tailscale = shutil.which("tailscale")
        if not tailscale:
            raise SystemExit("--tailscale-socket requires the tailscale CLI")
        configure_tailscale_serve(
            tailscale,
            socket_path,
            https_port=args.tailscale_https_port,
            native_ssh_port=args.tailscale_native_ssh_port,
        )
    quota_status = f"installed {QUOTA_LABEL}" if claude_binary is not None else "skipped quota producer: Claude CLI unavailable"
    print(f"installed {LABEL} from {source} to {app_root}; {quota_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
