from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.process_probe_fixture import isolated_process_catalog

from codex_workbench.recovery_processes import (
    RecoveryProcessError,
    assert_recovery_source_idle,
    source_process_ids,
)


class RecoveryProcessesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process_ids = []
        self.enterContext(isolated_process_catalog(self.process_ids))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "local process probe")
    def test_live_source_process_rejects_and_exit_allows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                cwd=source, stdout=subprocess.PIPE, text=True,
            )
            self.process_ids.append(child.pid)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                self.assertIn(child.pid, source_process_ids(source))
                with self.assertRaisesRegex(RecoveryProcessError, "active process"):
                    assert_recovery_source_idle(source)
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()
            assert_recovery_source_idle(source)

    def test_darwin_probe_matches_descendants_not_prefix_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            raw = f"p41\0\nfcwd\0n{source}/nested\0\np42\0\nfcwd\0n{source}-other\0".encode()
            result = subprocess.CompletedProcess([], 0, raw, b"")
            with patch("codex_workbench.recovery_processes.sys.platform", "darwin"), patch(
                "codex_workbench.recovery_processes.subprocess.run", return_value=result
            ):
                self.assertEqual(source_process_ids(source), (41,))

    def test_probe_errors_refuse_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for result in (
                subprocess.CompletedProcess([], 2, b"", b""),
                subprocess.CompletedProcess([], 0, b"", b"permission denied"),
            ):
                with self.subTest(result=result), patch("codex_workbench.recovery_processes.sys.platform", "darwin"), patch(
                    "codex_workbench.recovery_processes.subprocess.run", return_value=result
                ):
                    with self.assertRaises(RecoveryProcessError):
                        source_process_ids(Path(directory))

    def test_unreadable_linux_process_refuses_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            entry = source / "123"
            entry.mkdir()
            with (
                patch("codex_workbench.recovery_processes.sys.platform", "linux"),
                patch.object(Path, "iterdir", return_value=iter([entry])),
                patch("codex_workbench.recovery_processes.os.readlink", side_effect=PermissionError("denied")),
            ):
                with self.assertRaisesRegex(RecoveryProcessError, "inspection was incomplete"):
                    source_process_ids(source)
