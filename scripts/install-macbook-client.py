#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import subprocess


LABEL = "com.lisihao.codex-workbench-tunnel"


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    run("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "macmini", "true")

    log_root = Path.home() / "Library" / "Logs" / "Codex Workbench"
    log_root.mkdir(parents=True, exist_ok=True)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LABEL}.plist"
    template = (source / "launchd" / f"{LABEL}.plist.in").read_text()
    rendered = template.replace("__LOG_ROOT__", str(log_root))
    plistlib.loads(rendered.encode())
    plist_path.write_text(rendered)
    plist_path.chmod(0o600)

    domain = f"gui/{run('id', '-u').stdout.strip()}"
    run("launchctl", "bootout", domain, str(plist_path), check=False)
    run("launchctl", "bootstrap", domain, str(plist_path))
    run("launchctl", "enable", f"{domain}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{domain}/{LABEL}")
    print("Codex Workbench cockpit: http://127.0.0.1:18766")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

