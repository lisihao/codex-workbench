"""Read-only process observations supplement explicit recovery operator assertions."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import sys


class RecoveryProcessError(ValueError):
    """The source cannot be shown idle for a local source-only extraction."""


_PROC_UID_ONLY = 4
_PROC_PIDVNODEPATHINFO = 9
_MAXPATHLEN = 1024
# Darwin's public proc_info.h defines vnode_info as 152 bytes and
# proc_vnodepathinfo as two vnode_info_path records.
_VNODE_INFO_SIZE = 152
_VNODE_INFO_PATH_SIZE = _VNODE_INFO_SIZE + _MAXPATHLEN
_PROC_VNODEPATHINFO_SIZE = 2 * _VNODE_INFO_PATH_SIZE


def _inside_source(cwd: str, source: Path) -> bool:
    path = Path(cwd).resolve()
    return path == source or source in path.parents


def _darwin_process_cwds(uid: int) -> tuple[tuple[int, str], ...]:
    """Read same-UID cwd paths through Darwin libproc without spawning lsof."""

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError as error:
        raise RecoveryProcessError(
            "cannot inspect source process activity: Darwin libproc is unavailable"
        ) from error
    libproc.proc_listpids.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
    ]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int

    required = libproc.proc_listpids(_PROC_UID_ONLY, uid, None, 0)
    if required <= 0:
        raise RecoveryProcessError("cannot inspect source process activity: process list unavailable")
    pid_size = ctypes.sizeof(ctypes.c_int)
    capacity = max(required // pid_size + 64, 128)
    pids: tuple[int, ...] | None = None
    for _attempt in range(3):
        buffer = (ctypes.c_int * capacity)()
        used = libproc.proc_listpids(
            _PROC_UID_ONLY, uid, buffer, ctypes.sizeof(buffer)
        )
        if used <= 0:
            raise RecoveryProcessError(
                "cannot inspect source process activity: process list failed"
            )
        count = used // pid_size
        if used < ctypes.sizeof(buffer):
            pids = tuple(pid for pid in buffer[:count] if pid > 0)
            break
        capacity *= 2
    if pids is None:
        raise RecoveryProcessError(
            "cannot inspect source process activity: process list changed during inspection"
        )

    observed: list[tuple[int, str]] = []
    for pid in pids:
        info = ctypes.create_string_buffer(_PROC_VNODEPATHINFO_SIZE)
        ctypes.set_errno(0)
        size = libproc.proc_pidinfo(
            pid,
            _PROC_PIDVNODEPATHINFO,
            0,
            info,
            ctypes.sizeof(info),
        )
        if size <= 0:
            error_number = ctypes.get_errno()
            if error_number == errno.ESRCH:
                continue
            raise RecoveryProcessError(
                "cannot inspect source process activity: cwd lookup was incomplete"
            )
        if size != _PROC_VNODEPATHINFO_SIZE:
            raise RecoveryProcessError(
                "cannot inspect source process activity: cwd record was incomplete"
            )
        raw_path = info.raw[_VNODE_INFO_SIZE:_VNODE_INFO_PATH_SIZE].split(b"\0", 1)[0]
        if not raw_path:
            continue
        observed.append((pid, os.fsdecode(raw_path)))
    return tuple(observed)


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
        for pid, cwd in _darwin_process_cwds(os.getuid()):
            if _inside_source(cwd, source):
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
