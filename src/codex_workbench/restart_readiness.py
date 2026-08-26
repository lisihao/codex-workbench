from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable


CommandRunner = Callable[[list[str]], tuple[int, str]]


def _run(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def assess_restart_readiness(
    *,
    platform_name: str = sys.platform,
    current_user: str | None = None,
    launch_agent: Path | None = None,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    user = current_user or os.environ.get("USER", "")
    launch_agent = launch_agent or Path.home() / "Library" / "LaunchAgents" / "com.lisihao.codex-workbench.plist"
    if platform_name != "darwin":
        return {
            "ready": False,
            "supported": False,
            "blockers": ["restart readiness is only defined for the macOS authority host"],
        }

    blockers: list[str] = []
    filevault_code, filevault_output = runner(["/usr/bin/fdesetup", "status"])
    filevault_enabled = (
        True
        if filevault_code == 0 and "filevault is on" in filevault_output.lower()
        else False
        if filevault_code == 0 and "filevault is off" in filevault_output.lower()
        else None
    )
    if filevault_enabled is True:
        blockers.append(
            "FileVault requires a local unlock or a separately authorized fdesetup authrestart before reboot"
        )
    elif filevault_enabled is None:
        blockers.append("FileVault state is unknown")

    auto_code, auto_output = runner(
        [
            "/usr/bin/defaults",
            "read",
            "/Library/Preferences/com.apple.loginwindow",
            "autoLoginUser",
        ]
    )
    auto_login_user = auto_output.strip() if auto_code == 0 and auto_output.strip() else None
    if auto_login_user != user:
        blockers.append(
            f"automatic login is not configured for {user}; the user LaunchAgent cannot start before login"
        )

    launch_agent_installed = launch_agent.is_file()
    if not launch_agent_installed:
        blockers.append(f"Workbench LaunchAgent is missing: {launch_agent}")

    power_code, power_output = runner(["/usr/bin/pmset", "-g", "custom"])
    restart_match = re.search(r"\bautorestart\s+(\d+)", power_output)
    sleep_values = [int(value) for value in re.findall(r"^\s*sleep\s+(\d+)", power_output, re.MULTILINE)]
    restart_after_power_failure = power_code == 0 and restart_match is not None and int(restart_match.group(1)) == 1
    sleep_disabled = power_code == 0 and bool(sleep_values) and all(value == 0 for value in sleep_values)
    if not restart_after_power_failure:
        blockers.append("pmset autorestart is not enabled")
    if not sleep_disabled:
        blockers.append("system sleep is not disabled for every active power profile")

    tailscale_code, tailscale_output = runner(["/opt/homebrew/bin/tailscale", "status"])
    tailscale_ready = tailscale_code == 0 and "logged out" not in tailscale_output.lower()
    if not tailscale_ready:
        blockers.append("Tailscale is not ready on the authority host")

    return {
        "ready": not blockers,
        "supported": True,
        "filevault_enabled": filevault_enabled,
        "auto_login_user": auto_login_user,
        "launch_agent_installed": launch_agent_installed,
        "tailscale_ready": tailscale_ready,
        "power": {
            "restart_after_power_failure": restart_after_power_failure,
            "sleep_disabled": sleep_disabled,
        },
        "blockers": blockers,
    }
