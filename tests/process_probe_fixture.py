"""Keep Linux kernel probes within each test's explicitly owned process catalog."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
import sys
from unittest.mock import patch


@contextmanager
def isolated_process_catalog(pids: Sequence[int]) -> Iterator[None]:
    """Use real procfs data for fixture PIDs, excluding unrelated runner services.

    macOS tests still exercise the full lsof query. Production enumeration and
    its refusal of unreadable same-user processes are not changed.
    """
    if not sys.platform.startswith("linux"):
        yield
        return
    original = Path.iterdir

    def entries(path: Path) -> Iterator[Path]:
        if path == Path("/proc"):
            return iter(Path("/proc") / str(pid) for pid in pids)
        return original(path)

    with patch.object(Path, "iterdir", entries):
        yield
