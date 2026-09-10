"""Read-only process observations supplement explicit recovery operator assertions."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


class RecoveryProcessError(ValueError):
    """The source cannot be shown idle for a local source-only extraction."""


def _inside_source(cwd: str, source: Path) -> bool:
    path = Path(cwd).resolve()
    return path == source or source in path.parents


def source_process_ids(worktree: Path) -> tuple[int, ...]:
    """Observe same-user processes whose cwd is the source or a descendant.

    This does not establish that remote effects ended or that a process which
    changed cwd exited. The existing explicit operator assertions remain
    required. No signal is sent and no process arguments are collected.
    """

    source = worktree.resolve(strict=True)
    if not source.is_dir():
        raise RecoveryProcessError("recovery source must be a directory")
    found: set[int] = set()
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-F0pn"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecoveryProcessError("cannot inspect source process activity") from error
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise RecoveryProcessError("source process inspection was incomplete")
        pid: int | None = None
        for field in result.stdout.split(b"\0"):
            field = field.lstrip(b"\n")
            if field.startswith(b"p"):
                try:
                    pid = int(field[1:])
                except ValueError as error:
                    raise RecoveryProcessError("invalid process inspection record") from error
            elif field.startswith(b"n"):
                if pid is None:
                    raise RecoveryProcessError("process cwd lacks an owner")
                if _inside_source(os.fsdecode(field[1:]), source):
                    found.add(pid)
    elif sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                cwd = os.readlink(entry / "cwd")
            except FileNotFoundError:
                # A process can exit between enumeration and reading its cwd.
                continue
            except OSError as error:
                raise RecoveryProcessError("source process inspection was incomplete") from error
            if _inside_source(cwd, source):
                found.add(int(entry.name))
    else:
        raise RecoveryProcessError("source process inspection is unsupported on this host")
    return tuple(sorted(found))


def assert_recovery_source_idle(worktree: Path) -> None:
    """Refuse source-only extraction while an observed source process is alive."""

    pids = source_process_ids(worktree)
    if pids:
        sample = ", ".join(str(pid) for pid in pids[:8])
        raise RecoveryProcessError(
            f"recovery source has {len(pids)} active process(es); pid sample: {sample}"
        )
