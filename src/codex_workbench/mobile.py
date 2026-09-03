"""Codex native Remote integration for the Workbench.

The Workbench remains the durable authority.  This module configures the
Workbench plugin and MCP server used by Codex Remote.  The native desktop app
is the sole owner of Remote Control and QR pairing; starting a second CLI
app-server would conflict with the desktop host.

All mutating operations accept an injectable runner and support ``dry_run`` so
the installer/CLI can show an exact plan without touching the user's Codex
home.  The ``CODEX_HOME`` used for management commands is always the user's
Codex home, even when the Workbench wrapper has exported its isolated home.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Mapping, Sequence


MCP_SERVER_NAME = "codex-workbench"
PLUGIN_NAME = "codex-workbench"
MARKETPLACE_NAME = "codex-workbench"
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
DESKTOP_REMOTE_SETUP_PATH = (
    "Settings > Connections > Control this Mac or PC > Set up or Add"
)
REMOTE_DOCUMENTATION_URL = "https://learn.chatgpt.com/docs/remote"


class MobileRemoteError(RuntimeError):
    """A safe, user-facing failure from the local Codex management API."""


@dataclass(frozen=True)
class CommandResult:
    """Small runner result accepted by :class:`MobileRemote`.

    Keeping this shape independent of ``subprocess.CompletedProcess`` makes
    the API straightforward to test without executing a real Codex command.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], dict[str, str]], object]


def _subprocess_runner(command: list[str], env: dict[str, str]) -> CommandResult:
    try:
        result = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(1, stderr=type(error).__name__)
    return CommandResult(
        result.returncode,
        result.stdout or "",
        result.stderr or "",
    )


def _coerce_result(value: object) -> CommandResult:
    """Accept CompletedProcess-like, mapping, tuple, or CommandResult values."""

    if isinstance(value, CommandResult):
        return value
    if isinstance(value, subprocess.CompletedProcess):
        return CommandResult(
            int(value.returncode),
            str(value.stdout or ""),
            str(value.stderr or ""),
        )
    if isinstance(value, Mapping):
        return CommandResult(
            int(value.get("returncode", value.get("code", 1))),
            str(value.get("stdout", "") or ""),
            str(value.get("stderr", "") or ""),
        )
    if isinstance(value, tuple):
        if len(value) == 2:
            return CommandResult(int(value[0]), str(value[1] or ""), "")
        if len(value) >= 3:
            return CommandResult(int(value[0]), str(value[1] or ""), str(value[2] or ""))
    raise TypeError("mobile command runner must return CommandResult, CompletedProcess, mapping, or tuple")


def _path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _resolve_user_codex_home(explicit: str | Path | None) -> Path:
    """Resolve the real user Codex home and reject Workbench's isolated home.

    The installed Workbench wrapper exports ``CODEX_HOME=<state>/codex-home``
    for the authority process.  Native mobile Codex must be configured in the
    user's normal ``~/.codex`` tree instead.  A custom user home is allowed,
    but an explicitly supplied Workbench-isolated path is rejected whenever
    the wrapper markers make it unambiguous.
    """

    candidate = (_path(explicit) or (Path.home() / ".codex")).resolve(strict=False)
    process_home = _path(os.environ.get("CODEX_WORKBENCH_PROCESS_HOME"))
    ambient_codex_home = _path(os.environ.get("CODEX_HOME"))
    isolated: set[Path] = set()
    if process_home is not None:
        isolated.add((process_home.parent / "codex-home").resolve(strict=False))
        isolated.add(process_home.resolve(strict=False))
        if ambient_codex_home is not None:
            isolated.add(ambient_codex_home.resolve(strict=False))
    if candidate in isolated:
        raise MobileRemoteError(
            "refusing to configure mobile Remote Control in the Workbench isolated CODEX_HOME; "
            "use the user's ~/.codex home"
        )
    return candidate


def _management_environment(user_codex_home: Path) -> dict[str, str]:
    """Build an environment that cannot inherit the wrapper's isolated home."""

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(user_codex_home)
    return environment


def _default_marketplace_source() -> str:
    # In an installed app the source is copied next to ``src``.  A source
    # checkout has the same shape.  Fall back to the public repository when a
    # wheel or another packaging layout does not carry the marketplace file.
    here = Path(__file__).resolve()
    for root in (here.parents[2], here.parents[3], Path.cwd()):
        if (root / ".agents" / "plugins" / "marketplace.json").is_file():
            return str(root)
    return "lisihao/codex-workbench"


def _default_workbench_command() -> list[str]:
    configured = os.environ.get("CODEX_WORKBENCH_BINARY")
    if configured:
        return [configured]
    found = shutil.which("codex-workbench")
    if found:
        return [found]
    here = Path(__file__).resolve()
    for root in (here.parents[2], here.parents[3]):
        candidate = root / "bin" / "codex-workbench"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return ["codex-workbench"]


def _plugin_registration(raw_output: str) -> dict[str, object]:
    """Reduce plugin-list JSON to non-sensitive installation state."""

    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return {"installed": False, "available": False, "selector": None}
    if not isinstance(payload, dict):
        return {"installed": False, "available": False, "selector": None}
    installed = payload.get("installed")
    available = payload.get("available")
    installed_item = next((
        item
        for item in installed or ()
        if isinstance(item, dict)
        and item.get("name") == PLUGIN_NAME
        and item.get("installed") is not False
        and item.get("enabled") is not False
    ), None) if isinstance(installed, list) else None
    installed_ok = installed_item is not None or any(
        isinstance(item, dict)
        and item.get("pluginId") == PLUGIN_SELECTOR
        and item.get("installed") is not False
        for item in installed or ()
    ) if isinstance(installed, list) else False
    available_ok = any(
        isinstance(item, dict) and item.get("pluginId") == PLUGIN_SELECTOR
        for item in available or ()
    ) if isinstance(available, list) else False
    selector = installed_item.get("pluginId") if isinstance(installed_item, dict) else None
    return {"installed": installed_ok, "available": available_ok, "selector": selector}


def _mcp_matches(raw_output: str, expected_command: Sequence[str]) -> bool:
    """Return whether the existing Workbench MCP is the requested stdio command."""

    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("enabled") is False:
        return False
    transport = payload.get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "stdio":
        return False
    actual = [transport.get("command"), *(transport.get("args") or [])]
    return actual == list(expected_command)


class MobileRemote:
    """Configure Workbench integration for desktop-owned Codex Remote."""

    def __init__(
        self,
        *,
        codex_binary: str | Path = "codex",
        user_codex_home: str | Path | None = None,
        marketplace_source: str | Path | None = None,
        workbench_binary: str | Path | None = None,
        runner: CommandRunner | None = None,
        dry_run: bool = False,
    ) -> None:
        self.codex_binary = str(codex_binary)
        self.user_codex_home = _resolve_user_codex_home(user_codex_home)
        self.marketplace_source = str(marketplace_source) if marketplace_source is not None else _default_marketplace_source()
        self.workbench_command = [str(workbench_binary)] if workbench_binary is not None else _default_workbench_command()
        self.runner = runner or _subprocess_runner
        self.dry_run = dry_run

    def _environment(self) -> dict[str, str]:
        return _management_environment(self.user_codex_home)

    def _run(self, command: Sequence[str]) -> CommandResult:
        if self.dry_run:
            return CommandResult(0)
        try:
            return _coerce_result(self.runner(list(command), self._environment()))
        except MobileRemoteError:
            raise
        except Exception as error:
            raise MobileRemoteError(f"Codex command failed before execution: {type(error).__name__}") from error

    def _require_success(self, command: Sequence[str], label: str) -> CommandResult:
        result = self._run(command)
        if result.returncode != 0:
            raise MobileRemoteError(f"{label} failed (exit {result.returncode})")
        return result

    def _mcp_command(self) -> list[str]:
        return [
            self.codex_binary,
            "mcp",
            "add",
            MCP_SERVER_NAME,
            "--",
            *self.workbench_command,
            "mcp",
        ]

    def _marketplace_command(self) -> list[str]:
        command = [
            self.codex_binary,
            "plugin",
            "marketplace",
            "add",
            self.marketplace_source,
        ]
        if self.marketplace_source == "lisihao/codex-workbench":
            command.extend(("--ref", "main"))
        command.append("--json")
        return command

    def _plugin_command(self) -> list[str]:
        return [self.codex_binary, "plugin", "add", PLUGIN_SELECTOR, "--json"]

    def status(self) -> dict[str, object]:
        commands = [
            [self.codex_binary, "mcp", "get", MCP_SERVER_NAME, "--json"],
            [self.codex_binary, "plugin", "list", "--available", "--json"],
        ]
        if self.dry_run:
            return {
                "ok": False,
                "action": "status",
                "dry_run": True,
                "user_codex_home": str(self.user_codex_home),
                "commands": commands,
            }

        mcp = self._run(commands[0])
        plugins = self._run(commands[1])
        expected_mcp = [*self.workbench_command, "mcp"]
        mcp_ok = mcp.returncode == 0 and _mcp_matches(mcp.stdout, expected_mcp)
        plugin = _plugin_registration(plugins.stdout) if plugins.returncode == 0 else {"installed": False, "available": False, "selector": None}
        integration_ready = bool(mcp_ok and plugin["installed"])
        return {
            "ok": integration_ready,
            "action": "status",
            "dry_run": False,
            "user_codex_home": str(self.user_codex_home),
            "integration_ready": integration_ready,
            "pairing_state": "not_attested",
            "remote_control": {
                "owner": "desktop_app",
                "setup_path": DESKTOP_REMOTE_SETUP_PATH,
                "documentation": REMOTE_DOCUMENTATION_URL,
            },
            "mcp": {"configured": mcp_ok},
            "plugin": plugin,
        }

    def enable(self) -> dict[str, object]:
        commands = [self._marketplace_command(), self._plugin_command(), self._mcp_command()]
        if self.dry_run:
            return {
                "ok": True,
                "action": "enable",
                "dry_run": True,
                "idempotent": True,
                "user_codex_home": str(self.user_codex_home),
                "commands": commands,
            }
        executed: list[dict[str, object]] = []
        skipped: list[str] = []

        plugin_list = self._require_success(
            [self.codex_binary, "plugin", "list", "--available", "--json"],
            "Codex plugin inspection",
        )
        plugin = _plugin_registration(plugin_list.stdout)
        if not plugin["installed"]:
            if not plugin["available"]:
                result = self._require_success(
                    commands[0], "Codex Workbench marketplace registration"
                )
                executed.append(
                    {
                        "action": "Codex Workbench marketplace registration",
                        "returncode": result.returncode,
                    }
                )
            else:
                skipped.append("Codex Workbench marketplace already registered")
            result = self._require_success(
                commands[1], "Codex Workbench plugin installation"
            )
            executed.append(
                {
                    "action": "Codex Workbench plugin installation",
                    "returncode": result.returncode,
                }
            )
        else:
            skipped.append("Codex Workbench plugin already installed")

        mcp_get = self._run(
            [self.codex_binary, "mcp", "get", MCP_SERVER_NAME, "--json"]
        )
        expected_mcp = [*self.workbench_command, "mcp"]
        if mcp_get.returncode == 0:
            if not _mcp_matches(mcp_get.stdout, expected_mcp):
                raise MobileRemoteError(
                    "existing codex-workbench MCP does not match the requested command; "
                    "refusing to overwrite it"
                )
            skipped.append("Codex Workbench MCP already configured")
        else:
            result = self._require_success(commands[2], "Codex Workbench MCP registration")
            executed.append(
                {
                    "action": "Codex Workbench MCP registration",
                    "returncode": result.returncode,
                }
            )

        return {
            "ok": True,
            "action": "enable",
            "dry_run": False,
            "idempotent": True,
            "user_codex_home": str(self.user_codex_home),
            "integration_ready": True,
            "pairing_state": "not_attested",
            "remote_control": {
                "owner": "desktop_app",
                "setup_path": DESKTOP_REMOTE_SETUP_PATH,
                "documentation": REMOTE_DOCUMENTATION_URL,
            },
            "executed": executed,
            "skipped": skipped,
            "preserved": {"other_codex_config": True},
        }

    def pair(self) -> dict[str, object]:
        # Pairing is intentionally desktop-owned.  The ChatGPT/Codex desktop
        # app presents the QR code and performs account/workspace approval.
        return {
            "ok": True,
            "action": "pair",
            "dry_run": self.dry_run,
            "manual_pairing_required": True,
            "pairing_state": "not_confirmed",
            "pairing_surface": "desktop_app",
            "desktop_setup_path": DESKTOP_REMOTE_SETUP_PATH,
            "documentation": REMOTE_DOCUMENTATION_URL,
            "commands": [],
            "pairing_code_available": False,
            "persisted": False,
            "next_step": (
                "在 Mac mini 的 ChatGPT/Codex 桌面 App 打开 "
                f"{DESKTOP_REMOTE_SETUP_PATH}，显示二维码后用手机扫描并批准。"
            ),
        }

    def disable(self) -> dict[str, object]:
        return {
            "ok": True,
            "action": "disable",
            "dry_run": self.dry_run,
            "manual_action_required": True,
            "remote_control": {
                "owner": "desktop_app",
                "setup_path": DESKTOP_REMOTE_SETUP_PATH,
            },
            "commands": [],
            "next_step": (
                "请在 ChatGPT/Codex 桌面 App 的 Connections 设置中关闭 Remote；"
                "Workbench 不会启动或停止原生 Remote host。"
            ),
            "preserved": {"mcp": True, "plugin": True, "other_codex_config": True},
        }


def mobile_status(**kwargs: Any) -> dict[str, object]:
    return MobileRemote(**kwargs).status()


def mobile_enable(**kwargs: Any) -> dict[str, object]:
    return MobileRemote(**kwargs).enable()


def mobile_pair(**kwargs: Any) -> dict[str, object]:
    return MobileRemote(**kwargs).pair()


def mobile_disable(**kwargs: Any) -> dict[str, object]:
    return MobileRemote(**kwargs).disable()


# Short aliases are useful to a CLI adapter and keep the library API pleasant.
status = mobile_status
enable = mobile_enable
pair = mobile_pair
disable = mobile_disable


__all__ = [
    "CommandResult",
    "MobileRemote",
    "MobileRemoteError",
    "disable",
    "enable",
    "mobile_disable",
    "mobile_enable",
    "mobile_pair",
    "mobile_status",
    "pair",
    "status",
]
