from __future__ import annotations

from contextlib import ExitStack
import fcntl
import json
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock


PHYSICAL_TMP = Path(tempfile.gettempdir()).resolve()


class InstallerSafePointTests(unittest.TestCase):
    @staticmethod
    def _installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.py"
        spec = importlib.util.spec_from_file_location(
            "codex_workbench_macos_installer_safe_point", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _write_executable(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _pnpm_node(self, root: Path) -> Path:
        return self._write_executable(
            root / "pnpm-node",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf '%s\\n' 'v22.13.0'\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$2\" = \"--version\" ]; then\n"
            "  printf '%s\\n' '11.25.0'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
        )

    @staticmethod
    def _database_marker(database: Path) -> str:
        connection = sqlite3.connect(database)
        try:
            row = connection.execute("SELECT value FROM marker").fetchone()
            assert row is not None
            return str(row[0])
        finally:
            connection.close()

    def _run_installer(
        self,
        module,
        root: Path,
        *,
        authority_loaded: bool,
        release_lock_on_stop: bool,
        existing_lock: bool = False,
        fail_after_authority_restart: bool = False,
        release_restarted_lock_on_stop: bool = True,
    ):
        source = Path(__file__).resolve().parents[1]
        source_commit = subprocess.run(
            ("git", "-C", str(source), "rev-parse", "HEAD"),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        home = root / "home"
        auth_source = home / ".codex" / "auth.json"
        auth_source.parent.mkdir(parents=True)
        auth_source.write_text("{}\n", encoding="utf-8")
        research = root / "research"
        for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
            path = research / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        codex = self._write_executable(root / "codex", "#!/bin/sh\nexit 0\n")
        self._write_executable(root / "codex-code-mode-host", "#!/bin/sh\nexit 0\n")
        pnpm_node = self._pnpm_node(root)
        state_root = root / "state"
        state_root.mkdir()
        database = state_root / "state.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker(value) VALUES ('before-install')")
            connection.execute(
                "CREATE TABLE events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_type TEXT NOT NULL, payload_json TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        old_app = state_root / "app"
        old_app.mkdir()
        (old_app / "old.txt").write_text("old application\n", encoding="utf-8")

        initial_authority_identity = {
            "instance_id": "fixture-instance",
            "pid": 4242,
            "boot_id": "fixture-boot",
        }
        restarted_authority_identity = {
            "instance_id": "restart-instance",
            "pid": 4343,
            "boot_id": "restart-boot",
        }
        active_authority_identity = (
            initial_authority_identity if authority_loaded else None
        )
        restarted_authority_started = False
        lock_path = state_root / "coordinator.lock"
        if authority_loaded:
            lock_path.write_text(
                json.dumps(
                    initial_authority_identity
                )
                + "\n",
                encoding="utf-8",
            )
        elif existing_lock:
            lock_path.touch()
        lock_handle = None
        if authority_loaded:
            lock_handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        loaded_labels = {module.LABEL} if authority_loaded else set()
        authority_bootouts = 0
        events: list[str] = []
        original_acquire = module.acquire_authority_safe_point
        original_snapshot = module.InstallTransaction.snapshot_sqlite
        original_release = module.release_authority_safe_point
        original_rollback = module.InstallTransaction.rollback
        safe_point_holds = 0

        def fake_run(
            *command: str, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            nonlocal active_authority_identity, authority_bootouts, lock_handle
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no tag")
            if command[:2] == ("id", "-u"):
                return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
            if command[0] == "launchctl":
                action = command[1]
                if action == "print":
                    label = command[-1].rsplit("/", 1)[-1]
                    if label not in loaded_labels:
                        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
                    if label == module.LABEL and active_authority_identity is not None:
                        stdout = f"pid = {active_authority_identity['pid']}\n"
                    else:
                        stdout = "loaded\n"
                    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
                if action == "kill":
                    self.assertEqual(command[2:], ("SIGTERM", f"gui/501/{module.LABEL}"))
                    active = active_authority_identity
                    assert active is not None
                    is_restarted = active == restarted_authority_identity
                    events.append("restart-authority-sigterm" if is_restarted else "authority-sigterm")
                    release_lock = release_restarted_lock_on_stop if is_restarted else release_lock_on_stop
                    if not release_lock:
                        return subprocess.CompletedProcess(command, 1, stdout="", stderr="fixture SIGTERM failure")
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            "INSERT INTO events(event_type, payload_json) VALUES (?, ?)",
                            (
                                "coordinator.stopped",
                                json.dumps(
                                    {
                                        "instance_id": active["instance_id"],
                                        "boot_id": active["boot_id"],
                                    }
                                ),
                            ),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    events.append("restart-authority-stopped-receipt" if is_restarted else "coordinator-stopped-receipt")
                    assert lock_handle is not None
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                    lock_handle = None
                    active_authority_identity = None
                    events.append("restart-authority-lock-released" if is_restarted else "authority-lock-released")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if action == "bootout":
                    label = Path(command[-1]).stem
                    if label == module.LABEL:
                        authority_bootouts += 1
                        if authority_loaded and authority_bootouts == 1:
                            events.append("authority-bootout-after-receipt")
                        else:
                            events.append("authority-restart-bootout")
                    loaded_labels.discard(label)
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if action == "bootstrap":
                    loaded_labels.add(Path(command[-1]).stem)
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[0].endswith("/runtime/codex"):
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli fixture\n", stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def recording_acquire(*args, **kwargs):
            nonlocal safe_point_holds
            handle = original_acquire(*args, **kwargs)
            safe_point_holds += 1
            events.append("installer-lock-acquired")
            return handle

        def recording_release(handle):
            nonlocal safe_point_holds
            self.assertGreater(safe_point_holds, 0)
            safe_point_holds -= 1
            events.append("installer-lock-released")
            return original_release(handle)

        def recording_rollback(transaction):
            if fail_after_authority_restart:
                self.assertEqual(safe_point_holds, 1)
            events.append("transaction-rollback")
            return original_rollback(transaction)

        def recording_snapshot(transaction, path, label="SQLite database"):
            events.append("sqlite-snapshot")
            return original_snapshot(transaction, path, label)
        def fail_after_restart(domain: str, label: str, plist_path: Path) -> None:
            nonlocal active_authority_identity, lock_handle, restarted_authority_started
            if label != module.LABEL:
                events.append(f"{label}-restart-failed")
                raise RuntimeError("injected sidecar restart failure")
            self.assertEqual(safe_point_holds, 0)
            lock_path.write_text(
                json.dumps(restarted_authority_identity) + "\n",
                encoding="utf-8",
            )
            lock_handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            active_authority_identity = restarted_authority_identity
            restarted_authority_started = True
            loaded_labels.add(label)
            connection = sqlite3.connect(database)
            try:
                connection.execute("UPDATE marker SET value = 'new-authority-write'")
                connection.commit()
            finally:
                connection.close()
            events.append("restart-authority-started")


        result = None
        failure = None
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(module.Path, "home", return_value=home))
                stack.enter_context(mock.patch.object(module, "run", side_effect=fake_run))
                stack.enter_context(
                    mock.patch.object(module, "macos_machine_id", return_value="fixture-machine")
                )
                stack.enter_context(mock.patch.object(module.shutil, "which", return_value=None))
                stack.enter_context(mock.patch.object(module, "preflight_global_agent_targets"))
                stack.enter_context(mock.patch.object(module, "preflight_managed_agent_skills"))
                stack.enter_context(mock.patch.object(module, "install_code_as_harness"))
                stack.enter_context(mock.patch.object(module, "install_archify"))
                stack.enter_context(mock.patch.object(module, "initial_capability_refresh"))
                stack.enter_context(
                    mock.patch.object(
                        module, "verified_source_commit", return_value=source_commit
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        module,
                        "acquire_authority_safe_point",
                        side_effect=recording_acquire,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        module, "release_authority_safe_point", side_effect=recording_release
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        module.InstallTransaction,
                        "snapshot_sqlite",
                        new=recording_snapshot,
                    )
                )
                stack.enter_context(
                    mock.patch.object(module.InstallTransaction, "rollback", new=recording_rollback)
                )
                if fail_after_authority_restart:
                    stack.enter_context(
                        mock.patch.object(
                            module, "restart_launch_agent", side_effect=fail_after_restart
                        )
                    )
                stack.enter_context(
                    mock.patch.object(
                        module.sys,
                        "argv",
                        [
                            "install-macos.py",
                            "--source",
                            str(source),
                            "--state-root",
                            str(state_root),
                            "--codex-binary",
                            str(codex),
                            "--pnpm-node",
                            str(pnpm_node),
                            "--research-skill-source",
                            str(research),
                        ],
                    )
                )
                try:
                    result = module.main()
                except BaseException as error:
                    failure = error
        finally:
            if lock_handle is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

        return result, failure, events, state_root

    def test_loaded_authority_releases_lock_before_sqlite_snapshot(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            result, failure, events, state_root = self._run_installer(
                module,
                Path(directory),
                authority_loaded=True,
                release_lock_on_stop=True,
            )

            self.assertEqual(result, 0)
            self.assertIsNone(failure)
            self.assertLess(
                events.index("authority-sigterm"),
                events.index("coordinator-stopped-receipt"),
            )
            self.assertLess(
                events.index("coordinator-stopped-receipt"),
                events.index("authority-lock-released"),
            )
            self.assertLess(
                events.index("authority-lock-released"),
                events.index("installer-lock-acquired"),
            )
            self.assertLess(
                events.index("installer-lock-acquired"),
                events.index("authority-bootout-after-receipt"),
            )
            self.assertLess(
                events.index("authority-bootout-after-receipt"),
                events.index("sqlite-snapshot"),
            )
            self.assertEqual(
                self._database_marker(state_root / "state.sqlite"), "before-install"
            )

    def test_failed_authority_stop_does_not_snapshot_or_replace(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            result, failure, events, state_root = self._run_installer(
                module,
                Path(directory),
                authority_loaded=True,
                release_lock_on_stop=False,
            )

            self.assertIsNone(result)
            self.assertIsInstance(failure, SystemExit)
            assert isinstance(failure, SystemExit)
            self.assertIn("could not be signaled", str(failure))
            self.assertEqual(events, ["authority-sigterm"])
            self.assertFalse((state_root / "app" / "install-manifest.json").exists())
            self.assertEqual(
                (state_root / "app" / "old.txt").read_text(encoding="utf-8"),
                "old application\n",
            )
            self.assertEqual(
                self._database_marker(state_root / "state.sqlite"), "before-install"
            )

    def test_already_stopped_authority_installs_without_stop_phase(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            result, failure, events, state_root = self._run_installer(
                module,
                Path(directory),
                authority_loaded=False,
                release_lock_on_stop=False,
                existing_lock=True,
            )

            self.assertEqual(result, 0)
            self.assertIsNone(failure)
            self.assertNotIn("authority-stop", events)
            self.assertLess(
                events.index("sqlite-snapshot"),
                events.index("authority-restart-bootout"),
            )
            self.assertTrue((state_root / "app" / "install-manifest.json").is_file())
            self.assertLess(
                events.index("installer-lock-acquired"),
                events.index("sqlite-snapshot"),
            )

    def test_post_restart_failure_redrains_before_sqlite_rollback(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            result, failure, events, state_root = self._run_installer(
                module,
                Path(directory),
                authority_loaded=True,
                release_lock_on_stop=True,
                fail_after_authority_restart=True,
                release_restarted_lock_on_stop=True,
            )

            self.assertIsNone(result)
            self.assertIsInstance(failure, RuntimeError)
            assert isinstance(failure, RuntimeError)
            self.assertIn("injected sidecar restart failure", str(failure))

            started = events.index("restart-authority-started")
            sidecar_failure = events.index(f"{module.CAPABILITY_LABEL}-restart-failed")
            sigterm = events.index("restart-authority-sigterm")
            receipt = events.index("restart-authority-stopped-receipt")
            released = events.index("restart-authority-lock-released")
            reacquired = events.index("installer-lock-acquired", released)
            rollback = events.index("transaction-rollback")
            unlocked = events.index("installer-lock-released", rollback)
            self.assertLess(started, sidecar_failure)
            self.assertLess(sidecar_failure, sigterm)
            self.assertLess(sigterm, receipt)
            self.assertLess(receipt, released)
            self.assertLess(released, reacquired)
            self.assertLess(reacquired, rollback)
            self.assertLess(rollback, unlocked)
            self.assertEqual(self._database_marker(state_root / "state.sqlite"), "before-install")
            self.assertEqual((state_root / "app" / "old.txt").read_text(encoding="utf-8"), "old application\n")

    def test_post_restart_failure_skips_sqlite_rollback_when_drain_fails(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            result, failure, events, state_root = self._run_installer(
                module,
                Path(directory),
                authority_loaded=True,
                release_lock_on_stop=True,
                fail_after_authority_restart=True,
                release_restarted_lock_on_stop=False,
            )

            self.assertIsNone(result)
            self.assertIsInstance(failure, SystemExit)
            assert isinstance(failure, SystemExit)
            self.assertIn("rollback skipped because safe quiescence was not proven", str(failure))

            self.assertLess(events.index("restart-authority-started"), events.index(f"{module.CAPABILITY_LABEL}-restart-failed"))
            self.assertLess(events.index(f"{module.CAPABILITY_LABEL}-restart-failed"), events.index("restart-authority-sigterm"))
            self.assertNotIn("restart-authority-stopped-receipt", events)
            self.assertNotIn("transaction-rollback", events)
            self.assertEqual(self._database_marker(state_root / "state.sqlite"), "new-authority-write")

    def test_sidecar_is_unloaded_before_exit_observation_to_prevent_keepalive_restart(self) -> None:
        module = self._installer_module()
        status = subprocess.CompletedProcess(
            ("launchctl", "print", f"gui/501/{module.RADAR_LABEL}"),
            0,
            stdout="pid = 8080\n",
            stderr="",
        )
        events: list[str] = []
        running = True

        def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            nonlocal running
            action = command[1]
            self.assertEqual(action, "bootout")
            running = False
            events.append("bootout")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def observe_exit(pid: int, signal: int) -> None:
            self.assertEqual((pid, signal), (8080, 0))
            self.assertFalse(running)
            events.append("exit-observed")
            raise ProcessLookupError()

        with mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
            module.os, "kill", side_effect=observe_exit
        ):
            module.stop_sidecar_for_install(
                "gui/501", module.RADAR_LABEL, Path("/tmp/radar.plist"), status
            )

        self.assertEqual(events, ["bootout", "exit-observed"])

    def test_sidecar_still_alive_after_unload_blocks_snapshot(self) -> None:
        module = self._installer_module()
        status = subprocess.CompletedProcess([], 0, stdout="pid = 8080\n", stderr="")
        with mock.patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0)), mock.patch.object(
            module.os, "kill", return_value=None
        ), mock.patch.object(module, "AUTHORITY_DRAIN_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(SystemExit, "process remains alive"):
                module.stop_sidecar_for_install("gui/501", module.RADAR_LABEL, Path("/tmp/radar.plist"), status)

    def test_source_commit_requires_clean_committed_checkout(self) -> None:
        module = self._installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            source = Path(directory) / "source"
            source.mkdir()
            subprocess.run(("git", "init", "--quiet", str(source)), check=True)
            tracked = source / "tracked.txt"
            tracked.write_text("clean\n", encoding="utf-8")
            ignored = source / ".env"
            (source / ".gitignore").write_text(".env\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(source), "add", "tracked.txt", ".gitignore"), check=True)
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ),
                check=True,
            )

            commit = module.verified_source_commit(source)
            self.assertRegex(commit, r"^[0-9a-f]{40}$")

            ignored.write_text("ignored runtime secret\n", encoding="utf-8")
            tracked.write_text("dirty\n", encoding="utf-8")
            archive_destination = source.parent / "application"
            module.extract_verified_source_archive(source, commit, archive_destination)
            self.assertEqual(
                (archive_destination / "tracked.txt").read_text(encoding="utf-8"),
                "clean\n",
            )
            self.assertFalse((archive_destination / ".env").exists())
            with self.assertRaisesRegex(SystemExit, "uncommitted changes"):
                module.verified_source_commit(source)
            tracked.write_text("clean\n", encoding="utf-8")
            (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "uncommitted changes"):
                module.verified_source_commit(source)


if __name__ == "__main__":
    unittest.main()
