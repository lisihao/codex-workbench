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


class CoordinatorAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorityIdentity:
    instance_id: str
    pid: int
    host: str
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
        return result.stdout.strip()
    return "unknown"
