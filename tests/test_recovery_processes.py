from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.process_probe_fixture import isolated_process_catalog

from codex_workbench.recovery_processes import (
    RecoveryProcessError,
    _darwin_process_cwds,
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
            with patch("codex_workbench.recovery_processes.sys.platform", "darwin"), patch(
                "codex_workbench.recovery_processes._darwin_process_cwds",
                return_value=((41, f"{source}/nested"), (42, f"{source}-other")),
            ):
                self.assertEqual(source_process_ids(source), (41,))

    def test_probe_errors_refuse_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("codex_workbench.recovery_processes.sys.platform", "darwin"), patch(
                "codex_workbench.recovery_processes._darwin_process_cwds",
                side_effect=RecoveryProcessError("cwd lookup was incomplete"),
            ):
                with self.assertRaises(RecoveryProcessError):
                    source_process_ids(Path(directory))

    def test_darwin_probe_uses_same_process_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            with patch(
                "codex_workbench.recovery_processes.sys.platform", "darwin"
            ), patch(
                "codex_workbench.recovery_processes._darwin_process_cwds",
                return_value=(),
            ) as probe:
                self.assertEqual(source_process_ids(source), ())
            probe.assert_called_once()

    def test_darwin_probe_rejects_incomplete_native_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "codex_workbench.recovery_processes.sys.platform", "darwin"
            ), patch(
                "codex_workbench.recovery_processes._darwin_process_cwds",
                side_effect=RecoveryProcessError("process list changed during inspection"),
            ) as run, self.assertRaisesRegex(
                RecoveryProcessError,
                "process list changed",
            ):
                source_process_ids(Path(directory))
            self.assertEqual(run.call_count, 1)

    def test_native_process_list_zero_fill_fails_closed(self) -> None:
        libproc = Mock()
        libproc.proc_listpids = Mock(side_effect=[4, 0])
        libproc.proc_pidinfo = Mock()
        with patch("codex_workbench.recovery_processes.ctypes.CDLL", return_value=libproc):
            with self.assertRaisesRegex(RecoveryProcessError, "process list failed"):
                _darwin_process_cwds(501)

    def test_native_cwd_zero_without_esrch_fails_closed(self) -> None:
        libproc = Mock()

        def list_pids(_kind, _uid, buffer, _size):
            if buffer is None:
                return 4
            buffer[0] = 123
            return 4

        libproc.proc_listpids = Mock(side_effect=list_pids)
        libproc.proc_pidinfo = Mock(return_value=0)
        with patch(
            "codex_workbench.recovery_processes.ctypes.CDLL", return_value=libproc
        ), patch(
            "codex_workbench.recovery_processes.ctypes.get_errno", return_value=0
        ), patch(
            "codex_workbench.recovery_processes.subprocess.run",
            return_value=subprocess.CompletedProcess([], 2, "", "ps failed"),
        ):
            with self.assertRaisesRegex(RecoveryProcessError, "cwd lookup was incomplete"):
                _darwin_process_cwds(501)

    def test_native_cwd_zero_skips_ps_confirmed_zombie(self) -> None:
        libproc = Mock()

        def list_pids(_kind, _uid, buffer, _size):
            if buffer is None:
                return 4
            buffer[0] = 123
            return 4

        libproc.proc_listpids = Mock(side_effect=list_pids)
        libproc.proc_pidinfo = Mock(return_value=0)
        with patch(
            "codex_workbench.recovery_processes.ctypes.CDLL", return_value=libproc
        ), patch(
            "codex_workbench.recovery_processes.ctypes.get_errno", return_value=0
        ), patch(
            "codex_workbench.recovery_processes.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "Z   \n", ""),
        ) as run:
            self.assertEqual(_darwin_process_cwds(501), ())
        run.assert_called_once_with(
            ["/bin/ps", "-p", "123", "-o", "state="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )

    def test_native_cwd_zero_skips_pid_that_exited_after_snapshot(self) -> None:
        libproc = Mock()

        def list_pids(_kind, _uid, buffer, _size):
            if buffer is None:
                return 4
            buffer[0] = 123
            return 4

        libproc.proc_listpids = Mock(side_effect=list_pids)
        libproc.proc_pidinfo = Mock(return_value=0)
        with patch(
            "codex_workbench.recovery_processes.ctypes.CDLL", return_value=libproc
        ), patch(
            "codex_workbench.recovery_processes.ctypes.get_errno", return_value=0
        ), patch(
            "codex_workbench.recovery_processes.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", ""),
        ):
            self.assertEqual(_darwin_process_cwds(501), ())

    def test_native_cwd_zero_rejects_ps_confirmed_live_process(self) -> None:
        libproc = Mock()

        def list_pids(_kind, _uid, buffer, _size):
            if buffer is None:
                return 4
            buffer[0] = 123
            return 4

        libproc.proc_listpids = Mock(side_effect=list_pids)
        libproc.proc_pidinfo = Mock(return_value=0)
        with patch(
            "codex_workbench.recovery_processes.ctypes.CDLL", return_value=libproc
        ), patch(
            "codex_workbench.recovery_processes.ctypes.get_errno", return_value=0
        ), patch(
            "codex_workbench.recovery_processes.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "S   \n", ""),
        ), self.assertRaisesRegex(RecoveryProcessError, "cwd lookup was incomplete"):
            _darwin_process_cwds(501)

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
