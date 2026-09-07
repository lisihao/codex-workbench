from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_workbench import plugin_cache_upgrade as upgrade


PHYSICAL_TMP = str(Path(tempfile.gettempdir()).resolve())


class FakeCodex:
    def __init__(
        self,
        *,
        codex_home: Path,
        source: Path,
        marketplace: str,
        plugin: str,
        installed_version: str,
    ) -> None:
        self.codex_home = codex_home
        self.source = source
        self.marketplace = marketplace
        self.plugin = plugin
        self.installed_version = installed_version
        self.commands: list[tuple[str, ...]] = []
        self.fail_add_after_cache_delete = False
        self.report_missing_version_after_add = False
        self.relative_source = False
        self.before_add: object = None
        self.source_type = "local"
        self.upgraded_source: Path | None = None
        self.omit_hook_after_add = False
        self.omit_skill_after_add = False

    @property
    def cache_root(self) -> Path:
        return (
            self.codex_home
            / "plugins"
            / "cache"
            / self.marketplace
            / self.plugin
        )

    def _result(
        self,
        command: tuple[str, ...],
        payload: object,
        *,
        returncode: int = 0,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            returncode,
            json.dumps(payload),
            stderr,
        )

    def __call__(
        self,
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert environment["CODEX_HOME"] == str(self.codex_home)
        tail = command[1:]
        if tail == ("plugin", "list", "--json"):
            source = (
                str(self.source.relative_to(self.source.parent))
                if self.relative_source
                else str(self.source)
            )
            return self._result(
                command,
                {
                    "installed": [
                        {
                            "pluginId": f"{self.plugin}@{self.marketplace}",
                            "name": self.plugin,
                            "version": self.installed_version,
                            "source": {"source": "local", "path": source},
                            "marketplaceSource": {
                                "sourceType": self.source_type,
                                "source": str(self.source.parent.parent),
                            },
                        }
                    ],
                    "available": [],
                },
            )
        if tail == (
            "plugin",
            "marketplace",
            "upgrade",
            self.marketplace,
            "--json",
        ):
            if self.source_type != "git" or self.upgraded_source is None:
                raise AssertionError("unexpected marketplace upgrade")
            self.source = self.upgraded_source
            return self._result(
                command,
                {"marketplaceName": self.marketplace, "status": "upgraded"},
            )
        if tail == (
            "plugin",
            "add",
            f"{self.plugin}@{self.marketplace}",
            "--json",
        ):
            if callable(self.before_add):
                self.before_add()
            shutil.rmtree(self.cache_root)
            if self.fail_add_after_cache_delete:
                return self._result(command, {}, returncode=2, stderr="fixture add failed")
            manifest = json.loads(
                (self.source / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            version = manifest["version"]
            destination = self.cache_root / version
            destination.parent.mkdir(parents=True)
            shutil.copytree(self.source, destination)
            if self.omit_hook_after_add:
                (destination / "scripts" / "wb_hook.py").unlink()
            if self.omit_skill_after_add:
                (destination / "skills" / "WB" / "SKILL.md").unlink()
            self.installed_version = (
                "9.9.9" if self.report_missing_version_after_add else version
            )
            return self._result(
                command,
                {
                    "pluginId": f"{self.plugin}@{self.marketplace}",
                    "name": self.plugin,
                    "version": version,
                    "installedPath": str(destination),
                },
            )
        raise AssertionError(f"unexpected command: {command}")


class PluginCacheUpgradeTests(unittest.TestCase):
    marketplace = "fixture-marketplace"
    plugin = "fixture-plugin"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="plugin-cache-upgrade-",
            dir=PHYSICAL_TMP,
        )
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "home" / ".codex"
        self.codex_home.mkdir(parents=True)
        self.source = self.root / "source" / "plugins" / self.plugin
        self._write_plugin(self.source, "2.0.0", b"new hook\n", mode=0o751)
        self.cache_root = (
            self.codex_home
            / "plugins"
            / "cache"
            / self.marketplace
            / self.plugin
        )
        self.old_cache = self.cache_root / "1.0.0"
        self._write_plugin(self.old_cache, "1.0.0", b"old hook\n", mode=0o741)
        self.trust_sentinel = self.codex_home / "hook-trust-sentinel.json"
        self.trust_sentinel.write_bytes(b'{"trusted":"unchanged"}\n')
        self.runner = FakeCodex(
            codex_home=self.codex_home,
            source=self.source,
            marketplace=self.marketplace,
            plugin=self.plugin,
            installed_version="1.0.0",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_plugin(
        self,
        root: Path,
        version: str,
        hook: bytes,
        *,
        mode: int,
    ) -> None:
        (root / ".codex-plugin").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "hooks").mkdir()
        (root / "skills" / "WB").mkdir(parents=True)
        (root / "data" / "__pycache__").mkdir(parents=True)
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": self.plugin,
                    "version": version,
                    "description": "fixture",
                    "hooks": "./hooks/hooks.json",
                    "skills": "./skills/",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        hook_path = root / "scripts" / "wb_hook.py"
        hook_path.write_bytes(hook)
        hook_path.chmod(mode)
        (root / "hooks" / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "$PLUGIN_ROOT/scripts/wb_hook.py",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (root / "skills" / "WB" / "SKILL.md").write_text(
            "# Fixture skill\n",
            encoding="utf-8",
        )
        (root / "data" / "payload.bin").write_bytes(b"\x00fixture\xff")
        (root / "data" / "__pycache__" / "hook.pyc").write_bytes(b"bytecode")
        root.chmod(0o750)

    def _snapshot(self, root: Path) -> dict[str, tuple[str, int, bytes | None]]:
        result: dict[str, tuple[str, int, bytes | None]] = {}
        paths = [root, *sorted(root.rglob("*"))]
        for path in paths:
            relative = "." if path == root else path.relative_to(root).as_posix()
            if path.is_dir():
                result[relative] = ("directory", path.stat().st_mode & 0o777, None)
            else:
                result[relative] = (
                    "file",
                    path.stat().st_mode & 0o777,
                    path.read_bytes(),
                )
        return result

    def _pending(self) -> Path:
        plan = upgrade.inspect_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            runner=self.runner,
        )
        return upgrade._snapshot_cache(plan)

    def _kill_upgrade_child(self, mode: str) -> subprocess.Popen[str]:
        marker = self.root / f"{mode}-reached"
        helper = self.root / f"{mode}-upgrade.py"
        helper.write_text(
            """
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from codex_workbench import plugin_cache_upgrade as upgrade

home = Path(os.environ["FIXTURE_CODEX_HOME"])
source = Path(os.environ["FIXTURE_SOURCE"])
marketplace = os.environ["FIXTURE_MARKETPLACE"]
plugin = os.environ["FIXTURE_PLUGIN"]
marker = Path(os.environ["FIXTURE_MARKER"])
mode = os.environ["FIXTURE_MODE"]
cache_root = home / "plugins" / "cache" / marketplace / plugin

def runner(command, **kwargs):
    tail = tuple(command[1:])
    if tail == ("plugin", "list", "--json"):
        payload = {
            "installed": [{
                "pluginId": f"{plugin}@{marketplace}",
                "name": plugin,
                "version": "1.0.0",
                "source": {"source": "local", "path": str(source)},
                "marketplaceSource": {"sourceType": "local", "source": str(source)},
            }],
            "available": [],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
    if tail == ("plugin", "add", f"{plugin}@{marketplace}", "--json"):
        shutil.rmtree(cache_root)
        marker.write_text("native-cache-deleted", encoding="utf-8")
        while True:
            time.sleep(1)
    raise AssertionError(command)

if mode == "prepublish":
    original_copy = upgrade._copy_plugin_tree
    def interrupted_copy(copy_source, destination):
        original_copy(copy_source, destination)
        marker.write_text("staging-created", encoding="utf-8")
        while True:
            time.sleep(1)
    upgrade._copy_plugin_tree = interrupted_copy

upgrade.safe_upgrade(
    codex_home=home,
    codex_binary="codex",
    marketplace=marketplace,
    plugin=plugin,
    apply=True,
    confirm_active_hook_cache_retention=True,
    confirm_host_stopped=True,
    runner=runner,
)
""".lstrip(),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(Path(upgrade.__file__).resolve().parents[1]),
                "FIXTURE_CODEX_HOME": str(self.codex_home),
                "FIXTURE_SOURCE": str(self.source),
                "FIXTURE_MARKETPLACE": self.marketplace,
                "FIXTURE_PLUGIN": self.plugin,
                "FIXTURE_MARKER": str(marker),
                "FIXTURE_MODE": mode,
                "HOME": str(self.root / "home"),
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(helper)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"upgrade child exited before {mode}: {stdout}\n{stderr}"
                )
            time.sleep(0.02)
        if not marker.exists():
            process.kill()
            stdout, stderr = process.communicate()
            self.fail(f"upgrade child never reached {mode}: {stdout}\n{stderr}")
        process.kill()
        process.communicate(timeout=10)
        self.assertLess(process.returncode, 0)
        return process

    def test_dry_run_is_read_only(self) -> None:
        before = self._snapshot(self.old_cache)

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            runner=self.runner,
        )

        self.assertEqual(result["mode"], "dry-run")
        self.assertTrue(result["upgrade_required"])
        self.assertFalse(result["online_zero_downtime"])
        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertFalse((self.codex_home / "plugin-cache-retention").exists())
        self.assertEqual(self.runner.commands, [("codex", "plugin", "list", "--json")])

    def test_apply_requires_explicit_context_flow_confirmation(self) -> None:
        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "confirm-active-hook-cache-retention",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                runner=self.runner,
            )

        self.assertEqual(self.runner.commands, [])
        self.assertFalse((self.codex_home / "plugin-cache-retention").exists())

    def test_apply_requires_host_stopped_confirmation(self) -> None:
        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "confirm-host-stopped",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                runner=self.runner,
            )

        self.assertEqual(self.runner.commands, [])
        self.assertFalse((self.codex_home / "plugin-cache-retention").exists())

    def test_recover_only_requires_apply(self) -> None:
        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "recover-only requires --apply",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                recover_only=True,
                runner=self.runner,
            )

        self.assertEqual(self.runner.commands, [])

    def test_upgrade_restores_every_old_byte_and_mode_and_keeps_new_version(self) -> None:
        older = self.cache_root / "0.9.0"
        self._write_plugin(older, "0.9.0", b"older hook\n", mode=0o711)
        before = {
            "0.9.0": self._snapshot(older),
            "1.0.0": self._snapshot(self.old_cache),
        }
        trust_before = self.trust_sentinel.read_bytes()
        task_state = self.root / "state.sqlite"
        task_state.write_bytes(b"task-state-unchanged")

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "upgraded")
        self.assertEqual(result["installed_version"], "2.0.0")
        self.assertEqual(result["retained_versions"], ["0.9.0", "1.0.0"])
        self.assertEqual(self._snapshot(self.cache_root / "0.9.0"), before["0.9.0"])
        self.assertEqual(self._snapshot(self.cache_root / "1.0.0"), before["1.0.0"])
        self.assertEqual(
            (self.cache_root / "2.0.0" / "scripts" / "wb_hook.py").read_bytes(),
            b"new hook\n",
        )
        self.assertEqual(self.trust_sentinel.read_bytes(), trust_before)
        self.assertEqual(task_state.read_bytes(), b"task-state-unchanged")
        self.assertFalse(
            self.codex_home.joinpath(
                "plugin-cache-retention", self.marketplace, self.plugin, "pending"
            ).exists()
        )
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "upgraded")
        self.assertEqual(receipt["installed_version"], "1.0.0")
        self.assertEqual(receipt["final_installed_version"], "2.0.0")
        retained = Path(receipt["retained_cache"]) / "versions"
        self.assertEqual(self._snapshot(retained / "0.9.0"), before["0.9.0"])
        self.assertEqual(self._snapshot(retained / "1.0.0"), before["1.0.0"])
        flattened = " ".join(" ".join(command) for command in self.runner.commands)
        self.assertNotIn(" remove ", f" {flattened} ")
        self.assertNotIn("hook", flattened)
        self.assertEqual(
            self.runner.commands.count(("codex", "plugin", "list", "--json")),
            3,
        )
        self.assertFalse(result["mutates_trust"])
        self.assertFalse(result["prunes_old_cache"])

    def test_pending_snapshot_is_synced_before_native_add(self) -> None:
        sync_calls: list[int] = []
        real_fsync = os.fsync

        def record_fsync(descriptor: int) -> None:
            sync_calls.append(descriptor)
            real_fsync(descriptor)

        def assert_durable_pending() -> None:
            pending = (
                self.codex_home
                / "plugin-cache-retention"
                / self.marketplace
                / self.plugin
                / "pending"
            )
            self.assertTrue(pending.is_dir())
            self.assertGreater(len(sync_calls), 0)

        self.runner.before_add = assert_durable_pending
        with patch.object(upgrade.os, "fsync", side_effect=record_fsync):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertGreater(len(sync_calls), 0)

    def test_native_add_failure_restores_old_cache(self) -> None:
        before = self._snapshot(self.old_cache)
        self.runner.fail_add_after_cache_delete = True

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "fixture add failed"):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self._snapshot(self.old_cache), before)
        receipt_root = self.codex_home / "plugin-cache-retention" / self.marketplace / self.plugin
        self.assertFalse((receipt_root / "pending").exists())
        receipts = list((receipt_root / "receipts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "upgrade_failed_cache_restored")

    def test_interrupted_pending_recovers_across_invocations(self) -> None:
        before = self._snapshot(self.old_cache)
        pending = self._pending()
        receipt = upgrade._pending_receipt(
            pending,
            codex_home=self.codex_home,
            marketplace=self.marketplace,
            plugin=self.plugin,
        )
        upgrade._set_pending_target(pending, receipt, "2.0.0", self.source)
        shutil.rmtree(self.cache_root)
        partial = pending / "restore-staging" / "1.0.0"
        partial.mkdir(parents=True)
        (partial / "partial").write_text("partial", encoding="utf-8")

        dry_run = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            runner=self.runner,
        )
        self.assertTrue(dry_run["pending_recovery"])
        self.assertEqual(dry_run["retained_versions"], ["1.0.0"])

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            recover_only=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertFalse(pending.exists())

    def test_sigkill_after_native_cache_delete_recovers_in_new_process_path(self) -> None:
        before = self._snapshot(self.old_cache)
        self._kill_upgrade_child("after-delete")
        pending = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
            / "pending"
        )
        self.assertTrue(pending.is_dir())
        self.assertFalse(self.cache_root.exists())

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            recover_only=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertFalse(pending.exists())

    def test_sigkill_before_pending_publish_reports_residue_without_cache_loss(self) -> None:
        before = self._snapshot(self.old_cache)
        self._kill_upgrade_child("prepublish")
        retention_root = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
        )
        self.assertFalse((retention_root / "pending").exists())
        residues = list(retention_root.glob(".pending-*"))
        self.assertEqual(len(residues), 1)
        self.assertEqual(self._snapshot(self.old_cache), before)

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "pre-publish plugin staging residue found",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                runner=self.runner,
            )

        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertTrue(residues[0].is_dir())
        self.assertEqual(self.runner.commands, [])

    def test_recovery_repairs_new_registry_version_with_missing_cache(self) -> None:
        before = self._snapshot(self.old_cache)
        pending = self._pending()
        receipt = upgrade._pending_receipt(
            pending,
            codex_home=self.codex_home,
            marketplace=self.marketplace,
            plugin=self.plugin,
        )
        upgrade._set_pending_target(pending, receipt, "2.0.0", self.source)
        shutil.rmtree(self.cache_root)
        self.runner.installed_version = "2.0.0"

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            recover_only=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertTrue((self.cache_root / "2.0.0").is_dir())
        self.assertFalse(pending.exists())

    def test_failed_repair_add_restores_old_cache_and_keeps_pending(self) -> None:
        before = self._snapshot(self.old_cache)
        pending = self._pending()
        receipt = upgrade._pending_receipt(
            pending,
            codex_home=self.codex_home,
            marketplace=self.marketplace,
            plugin=self.plugin,
        )
        upgrade._set_pending_target(pending, receipt, "2.0.0", self.source)
        shutil.rmtree(self.cache_root)
        self.runner.installed_version = "2.0.0"
        self.runner.fail_add_after_cache_delete = True

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "fixture add failed.*pending recovery remains",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                recover_only=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self._snapshot(self.old_cache), before)
        self.assertTrue(pending.is_dir())
        self.assertFalse((self.cache_root / "2.0.0").exists())

    def test_post_install_list_mismatch_keeps_pending_recovery(self) -> None:
        self.runner.report_missing_version_after_add = True

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "pending receipt retained",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        pending = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
            / "pending"
        )
        self.assertTrue(pending.is_dir())
        self.assertTrue(self.old_cache.is_dir())

    def test_native_success_with_missing_hook_is_rejected_and_keeps_recovery(self) -> None:
        before = self._snapshot(self.old_cache)
        self.runner.omit_hook_after_add = True

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "Hook PLUGIN_ROOT reference is missing.*pending receipt retained",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self._snapshot(self.old_cache), before)
        pending = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
            / "pending"
        )
        self.assertTrue(pending.is_dir())

    def test_native_success_with_missing_skill_is_rejected_and_keeps_recovery(self) -> None:
        before = self._snapshot(self.old_cache)
        self.runner.omit_skill_after_add = True

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "plugin skills directory has no SKILL.md.*pending receipt retained",
        ):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self._snapshot(self.old_cache), before)
        pending = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
            / "pending"
        )
        self.assertTrue(pending.is_dir())

    def test_invalid_pending_version_is_rejected_before_cache_write(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        receipt_path = pending / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["cached_versions"][0]["version"] = "../escape"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "safe path component"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )

        self.assertFalse(self.cache_root.exists())
        self.assertFalse((pending.parent / "escape").exists())

    def test_duplicate_or_non_hex_pending_entries_are_rejected_before_write(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        receipt_path = pending / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["cached_versions"].append(dict(receipt["cached_versions"][0]))
        receipt["cached_versions"][0]["digest"] = "z" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "receipt is invalid"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )

        self.assertFalse(self.cache_root.exists())

    def test_pending_target_version_and_source_path_must_be_recorded_together(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        receipt_path = pending / "receipt.json"
        original = json.loads(receipt_path.read_text(encoding="utf-8"))

        for target_version, target_source_path in (
            (None, original["target_source_path"]),
            (original["target_version"], None),
        ):
            with self.subTest(
                target_version=target_version,
                target_source_path=target_source_path,
            ):
                damaged = json.loads(json.dumps(original))
                damaged["target_version"] = target_version
                damaged["target_source_path"] = target_source_path
                receipt_path.write_text(json.dumps(damaged), encoding="utf-8")
                with self.assertRaisesRegex(
                    upgrade.PluginCacheUpgradeError,
                    "must be recorded together",
                ):
                    upgrade.restore_pending_cache(
                        codex_home=self.codex_home,
                        marketplace=self.marketplace,
                        plugin=self.plugin,
                    )
                self.assertFalse(self.cache_root.exists())
                self.assertTrue(pending.is_dir())

    def test_pending_receipt_symlink_is_rejected_before_external_read_or_write(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        receipt_path = pending / "receipt.json"
        receipt_path.unlink()
        outside = self.root / "outside-receipt.json"
        outside.write_text('{"private":"must-not-be-read"}\n', encoding="utf-8")
        receipt_path.symlink_to(outside)

        with self.assertRaisesRegex(
            upgrade.PluginCacheUpgradeError,
            "pending plugin cache receipt has a symlink ancestor",
        ) as captured:
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )

        self.assertNotIn("must-not-be-read", str(captured.exception))
        self.assertFalse(self.cache_root.exists())
        self.assertEqual(
            outside.read_text(encoding="utf-8"),
            '{"private":"must-not-be-read"}\n',
        )

    def test_symlink_cache_entry_and_relative_source_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.write_text("private", encoding="utf-8")
        (self.old_cache / "linked").symlink_to(outside)
        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "contains a symlink"):
            upgrade.inspect_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                runner=self.runner,
            )
        (self.old_cache / "linked").unlink()
        self.runner.relative_source = True
        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "must be absolute"):
            upgrade.inspect_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                runner=self.runner,
            )

    def test_cache_root_symlink_does_not_write_outside_scope(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        outside = self.root / "outside-cache"
        outside.mkdir()
        self.cache_root.parent.mkdir(parents=True, exist_ok=True)
        self.cache_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "symlink ancestor"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(pending.is_dir())

    def test_restore_staging_symlinks_and_files_fail_before_cache_write(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        restore_root = pending / "restore-staging"
        outside = self.root / "outside-restore"
        outside.mkdir()
        restore_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "symlink ancestor"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )
        self.assertFalse(self.cache_root.exists())
        self.assertEqual(list(outside.iterdir()), [])

        restore_root.unlink()
        missing = self.root / "missing-restore-target"
        restore_root.symlink_to(missing, target_is_directory=True)
        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "symlink ancestor"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )
        self.assertFalse(self.cache_root.exists())
        self.assertFalse(missing.exists())

        restore_root.unlink()
        restore_root.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "not a directory"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )
        self.assertFalse(self.cache_root.exists())

    def test_receipts_symlink_fails_before_native_commands(self) -> None:
        retention_root = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
        )
        retention_root.mkdir(parents=True)
        outside = self.root / "outside-retention"
        outside.mkdir()
        (retention_root / "receipts").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "symlink ancestor"):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self.runner.commands, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_pending_symlink_fails_before_native_commands(self) -> None:
        retention_root = (
            self.codex_home
            / "plugin-cache-retention"
            / self.marketplace
            / self.plugin
        )
        retention_root.mkdir(parents=True)
        outside = self.root / "outside-pending"
        outside.mkdir()
        (retention_root / "pending").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "symlink ancestor"):
            upgrade.safe_upgrade(
                codex_home=self.codex_home,
                codex_binary="codex",
                marketplace=self.marketplace,
                plugin=self.plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
                runner=self.runner,
            )

        self.assertEqual(self.runner.commands, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflicting_restored_version_keeps_pending(self) -> None:
        pending = self._pending()
        shutil.rmtree(self.cache_root)
        self._write_plugin(self.old_cache, "1.0.0", b"conflict\n", mode=0o700)

        with self.assertRaisesRegex(upgrade.PluginCacheUpgradeError, "conflicts"):
            upgrade.restore_pending_cache(
                codex_home=self.codex_home,
                marketplace=self.marketplace,
                plugin=self.plugin,
            )

        self.assertTrue(pending.is_dir())
        self.assertEqual(
            (self.old_cache / "scripts" / "wb_hook.py").read_bytes(),
            b"conflict\n",
        )

    def test_concurrent_apply_cannot_claim_another_process_pending(self) -> None:
        lock_path = upgrade._lock_path(
            self.codex_home,
            marketplace=self.marketplace,
            plugin=self.plugin,
        )
        lock_path.parent.mkdir(parents=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                upgrade.PluginCacheUpgradeError,
                "another plugin cache upgrade is active",
            ):
                upgrade.safe_upgrade(
                    codex_home=self.codex_home,
                    codex_binary="codex",
                    marketplace=self.marketplace,
                    plugin=self.plugin,
                    apply=True,
                    confirm_active_hook_cache_retention=True,
                    confirm_host_stopped=True,
                    runner=self.runner,
                )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertEqual(self.runner.commands, [])

    def test_same_local_version_does_not_call_native_add(self) -> None:
        shutil.rmtree(self.source)
        self._write_plugin(self.source, "1.0.0", b"same hook\n", mode=0o741)

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "already_current")
        self.assertIsNone(result["receipt"])
        self.assertEqual(self.runner.commands, [("codex", "plugin", "list", "--json")])

    def test_git_marketplace_refresh_rediscovers_source_before_install(self) -> None:
        old_source = self.root / "git-old" / "plugins" / self.plugin
        new_source = self.root / "git-new" / "plugins" / self.plugin
        self._write_plugin(old_source, "1.0.0", b"source old\n", mode=0o741)
        self._write_plugin(new_source, "2.0.0", b"source new\n", mode=0o751)
        old_cache_before = self._snapshot(self.old_cache)
        self.runner.source = old_source
        self.runner.source_type = "git"
        self.runner.upgraded_source = new_source

        result = upgrade.safe_upgrade(
            codex_home=self.codex_home,
            codex_binary="codex",
            marketplace=self.marketplace,
            plugin=self.plugin,
            apply=True,
            confirm_active_hook_cache_retention=True,
            confirm_host_stopped=True,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "upgraded")
        self.assertEqual(self._snapshot(self.old_cache), old_cache_before)
        self.assertEqual(
            (self.cache_root / "2.0.0" / "scripts" / "wb_hook.py").read_bytes(),
            b"source new\n",
        )
        self.assertIn(
            (
                "codex",
                "plugin",
                "marketplace",
                "upgrade",
                self.marketplace,
                "--json",
            ),
            self.runner.commands,
        )
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["target_version"], "2.0.0")
        self.assertEqual(receipt["target_source_path"], str(new_source))

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is unavailable")
    def test_real_codex_cli_upgrade_retains_old_hook_without_running_it(self) -> None:
        codex = shutil.which("codex")
        assert codex is not None
        home = self.root / "real-home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        marketplace_root = self.root / "real-marketplace"
        marketplace = "retention-fixture"
        plugin = "retention-fixture"
        plugin_root = marketplace_root / "plugins" / plugin
        marker = self.root / "hook-ran"

        def write_real_plugin(version: str, body: str) -> None:
            if plugin_root.exists():
                shutil.rmtree(plugin_root)
            (plugin_root / ".codex-plugin").mkdir(parents=True)
            (plugin_root / "hooks").mkdir()
            (plugin_root / "scripts").mkdir()
            (plugin_root / "skills" / "WB").mkdir(parents=True)
            (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": plugin,
                        "version": version,
                        "description": "retention fixture",
                        "hooks": "./hooks/hooks.json",
                        "skills": "./skills/",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "hooks" / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f"/usr/bin/python3 $PLUGIN_ROOT/scripts/hook.py "
                                                f"{marker}"
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "scripts" / "hook.py").write_text(body, encoding="utf-8")
            (plugin_root / "skills" / "WB" / "SKILL.md").write_text(
                "# Fixture skill\n",
                encoding="utf-8",
            )

        write_real_plugin(
            "1.0.0",
            "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ran')\n",
        )
        (marketplace_root / ".agents" / "plugins").mkdir(parents=True)
        (marketplace_root / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": marketplace,
                    "plugins": [
                        {
                            "name": plugin,
                            "source": {"source": "local", "path": f"./plugins/{plugin}"},
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
        add_marketplace = subprocess.run(
            [codex, "plugin", "marketplace", "add", str(marketplace_root), "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(add_marketplace.returncode, 0, add_marketplace.stderr)
        install = subprocess.run(
            [codex, "plugin", "add", f"{plugin}@{marketplace}", "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        old_path = codex_home / "plugins" / "cache" / marketplace / plugin / "1.0.0"
        old_before = self._snapshot(old_path)
        trust_sentinel = codex_home / "trust-sentinel"
        trust_sentinel.write_text("unchanged", encoding="utf-8")
        config_before = (codex_home / "config.toml").read_bytes()
        write_real_plugin(
            "2.0.0",
            "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('new-ran')\n",
        )

        with patch.dict(os.environ, {"HOME": str(home)}):
            result = upgrade.safe_upgrade(
                codex_home=codex_home,
                codex_binary=codex,
                marketplace=marketplace,
                plugin=plugin,
                apply=True,
                confirm_active_hook_cache_retention=True,
                confirm_host_stopped=True,
            )

        self.assertEqual(result["status"], "upgraded")
        self.assertEqual(self._snapshot(old_path), old_before)
        self.assertTrue(
            codex_home.joinpath(
                "plugins", "cache", marketplace, plugin, "2.0.0", "scripts", "hook.py"
            ).is_file()
        )
        self.assertFalse(marker.exists())
        self.assertEqual(trust_sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual((codex_home / "config.toml").read_bytes(), config_before)
        old_entry = subprocess.run(
            ["/usr/bin/python3", str(old_path / "scripts" / "hook.py"), str(marker)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(old_entry.returncode, 0, old_entry.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "ran")


if __name__ == "__main__":
    unittest.main()
