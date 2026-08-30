from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid
import re
import sys
from typing import Callable


class CoordinatorAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorityIdentity:
    instance_id: str
    pid: int
    host: str
    machine_id: str
    boot_id: str
    started_at: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


class CoordinatorAuthorityLease:
    """Process-lifetime exclusive ownership of the Mac mini coordinator."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self.identity = AuthorityIdentity(
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            host=socket.gethostname(),
            machine_id=authority_machine_id(),
            boot_id=machine_boot_id(),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def __enter__(self) -> AuthorityIdentity:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            owner = handle.read().strip() or "unknown owner"
            handle.close()
            raise CoordinatorAuthorityError(
                f"coordinator authority is already held: {owner}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(self.identity.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self.identity

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def machine_boot_id() -> str:
    linux_id = Path("/proc/sys/kernel/random/boot_id")
    if linux_id.is_file():
        return linux_id.read_text().strip()
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.boottime"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return normalize_boot_id(result.stdout)
    return "unknown"


MachineIdRunner = Callable[[list[str]], tuple[int, str]]


def _run_machine_id(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def authority_machine_id(
    *,
    platform_name: str | None = None,
    runner: MachineIdRunner = _run_machine_id,
    linux_machine_id: Path = Path("/etc/machine-id"),
) -> str:
    """Return the stable platform identity used to fence the authority ledger."""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        code, output = runner(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
        )
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', output)
        if code == 0 and match:
            return "darwin:ioplatformuuid:" + match.group(1).lower()
        raise CoordinatorAuthorityError("macOS IOPlatformUUID is unavailable")
    if platform_name.startswith("linux") and linux_machine_id.is_file():
        value = linux_machine_id.read_text().strip().lower()
        if re.fullmatch(r"[0-9a-f-]{16,64}", value):
            return "linux:machine-id:" + value
    raise CoordinatorAuthorityError(f"stable authority machine ID is unavailable on {platform_name}")


def normalize_boot_id(raw: str) -> str:
    """Return a stable boot identity from platform output.

    macOS may format ``kern.boottime`` with a recalculated microsecond field;
    the boot epoch second is the stable machine-restart boundary.
    """
    value = raw.strip()
    match = re.search(r"\bsec\s*=\s*(\d+)", value)
    if match:
        return f"darwin:{match.group(1)}"
    return value
