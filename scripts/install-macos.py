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
    parser.add_argument("--state-root", default="~/Library/Application Support/Codex Workbench")
    parser.add_argument("--codex-binary", default="~/.codex/packages/standalone/current/codex")
    parser.add_argument("--quota-snapshot-file")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    state_root = Path(args.state_root).expanduser().resolve()
    app_root = state_root / "app"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / f"{LABEL}.plist"
    logs = state_root / "logs"
    runtime_root = state_root / "runtime"
    codex_home = state_root / "codex-home"
    process_home = state_root / "codex-process-home"
    quota_snapshot_file = (
        Path(args.quota_snapshot_file).expanduser().resolve()
        if args.quota_snapshot_file
        else state_root / "claude-quota.json"
    )

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
            "quota_snapshot_file": str(quota_snapshot_file),
            "quota_refresh_seconds": int(config_raw.get("quota_refresh_seconds", 300)),
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
        "#!/bin/zsh\n"
        f"export PYTHONPATH={str(app_root / 'src')!r}\n"
        f"export CODEX_HOME={str(codex_home)!r}\n"
        f"export CODEX_WORKBENCH_PROCESS_HOME={str(process_home)!r}\n"
        f"export CODEX_WORKBENCH_CODEX={str(codex_binary)!r}\n"
        f"export CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE={str(quota_snapshot_file)!r}\n"
        "export CODEX_WORKBENCH_CLAUDE=/opt/homebrew/bin/claude\n"
        f"exec {str(runtime_selector)!r} -m codex_workbench \"$@\"\n"
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
    print(f"installed {LABEL} from {source} to {app_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
