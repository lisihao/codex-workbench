from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
import importlib.util
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


PHYSICAL_TMP = Path(tempfile.gettempdir()).resolve()


class InstallerTests(unittest.TestCase):
    @staticmethod
    def _macbook_installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_macbook_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _macos_installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_macos_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _harness_installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-code-as-harness.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_harness_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fake_runtime(self, directory: Path, name: str, probe_exit: int) -> Path:
        runtime = directory / name
        runtime.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            f"  exit {probe_exit}\n"
            "fi\n"
            "printf 'selected-runtime=%s args=%s\\n' \"$0\" \"$*\"\n"
        )
        runtime.chmod(0o755)
        return runtime

    def _run_selector(self, runtime: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["CODEX_WORKBENCH_PYTHON"] = str(runtime)
        selector = Path(__file__).resolve().parents[1] / "scripts" / "python-runtime"
        return subprocess.run(
            [str(selector), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_python39_runtime_is_rejected_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            runtime = self._fake_runtime(Path(directory), "python3.9", 1)
            result = self._run_selector(runtime)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires Python 3.11 or newer", result.stderr)
        self.assertIn("CODEX_WORKBENCH_PYTHON", result.stderr)

    def test_python311_runtime_is_accepted_with_spaces_in_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex workbench ", dir=PHYSICAL_TMP) as directory:
            runtime = self._fake_runtime(Path(directory), "python 3.11", 0)
            result = self._run_selector(runtime, "-m", "codex_workbench", "marker")

        self.assertEqual(result.returncode, 0)
        self.assertIn("python 3.11", result.stdout)
        self.assertIn("marker", result.stdout)

    def test_python314_runtime_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            runtime = self._fake_runtime(Path(directory), "python3.14", 0)
            result = self._run_selector(runtime, "--version")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--version", result.stdout)

    def test_macos_installer_generated_wrapper_uses_runtime_selector(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scripts" / "install-macos.py").read_text()
        self.assertIn('app_root / "scripts" / "python-runtime"', source)
        self.assertNotIn("exec /opt/homebrew/bin/python3 -m codex_workbench", source)
        self.assertIn("CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE", source)
        self.assertIn('"authority_machine_id": authority_machine_id', source)
        self.assertIn('"--claude-binary"', source)
        self.assertNotIn("CODEX_WORKBENCH_CLAUDE=/opt/homebrew/bin/claude", source)
        self.assertIn('default="~/.agents/skills/research"', source)

    def test_macos_installer_copies_only_managed_research_skill(self) -> None:
        module = self._macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            source = root / "source"
            process_home = root / "process-home"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            unrelated = process_home / ".agents" / "skills" / "unrelated" / "SKILL.md"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep")

            destination = module.install_research_skill(source, process_home)

            self.assertEqual(destination, process_home / ".agents" / "skills" / "research")
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                self.assertTrue((destination / relative).is_file())
            self.assertEqual(unrelated.read_text(), "keep")

    def test_authority_installer_raises_existing_capacity_to_eight_without_lowering_custom_capacity(self) -> None:
        module = self._macos_installer_module()
        self.assertEqual(module.authority_max_workers({}), 8)
        self.assertEqual(module.authority_max_workers({"max_workers": 4}), 8)
        self.assertEqual(module.authority_max_workers({"max_workers": 12}), 12)

    def test_authority_installer_persists_a_valid_spark_lane_and_performance_refresh_contract(self) -> None:
        module = self._macos_installer_module()
        self.assertEqual(module.authority_spark_workers({}, max_workers=8), 4)
        self.assertEqual(module.authority_spark_workers({"spark_workers": 0}, max_workers=8), 0)
        self.assertEqual(module.authority_spark_workers({"spark_workers": 3}, max_workers=8), 3)
        with self.assertRaisesRegex(SystemExit, "spark_workers must be between"):
            module.authority_spark_workers({"spark_workers": 9}, max_workers=8)

        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            state_root = root / "state"
            app_root = state_root / "app"
            existing = {
                "user_setting": "preserve",
                "performance": {"operator_note": "preserve"},
            }
            performance = module.performance_installation_config(
                existing,
                app_root=app_root,
                state_root=state_root,
                refresh_seconds=1234,
            )

        self.assertEqual(performance["operator_note"], "preserve")
        self.assertEqual(performance["state_root"], str(state_root / "performance"))
        self.assertEqual(
            performance["baseline_resource"],
            module.PERFORMANCE_BASELINE_RESOURCE,
        )
        self.assertEqual(performance["refresh_interval_seconds"], 1234)
        self.assertEqual(
            performance["refresh_command"],
            [
                str(app_root / "scripts" / "python-runtime"),
                "-m",
                "codex_workbench",
                "--home",
                str(state_root),
                "capabilities",
                "refresh",
                "--activate-safe",
            ],
        )

    def test_authority_installer_persists_radar_contract_without_overwriting_unknown_fields(self) -> None:
        module = self._macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            state_root = root / "state"
            app_root = state_root / "app"
            config = {
                "user_setting": "preserve",
                "radar": {"operator_note": "preserve", "upstream": {"local_note": "preserve"}},
            }
            radar = module.radar_installation_config(
                config,
                app_root=app_root,
                state_root=state_root,
                refresh_seconds=1234,
                upstream={
                    "repository": "https://github.com/WineChord/codex-radar",
                    "tag": "v0.1.69",
                    "commit": "fixture-radar-commit",
                    "attribution": "数据来自 Codex 雷达 codexradar.com",
                },
            )

        self.assertEqual(config["user_setting"], "preserve")
        self.assertEqual(radar["operator_note"], "preserve")
        self.assertEqual(radar["upstream"]["local_note"], "preserve")
        self.assertEqual(radar["producer"], module.RADAR_PRODUCER)
        self.assertEqual(
            radar["upstream"],
            {
                "local_note": "preserve",
                "repository": "https://github.com/WineChord/codex-radar",
                "tag": "v0.1.69",
                "commit": "fixture-radar-commit",
            },
        )
        self.assertEqual(radar["state_root"], str(state_root / "radar"))
        self.assertEqual(
            radar["authorization_receipt"],
            str(state_root / "radar" / "authorization.json"),
        )
        self.assertEqual(radar["refresh_interval_seconds"], 1234)
        self.assertEqual(radar["attribution"], "数据来自 Codex 雷达 codexradar.com")
        self.assertEqual(
            radar["refresh_command"],
            [
                str(app_root / "scripts" / "python-runtime"),
                "-m",
                "codex_workbench",
                "--home",
                str(state_root),
                "radar",
                "refresh",
            ],
        )
        self.assertTrue(radar["authority_only"])

    def test_authority_service_render_uses_real_home_and_explicit_claude_path(self) -> None:
        module = self._macos_installer_module()
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / f"{module.LABEL}.plist.in").read_text()
        with mock.patch.object(module.Path, "home", return_value=Path("/Users/example")):
            rendered = module.render_authority_service_plist(
                template,
                app_root=Path("/tmp/app"),
                state_root=Path("/tmp/state"),
                codex_binary=Path("/tmp/runtime/codex"),
                codex_home=Path("/tmp/state/codex-home"),
                process_home=Path("/tmp/state/process-home"),
                quota_snapshot_file=Path("/tmp/state/claude-quota.json"),
                claude_binary=Path("/tmp/claude-2.1.239"),
            )

        payload = plistlib.loads(rendered.encode())
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["HOME"], "/Users/example")
        self.assertEqual(environment["CODEX_WORKBENCH_CLAUDE"], "/tmp/claude-2.1.239")
        self.assertEqual(environment["CODEX_WORKBENCH_CODEX"], "/tmp/runtime/codex")

    def test_harness_installer_is_idempotent_and_preserves_existing_policy(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            codex_policy = home / ".codex" / "AGENTS.md"
            claude_policy = home / ".claude" / "CLAUDE.md"
            codex_policy.parent.mkdir(parents=True)
            claude_policy.parent.mkdir(parents=True)
            codex_policy.write_text("# keep Codex policy\n")
            claude_policy.write_text("# keep Claude policy\n")

            first = module.install_code_as_harness(source, home)
            first_codex_policy = codex_policy.read_text()
            first_claude_policy = claude_policy.read_text()
            second = module.install_code_as_harness(source, home)

            self.assertEqual(first, second)
            for agent, paths in first.items():
                skill = Path(paths["skill"])
                policy = Path(paths["policy"])
                self.assertTrue(skill.is_file())
                self.assertIn(module.SKILL_MARKER, skill.read_text())
                self.assertTrue((skill.parent / "references" / "aegis-integration.md").is_file())
                self.assertTrue((skill.parent / "references" / "tier-examples.md").is_file())
                self.assertTrue((skill.parent / "agents" / "openai.yaml").is_file())
                self.assertEqual(policy.read_text().count(module.POLICY_START), 1)
                self.assertEqual(policy.read_text().count(module.POLICY_END), 1)
            self.assertIn("# keep Codex policy", first_codex_policy)
            self.assertIn("# keep Claude policy", first_claude_policy)
            self.assertEqual(codex_policy.read_text(), first_codex_policy)
            self.assertEqual(claude_policy.read_text(), first_claude_policy)

    def test_harness_installer_explicitly_adopts_the_recognized_legacy_skill(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        legacy = (
            "---\nname: code-as-harness\n"
            "description: prior compatible capability\n---\n"
            "# Code as Harness\n"
            "## 1. Classify once\n"
            "## 5. Completion receipt\n"
            "Read [Aegis](references/aegis-integration.md) and "
            "[tiers](references/tier-examples.md).\n"
        )
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            for root in (".codex", ".claude"):
                skill_root = home / root / "skills" / module.SKILL_NAME
                skill_root.mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(legacy)
                (skill_root / "keep-user-note.md").write_text("keep\n")

            with self.assertRaisesRegex(SystemExit, "unmanaged Code-as-Harness skill"):
                module.preflight_code_as_harness(source, home)

            module.install_code_as_harness(source, home, adopt_compatible=True)

            for root in (".codex", ".claude"):
                skill_root = home / root / "skills" / module.SKILL_NAME
                self.assertIn(module.SKILL_MARKER, (skill_root / "SKILL.md").read_text())
                self.assertEqual((skill_root / "keep-user-note.md").read_text(), "keep\n")
                self.assertTrue((skill_root / "agents" / "openai.yaml").is_file())

    def test_harness_installer_refuses_unmanaged_skill_without_touching_policies(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            skill = home / ".codex" / "skills" / module.SKILL_NAME / "SKILL.md"
            policy = home / ".codex" / "AGENTS.md"
            skill.parent.mkdir(parents=True)
            policy.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(
                "---\nname: code-as-harness\n---\n"
                "<!-- codex_workbench_managed: true -->\nuser-owned\n"
            )
            policy.write_text("# keep\n")

            with self.assertRaisesRegex(SystemExit, "unmanaged Code-as-Harness skill"):
                module.install_code_as_harness(source, home)

            self.assertEqual(policy.read_text(), "# keep\n")
            self.assertFalse((home / ".claude" / "skills" / module.SKILL_NAME / "SKILL.md").exists())

    def test_harness_installer_preserves_policy_text_that_is_not_a_managed_block(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            policy = home / ".codex" / "AGENTS.md"
            policy.parent.mkdir(parents=True)
            original = f"# Keep this literal: {module.POLICY_START}\n"
            policy.write_text(original)

            module.install_code_as_harness(source, home)

            self.assertTrue(policy.read_text().startswith(original))
            self.assertEqual(policy.read_text().count(module.POLICY_START), 2)
            self.assertEqual(policy.read_text().count(module.POLICY_END), 1)

    def test_macbook_installer_preflights_codex_before_harness_write(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with mock.patch.object(module, "run") as run, mock.patch.object(
            module.shutil, "which", return_value=None
        ), mock.patch.object(module, "install_code_as_harness") as install, mock.patch.object(
            module.sys,
            "argv",
            ["install-macbook-client.py", "--source", str(source), "--ssh-transport", "system"],
        ):
            run.return_value = subprocess.CompletedProcess(
                ["fixture"],
                0,
                stdout="501\n",
                stderr="",
            )
            with self.assertRaisesRegex(SystemExit, "Codex CLI is required"):
                module.main()

        install.assert_not_called()

    def test_macos_installer_preflights_codex_before_harness_write(self) -> None:
        module = self._macos_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            research = root / "research"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = research / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            state_root = root / "state"
            missing_codex = root / "missing-codex"
            with mock.patch.object(module, "macos_machine_id", return_value="fixture-machine"), mock.patch.object(
                module, "install_code_as_harness"
            ) as install, mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macos.py",
                    "--source",
                    str(source),
                    "--state-root",
                    str(state_root),
                    "--research-skill-source",
                    str(research),
                    "--codex-binary",
                    str(missing_codex),
                ],
            ):
                with self.assertRaises(FileNotFoundError):
                    module.main()

            install.assert_not_called()
            self.assertFalse(state_root.exists())

    def test_harness_installer_refuses_an_out_of_order_policy_block(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            policy = home / ".codex" / "AGENTS.md"
            policy.parent.mkdir(parents=True)
            original = f"{module.POLICY_END}\nuser content\n{module.POLICY_START}\n"
            policy.write_text(original)

            with self.assertRaisesRegex(SystemExit, "out-of-order"):
                module.install_code_as_harness(source, home)

            self.assertEqual(policy.read_text(), original)

    def test_harness_installer_preflights_both_agent_targets_before_writing(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            (home / ".claude").write_text("not a directory")

            with self.assertRaisesRegex(SystemExit, "Target parent is not a directory"):
                module.install_code_as_harness(source, home)

            self.assertFalse(
                (home / ".codex" / "skills" / module.SKILL_NAME / "SKILL.md").exists()
            )
            self.assertFalse((home / ".codex" / "AGENTS.md").exists())

    def test_harness_installer_rejects_live_and_broken_symlink_ancestors(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            destination = root / "destination"
            destination.mkdir()
            for name, link_target in (
                ("live-home", destination),
                ("broken-home", root / "missing-home"),
            ):
                with self.subTest(name=name):
                    home = root / name
                    home.symlink_to(link_target, target_is_directory=True)
                    with self.assertRaisesRegex(SystemExit, "symlink ancestor"):
                        module.preflight_code_as_harness(source, home)
                    self.assertFalse((destination / ".codex" / "AGENTS.md").exists())

    def test_harness_installer_rolls_back_all_files_when_later_swap_fails(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            module.install_code_as_harness(source, home)
            targets = {
                path: path.read_bytes()
                for path in module._transaction_targets(home).values()
            }
            updated_source = root / "updated-source"
            updated_root = updated_source / module.CANONICAL_SKILL_ROOT_RELATIVE_PATH
            shutil.copytree(source / module.CANONICAL_SKILL_ROOT_RELATIVE_PATH, updated_root)
            updated_skill = updated_source / module.CANONICAL_SKILL_RELATIVE_PATH
            updated_skill.write_text(
                (source / module.CANONICAL_SKILL_RELATIVE_PATH).read_text(encoding="utf-8")
                + "\n<!-- transaction-update -->\n",
                encoding="utf-8",
            )
            claude_policy = home / module.TARGETS["claude-code"]["policy"]
            real_replace = module.os.replace

            def fail_late_stage(source_path: object, destination: object) -> object:
                if (
                    Path(source_path).name.startswith(f".{claude_policy.name}.stage-")
                    and Path(destination) == claude_policy
                ):
                    raise OSError("injected late endpoint failure")
                return real_replace(source_path, destination)

            with mock.patch.object(module.os, "replace", side_effect=fail_late_stage):
                with self.assertRaisesRegex(SystemExit, "atomic install failed"):
                    module.install_code_as_harness(updated_source, home)

            for path, before in targets.items():
                self.assertEqual(path.read_bytes(), before)
            leftovers = [
                path
                for path in root.rglob("*")
                if ".stage-" in path.name or ".backup-" in path.name
            ]
            self.assertEqual(leftovers, [])

    def test_harness_installer_recovers_persistent_transaction_after_interrupt(self) -> None:
        module = self._harness_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            module.install_code_as_harness(source, home)
            updated_source = root / "updated-source"
            updated_root = updated_source / module.CANONICAL_SKILL_ROOT_RELATIVE_PATH
            shutil.copytree(source / module.CANONICAL_SKILL_ROOT_RELATIVE_PATH, updated_root)
            updated_skill = updated_source / module.CANONICAL_SKILL_RELATIVE_PATH
            updated_skill.write_text(
                (source / module.CANONICAL_SKILL_RELATIVE_PATH).read_text(encoding="utf-8")
                + "\n<!-- recovered-transaction-update -->\n",
                encoding="utf-8",
            )
            interrupted_target = home / module.TARGETS["claude-code"]["policy"]
            real_replace = module.os.replace

            def interrupt_late_stage(source_path: object, destination: object) -> object:
                if (
                    Path(source_path).name.startswith(f".{interrupted_target.name}.stage-")
                    and Path(destination) == interrupted_target
                ):
                    raise SystemExit("simulated power loss")
                return real_replace(source_path, destination)

            with mock.patch.object(module.os, "replace", side_effect=interrupt_late_stage):
                with self.assertRaisesRegex(SystemExit, "simulated power loss"):
                    module.install_code_as_harness(updated_source, home)

            self.assertTrue((home / module.TRANSACTION_RECORD_FILENAME).is_file())
            module.install_code_as_harness(updated_source, home)
            for agent, paths in module.TARGETS.items():
                skill = home / paths["skill"]
                policy = home / paths["policy"]
                self.assertIn("recovered-transaction-update", skill.read_text(encoding="utf-8"))
                self.assertEqual(policy.read_text(encoding="utf-8").count(module.POLICY_START), 1)
                self.assertEqual(policy.read_text(encoding="utf-8").count(module.POLICY_END), 1)
            self.assertFalse((home / module.TRANSACTION_RECORD_FILENAME).exists())
            leftovers = [
                path
                for path in home.rglob("*")
                if ".stage-" in path.name or ".backup-" in path.name
            ]
            self.assertEqual(leftovers, [])

    def test_device_installers_delegate_to_the_canonical_harness_installer(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("install-macos.py", "install-macbook-client.py"):
            source = (root / "scripts" / name).read_text()
            self.assertIn('"install-code-as-harness.py"', source)
            self.assertIn("install_code_as_harness(source)", source)

    def test_authority_launch_agent_persists_quota_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / "com.lisihao.codex-workbench.plist.in").read_text()
        rendered = (
            template.replace("__APP_ROOT__", "/tmp/app")
            .replace("__STATE_ROOT__", "/tmp/state")
            .replace("__CODEX_BINARY__", "/tmp/codex")
            .replace("__CODEX_HOME__", "/tmp/codex-home")
            .replace("__PROCESS_HOME__", "/tmp/process-home")
            .replace("__QUOTA_SNAPSHOT_FILE__", "/tmp/state/claude-quota.json")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertEqual(
            payload["EnvironmentVariables"]["CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE"],
            "/tmp/state/claude-quota.json",
        )

    def test_quota_launch_agent_stays_alive_in_on_demand_only_gui_domain(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / "com.lisihao.codex-workbench-quota.plist.in").read_text()
        rendered = (
            template.replace("__APP_ROOT__", "/tmp/app")
            .replace("__STATE_ROOT__", "/tmp/state")
            .replace("__USER_HOME__", "/Users/example")
            .replace("__CLAUDE_BINARY__", "/tmp/claude")
            .replace("__QUOTA_SNAPSHOT_FILE__", "/tmp/state/claude-quota.json")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertEqual(payload["Label"], "com.lisihao.codex-workbench-quota")
        self.assertTrue(payload["RunAtLoad"])
        self.assertNotIn("StartInterval", payload)
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(payload["ThrottleInterval"], 10)
        self.assertIn("watch-claude", payload["ProgramArguments"])
        interval_index = payload["ProgramArguments"].index("--interval")
        self.assertEqual(payload["ProgramArguments"][interval_index + 1], "60")
        self.assertIn("/tmp/claude", payload["ProgramArguments"])
        self.assertEqual(payload["EnvironmentVariables"]["HOME"], "/Users/example")
        self.assertEqual(payload["StandardOutPath"], "/tmp/state/logs/quota.log")

    def test_capability_launch_agent_uses_the_installed_runtime_and_passive_refresh(self) -> None:
        module = self._macos_installer_module()
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / f"{module.CAPABILITY_LABEL}.plist.in").read_text()
        rendered = module.render_capability_plist(
            template,
            app_root=Path("/tmp/app"),
            state_root=Path("/tmp/state"),
            codex_binary=Path("/tmp/runtime/codex"),
            codex_home=Path("/tmp/state/codex-home"),
            process_home=Path("/tmp/state/codex-process-home"),
            quota_snapshot_file=Path("/tmp/state/claude-quota.json"),
            claude_binary=Path("/tmp/claude"),
            refresh_seconds=module.DEFAULT_CAPABILITY_REFRESH_SECONDS,
        )

        payload = plistlib.loads(rendered.encode())
        arguments = payload["ProgramArguments"]
        environment = payload["EnvironmentVariables"]
        self.assertEqual(payload["Label"], module.CAPABILITY_LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 6 * 60 * 60)
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(
            arguments,
            [
                "/tmp/app/scripts/python-runtime",
                "-m",
                "codex_workbench",
                "--home",
                "/tmp/state",
                "capabilities",
                "refresh",
                "--activate-safe",
            ],
        )
        self.assertEqual(environment["CODEX_HOME"], "/tmp/state/codex-home")
        self.assertEqual(environment["CODEX_WORKBENCH_CODEX"], "/tmp/runtime/codex")
        self.assertEqual(environment["CODEX_WORKBENCH_CLAUDE"], "/tmp/claude")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("login", arguments)
        self.assertNotIn("exec", arguments)

    def test_radar_launch_agent_is_authority_only_and_has_no_secret_environment(self) -> None:
        module = self._macos_installer_module()
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / f"{module.RADAR_LABEL}.plist.in").read_text()
        with mock.patch.object(module.Path, "home", return_value=Path("/Users/example")):
            rendered = module.render_radar_plist(
                template,
                app_root=Path("/tmp/app"),
                state_root=Path("/tmp/state"),
                refresh_seconds=module.DEFAULT_RADAR_REFRESH_SECONDS,
            )

        payload = plistlib.loads(rendered.encode())
        self.assertEqual(payload["Label"], module.RADAR_LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 24 * 60 * 60)
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/tmp/app/scripts/python-runtime",
                "-m",
                "codex_workbench",
                "--home",
                "/tmp/state",
                "radar",
                "refresh",
            ],
        )
        self.assertEqual(
            set(payload["EnvironmentVariables"]),
            {"HOME", "PATH", "PYTHONPATH", "PYTHONUNBUFFERED"},
        )
        self.assertNotIn("CODEX_HOME", payload["EnvironmentVariables"])
        self.assertNotIn("CODEX_WORKBENCH_CODEX", payload["EnvironmentVariables"])
        self.assertNotIn("CODEX_WORKBENCH_CLAUDE", payload["EnvironmentVariables"])
        self.assertNotIn("OPENAI_API_KEY", payload["EnvironmentVariables"])
        self.assertNotIn("ANTHROPIC_API_KEY", payload["EnvironmentVariables"])
        self.assertEqual(payload["StandardOutPath"], "/tmp/state/logs/radar.log")
        self.assertEqual(payload["StandardErrorPath"], "/tmp/state/logs/radar.error.log")

    def test_radar_provider_writer_is_not_installed_by_macbook_client(self) -> None:
        root = Path(__file__).resolve().parents[1]
        client_source = (root / "scripts" / "install-macbook-client.py").read_text()
        self.assertNotIn("codex_radar_provider", client_source)
        self.assertNotIn("codex-workbench-radar", client_source)

    def test_initial_capability_refresh_uses_fixture_runtime_without_api_keys_or_model_prompt(self) -> None:
        module = self._macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            app_root = root / "app"
            runtime = app_root / "scripts" / "python-runtime"
            arguments_path = root / "arguments.txt"
            environment_path = root / "environment.txt"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments_path))}\n"
                f"env | /usr/bin/sort > {shlex.quote(str(environment_path))}\n"
                "exit 0\n"
            )
            runtime.chmod(0o755)
            codex = root / "runtime-codex"
            claude = root / "fixture-claude"
            codex.write_text("#!/bin/sh\nexit 99\n")
            claude.write_text("#!/bin/sh\nexit 99\n")
            codex.chmod(0o755)
            claude.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "must-not-forward", "ANTHROPIC_API_KEY": "must-not-forward"},
                clear=False,
            ):
                module.initial_capability_refresh(
                    app_root=app_root,
                    state_root=root / "state",
                    codex_binary=codex,
                    codex_home=root / "state" / "codex-home",
                    process_home=root / "state" / "codex-process-home",
                    quota_snapshot_file=root / "state" / "claude-quota.json",
                    claude_binary=claude,
                )

            arguments = arguments_path.read_text().splitlines()
            environment = environment_path.read_text()
            self.assertEqual(
                arguments,
                [
                    "-m",
                    "codex_workbench",
                    "--home",
                    str(root / "state"),
                    "capabilities",
                    "refresh",
                    "--bundled",
                    "--activate-safe",
                ],
            )
            self.assertNotIn("must-not-forward", environment)
            self.assertNotIn("OPENAI_API_KEY=", environment)
            self.assertNotIn("ANTHROPIC_API_KEY=", environment)
            self.assertNotIn("login", arguments)
            self.assertNotIn("exec", arguments)

    def test_initial_capability_refresh_fails_loudly_on_fixture_failure(self) -> None:
        module = self._macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            runtime = root / "app" / "scripts" / "python-runtime"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("#!/bin/sh\nprintf 'bundled fixture failed' >&2\nexit 23\n")
            runtime.chmod(0o755)

            with self.assertRaisesRegex(SystemExit, "initial capability catalog refresh failed: bundled fixture failed"):
                module.initial_capability_refresh(
                    app_root=root / "app",
                    state_root=root / "state",
                    codex_binary=root / "runtime-codex",
                    codex_home=root / "state" / "codex-home",
                    process_home=root / "state" / "codex-process-home",
                    quota_snapshot_file=root / "state" / "claude-quota.json",
                    claude_binary=None,
                )

    def test_macbook_tunnel_reconnect_is_bounded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "launchd" / "com.lisihao.codex-workbench-tunnel.plist.in"
        ).read_text()
        rendered = (
            template.replace("__LOG_ROOT__", "/tmp/logs")
            .replace("__CLIENT_ID__", "macbook-fixture")
            .replace("__AUTHORITY_SSH_ALIAS__", "authority-fixture")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 300)
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("ThrottleInterval", payload)
        self.assertIn("127.0.0.1:18766:127.0.0.1:8766", payload["ProgramArguments"])
        self.assertIn("-N", payload["ProgramArguments"])
        self.assertEqual(payload["ProgramArguments"][-1], "authority-fixture")
        self.assertNotIn("client heartbeat", " ".join(payload["ProgramArguments"]))

    def test_macbook_heartbeat_uses_one_bounded_short_lived_native_ssh(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "launchd" / "com.lisihao.codex-workbench-heartbeat.plist.in"
        ).read_text()
        rendered = (
            template.replace("__LOG_ROOT__", "/tmp/logs")
            .replace("__CLIENT_ID__", "macbook-fixture")
            .replace("__AUTHORITY_SSH_ALIAS__", "authority-fixture")
        )
        payload = plistlib.loads(rendered.encode())
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 300)
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(payload["ProgramArguments"][-2], "authority-fixture")
        self.assertIn("client heartbeat", payload["ProgramArguments"][-1])
        self.assertIn("macbook-fixture", payload["ProgramArguments"][-1])
        self.assertNotIn("while true", payload["ProgramArguments"][-1])

    def test_macbook_installer_installs_tunnel_and_heartbeat_agents(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        ).read_text()
        self.assertIn('HEARTBEAT_LABEL = "com.lisihao.codex-workbench-heartbeat"', source)
        self.assertIn("for label in (TUNNEL_LABEL, HEARTBEAT_LABEL)", source)

    def test_macbook_installer_supports_configurable_authority_alias(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py"
        ).read_text()
        self.assertIn('"--authority-ssh-alias"', source)
        self.assertIn('default="macmini"', source)
        self.assertIn('__AUTHORITY_SSH_ALIAS__', source)

    def test_macbook_installer_auto_uses_native_ssh_over_tailscale_serve(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="100.64.0.42",
        ), mock.patch.object(
            module,
            "configured_ssh_proxycommand",
            return_value=None,
        ), mock.patch.object(
            module.shutil,
            "which",
            return_value="/opt/homebrew/bin/tailscale",
        ):
            arguments = module.ssh_transport_arguments("macmini", "auto")

        self.assertEqual(
            arguments,
            (
                "-o",
                "ProxyCommand=/opt/homebrew/bin/tailscale nc %h 10022",
                "-o",
                "HostKeyAlias=codex-workbench-100.64.0.42",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ),
        )

    def test_macbook_installer_can_still_select_tailscale_ssh_explicitly(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="100.64.0.42",
        ), mock.patch.object(
            module,
            "configured_ssh_proxycommand",
            return_value=None,
        ), mock.patch.object(
            module.shutil,
            "which",
            return_value="/opt/homebrew/bin/tailscale",
        ):
            arguments = module.ssh_transport_arguments(
                "macmini",
                "tailscale-userspace",
            )

        self.assertEqual(
            arguments,
            ("-o", "ProxyCommand=/opt/homebrew/bin/tailscale nc %h %p"),
        )

    def test_native_ssh_preserves_the_configured_userspace_tailscale_socket(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="100.64.0.42",
        ), mock.patch.object(
            module,
            "configured_ssh_proxycommand",
            return_value=(
                "/opt/homebrew/bin/tailscale "
                "--socket=/Users/example/.local/share/tailscale-userspace/tailscaled.sock "
                "nc %h %p"
            ),
        ), mock.patch.object(
            module.shutil,
            "which",
            return_value=None,
        ):
            arguments = module.ssh_transport_arguments("macmini", "auto")

        self.assertEqual(
            arguments[1],
            (
                "ProxyCommand=/opt/homebrew/bin/tailscale "
                "--socket=/Users/example/.local/share/tailscale-userspace/tailscaled.sock "
                "nc %h 10022"
            ),
        )

    def test_macbook_installer_keeps_system_ssh_for_non_tailscale_host(self) -> None:
        module = self._macbook_installer_module()
        with mock.patch.object(
            module,
            "configured_ssh_hostname",
            return_value="git.example.test",
        ):
            arguments = module.ssh_transport_arguments("build-server", "auto")

        self.assertEqual(arguments, ())

    def test_macbook_location_aware_options_require_complete_endpoints(self) -> None:
        module = self._macbook_installer_module()
        with self.assertRaisesRegex(SystemExit, "requires --authority-lan-host"):
            module.location_aware_enabled(
                "location-aware",
                lan_host="mini.home",
                tailnet_host=None,
                home_networks=["192.168.40.0/24"],
                lan_port=None,
            )
        with self.assertRaisesRegex(SystemExit, "require --ssh-transport auto or location-aware"):
            module.location_aware_enabled(
                "system",
                lan_host="mini.home",
                tailnet_host="mini.tailnet.ts.net",
                home_networks=["192.168.40.0/24"],
                lan_port=None,
            )
        self.assertTrue(
            module.location_aware_enabled(
                "auto",
                lan_host="mini-rn0x.home",
                tailnet_host="mini.tailnet.ts.net",
                home_networks=["192.168.40.25/24"],
                lan_port=2200,
            )
        )
        transport = module.build_location_aware_transport(
            lan_host="mini-rn0x.home",
            lan_port=2200,
            tailnet_host="mini.tailnet.ts.net",
            tailnet_port=10022,
            home_networks=["192.168.40.25/24"],
            tailscale_binary="/opt/homebrew/bin/tailscale",
            status_file=Path("/tmp/location-status.json"),
            tailscale_socket="/Users/example/.local/share/tailscale/tailscaled.sock",
        )
        self.assertEqual(transport.configuration["home_networks"], ["192.168.40.0/24"])
        self.assertEqual(transport.configuration["lan"], {"host": "mini-rn0x.home", "port": 2200})
        self.assertEqual(transport.host_key_alias, "codex-workbench-authority")
        self.assertEqual(
            transport.configuration["tailscale"]["socket"],
            "/Users/example/.local/share/tailscale/tailscaled.sock",
        )
        with self.assertRaisesRegex(SystemExit, "cannot overlap Tailscale"):
            module.normalise_home_networks(["100.64.0.0/10"])

    def test_macbook_location_aware_preflight_uses_ephemeral_proxy_and_config(self) -> None:
        module = self._macbook_installer_module()
        root = Path(__file__).resolve().parents[1]
        source_proxy = root / "scripts" / "workbench-location-proxy.py"
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            persistent_status = Path(directory) / "persistent" / "status.json"
            transport = module.build_location_aware_transport(
                lan_host="mini.home",
                lan_port=22,
                tailnet_host="mini.tailnet.ts.net",
                tailnet_port=10022,
                home_networks=["192.168.40.0/24"],
                tailscale_binary="/opt/homebrew/bin/tailscale",
                status_file=persistent_status,
            )
            observed: list[tuple[Path, Path, Path]] = []

            def check_preflight(
                authority: str,
                transport_arguments: tuple[str, ...],
                state_root: str,
            ) -> str:
                self.assertEqual(authority, "macmini")
                self.assertEqual(state_root, "/srv/codex-workbench")
                proxy_command = next(
                    value.removeprefix("ProxyCommand=")
                    for value in transport_arguments
                    if value.startswith("ProxyCommand=")
                )
                command = shlex.split(proxy_command)
                self.assertEqual(command[1], "--config")
                proxy = Path(command[0])
                configuration = Path(command[2])
                runtime = proxy.with_suffix(".py")
                self.assertNotEqual(proxy, source_proxy)
                self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
                self.assertEqual(runtime.read_bytes(), source_proxy.read_bytes())
                self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
                launcher = proxy.read_text()
                self.assertIn(shlex.quote(module.sys.executable), launcher)
                self.assertIn(shlex.quote(str(runtime)), launcher)
                payload = json.loads(configuration.read_text())
                self.assertEqual(payload["status_file"], str(configuration.parent / "status.json"))
                self.assertNotEqual(payload["status_file"], str(persistent_status))
                self.assertIn("HostKeyAlias=codex-workbench-authority", transport_arguments)
                observed.append((proxy, configuration, runtime))
                return "/srv/codex-workbench/app/bin/codex-workbench"

            with mock.patch.object(module, "preflight_remote_mcp", side_effect=check_preflight):
                result = module.preflight_location_aware_mcp(
                    "macmini",
                    source_proxy,
                    transport,
                    "/srv/codex-workbench",
                )

            self.assertEqual(result, "/srv/codex-workbench/app/bin/codex-workbench")
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0][0].exists())
            self.assertFalse(observed[0][1].exists())
            self.assertFalse(observed[0][2].exists())

    def test_macbook_location_aware_install_uses_one_transport_for_mcp_tunnel_and_heartbeat(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        source_proxy = source / "scripts" / "workbench-location-proxy.py"
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                if command[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def fake_which(name: str) -> str | None:
                return {
                    "codex": "/usr/bin/codex",
                    "tailscale": "/opt/homebrew/bin/tailscale",
                }.get(name)

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module, "run", side_effect=fake_run
            ), mock.patch.object(
                module.shutil, "which", side_effect=fake_which
            ), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "install_code_as_harness"
            ), mock.patch.object(module, "install_archify"), mock.patch.object(
                module, "preflight_location_aware_mcp", return_value="/remote/codex-workbench"
            ) as preflight, mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macbook-client.py",
                    "--source",
                    str(source),
                    "--authority-lan-host",
                    "mini.home",
                    "--authority-tailnet-host",
                    "mini.tailnet.ts.net",
                    "--home-network",
                    "192.168.40.0/24",
                    "--tailscale-socket",
                    "/Users/example/.local/share/tailscale/tailscaled.sock",
                ],
            ):
                self.assertEqual(module.main(), 0)

            client_root = home / "Library" / "Application Support" / "Codex Workbench Client"
            proxy = client_root / "bin" / "workbench-location-proxy"
            proxy_runtime = client_root / "libexec" / "workbench-location-proxy.py"
            configuration = client_root / "transport.json"
            status = client_root / "status.json"
            self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
            self.assertEqual(proxy_runtime.stat().st_mode & 0o777, 0o600)
            self.assertEqual(configuration.stat().st_mode & 0o777, 0o600)
            self.assertEqual(proxy_runtime.read_bytes(), source_proxy.read_bytes())
            launcher = proxy.read_text()
            self.assertIn(shlex.quote(module.sys.executable), launcher)
            self.assertIn(shlex.quote(str(proxy_runtime)), launcher)
            payload = json.loads(configuration.read_text())
            self.assertEqual(payload["status_file"], str(status))
            self.assertEqual(payload["tailscale"], {
                "host": "mini.tailnet.ts.net",
                "port": 10022,
                "binary": "/opt/homebrew/bin/tailscale",
                "socket": "/Users/example/.local/share/tailscale/tailscaled.sock",
            })

            expected_transport = module.location_aware_ssh_arguments(
                proxy,
                configuration,
                module.LOCATION_AWARE_HOST_KEY_ALIAS,
            )
            tunnel_plist = home / "Library" / "LaunchAgents" / f"{module.TUNNEL_LABEL}.plist"
            tunnel_arguments = plistlib.loads(tunnel_plist.read_bytes())["ProgramArguments"]
            for value in expected_transport:
                self.assertIn(value, tunnel_arguments)
            heartbeat_plist = home / "Library" / "LaunchAgents" / f"{module.HEARTBEAT_LABEL}.plist"
            heartbeat_arguments = plistlib.loads(heartbeat_plist.read_bytes())["ProgramArguments"]
            heartbeat_launcher = client_root / "bin" / "workbench-client-heartbeat"
            heartbeat_runtime = client_root / "libexec" / "workbench-client-heartbeat.py"
            self.assertEqual(heartbeat_arguments[0], str(heartbeat_launcher))
            self.assertIn(str(proxy), heartbeat_arguments)
            self.assertIn(str(configuration), heartbeat_arguments)
            self.assertEqual(
                heartbeat_runtime.read_bytes(),
                (source / "scripts" / "workbench-client-heartbeat.py").read_bytes(),
            )
            mcp_add = next(
                command for command in calls if len(command) >= 3 and command[1:3] == ("mcp", "add")
            )
            for value in expected_transport:
                self.assertIn(value, mcp_add)
            self.assertEqual(preflight.call_args.args[1], source_proxy)
            self.assertEqual(
                preflight.call_args.args[2].configuration["status_file"],
                str(status),
            )

    def test_macbook_static_install_disables_stale_location_profile_for_git_sync(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"
            client_root = home / "Library" / "Application Support" / "Codex Workbench Client"
            proxy = client_root / "bin" / "workbench-location-proxy"
            configuration = client_root / "transport.json"
            status = client_root / "status.json"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("#!/bin/sh\n")
            configuration.write_text('{"schema_version":1}\n')
            status.write_text('{"route":"tailscale"}\n')

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                if command[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module, "run", side_effect=fake_run
            ), mock.patch.object(
                module.shutil, "which", return_value="/usr/bin/codex"
            ), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "preflight_remote_mcp", return_value="/remote/codex-workbench"
            ), mock.patch.object(module, "install_code_as_harness"), mock.patch.object(
                module, "install_archify"
            ), mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macbook-client.py",
                    "--source",
                    str(source),
                    "--ssh-transport",
                    "system",
                ],
            ):
                self.assertEqual(module.main(), 0)

            self.assertTrue(proxy.exists())
            self.assertFalse(configuration.exists())
            self.assertFalse(status.exists())

    def test_macbook_location_aware_dry_run_does_not_write_or_connect(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def fake_which(name: str) -> str | None:
                return {
                    "codex": "/usr/bin/codex",
                    "tailscale": "/opt/homebrew/bin/tailscale",
                }.get(name)

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module, "run", side_effect=fake_run
            ), mock.patch.object(
                module.shutil, "which", side_effect=fake_which
            ), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "preflight_location_aware_mcp"
            ) as preflight, mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macbook-client.py",
                    "--source",
                    str(source),
                    "--dry-run",
                    "--authority-lan-host",
                    "mini.home",
                    "--authority-tailnet-host",
                    "mini.tailnet.ts.net",
                    "--home-network",
                    "192.168.40.0/24",
                ],
            ):
                self.assertEqual(module.main(), 0)

            preflight.assert_not_called()
            self.assertFalse(
                (home / "Library" / "Application Support" / "Codex Workbench Client").exists()
            )
            self.assertFalse(any(command and command[0] in {"ssh", "launchctl"} for command in calls))
            self.assertFalse(
                any(
                    len(command) >= 3 and command[1:3] in (("mcp", "remove"), ("mcp", "add"))
                    for command in calls
                )
            )

    def test_macbook_location_aware_rolls_back_proxy_config_and_status_on_launch_failure(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                if command[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")
                if command[:2] == ("launchctl", "bootstrap"):
                    raise RuntimeError("injected launchctl failure")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def fake_which(name: str) -> str | None:
                return {
                    "codex": "/usr/bin/codex",
                    "tailscale": "/opt/homebrew/bin/tailscale",
                }.get(name)

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module, "run", side_effect=fake_run
            ), mock.patch.object(
                module.shutil, "which", side_effect=fake_which
            ), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "preflight_location_aware_mcp", return_value="/remote/codex-workbench"
            ), mock.patch.object(module, "install_code_as_harness"), mock.patch.object(
                module, "install_archify"
            ), mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macbook-client.py",
                    "--source",
                    str(source),
                    "--ssh-transport",
                    "location-aware",
                    "--authority-lan-host",
                    "mini.home",
                    "--authority-tailnet-host",
                    "mini.tailnet.ts.net",
                    "--home-network",
                    "192.168.40.0/24",
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "injected launchctl failure"):
                    module.main()

            client_root = home / "Library" / "Application Support" / "Codex Workbench Client"
            self.assertFalse(client_root.exists())
            self.assertFalse(
                (home / "Library" / "LaunchAgents" / f"{module.TUNNEL_LABEL}.plist").exists()
            )

    def test_authority_installer_configures_tailnet_only_https_and_native_ssh(self) -> None:
        module = self._macos_installer_module()
        with mock.patch.object(module, "run") as run:
            module.configure_tailscale_serve(
                "/opt/homebrew/bin/tailscale",
                "/var/run/tailscale/tailscaled.sock",
                https_port=10443,
                native_ssh_port=10022,
            )

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    "/opt/homebrew/bin/tailscale",
                    "--socket=/var/run/tailscale/tailscaled.sock",
                    "serve",
                    "--yes",
                    "--bg",
                    "--https=10443",
                    "http://127.0.0.1:8766",
                ),
                mock.call(
                    "/opt/homebrew/bin/tailscale",
                    "--socket=/var/run/tailscale/tailscaled.sock",
                    "serve",
                    "--yes",
                    "--bg",
                    "--tcp=10022",
                    "tcp://127.0.0.1:22",
                ),
            ],
        )

    def test_top_level_installers_reject_direct_and_ancestor_symlinks(self) -> None:
        for module_loader in (self._macos_installer_module, self._macbook_installer_module):
            module = module_loader()
            with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
                root = Path(directory)
                target = root / "target"
                target.mkdir()
                for name, link_target in (
                    ("live", target),
                    ("broken", root / "missing"),
                ):
                    link = root / name
                    link.symlink_to(link_target, target_is_directory=True)
                    with self.subTest(installer=module_loader.__name__, name=name):
                        with self.assertRaisesRegex(SystemExit, "symlink ancestor"):
                            module.assert_directory_target(link / "child", "fixture target")
                direct = root / "direct"
                direct.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(SystemExit, "symlink ancestor"):
                    module.assert_file_target(direct, "fixture file")

    def test_installer_transactions_restore_files_without_touching_old_backup(self) -> None:
        for module_loader in (self._macos_installer_module, self._macbook_installer_module):
            module = module_loader()
            with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
                root = Path(directory)
                target = root / "config.json"
                target.write_text("before\n")
                transaction = module.InstallTransaction(root)
                transaction.snapshot(target, "fixture config")
                target.write_text("after\n")
                transaction.rollback()
                self.assertEqual(target.read_text(), "before\n")
                self.assertFalse(transaction.root.exists())

    def test_authority_preserves_existing_previous_app_backup_on_upgrade(self) -> None:
        module = self._macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            state_root = Path(directory)
            app = state_root / "app"
            app.mkdir()
            (app / "version").write_text("old")
            previous = state_root / "previous-app"
            previous.mkdir()
            (previous / "version").write_text("older")
            transaction = module.InstallTransaction(state_root)
            transaction.preserve_existing_app(app, state_root)
            app.mkdir()
            (app / "version").write_text("new")
            transaction.rollback()
            self.assertEqual((app / "version").read_text(), "old")
            self.assertEqual((previous / "version").read_text(), "older")

    def test_macbook_custom_authority_state_root_reaches_remote_paths(self) -> None:
        module = self._macbook_installer_module()
        self.assertEqual(
            module.authority_mcp_binary("/srv/codex-workbench"),
            "/srv/codex-workbench/app/bin/codex-workbench",
        )
        self.assertEqual(
            module.authority_mcp_binary("~/Library/Application Support/Custom WB"),
            "$HOME/Library/Application Support/Custom WB/app/bin/codex-workbench",
        )
        self.assertIn("--authority-state-root", (Path(__file__).resolve().parents[1] / "scripts" / "install-macbook-client.py").read_text())
        payload = module.render_client_plist(
            Path(__file__).resolve().parents[1],
            Path("/tmp/logs"),
            "macbook-fixture",
            "authority-fixture",
            module.HEARTBEAT_LABEL,
            (),
            "/srv/codex-workbench/app/bin/codex-workbench",
        )
        self.assertIn(
            "/srv/codex-workbench/app/bin/codex-workbench",
            payload["ProgramArguments"][-1],
        )
        with mock.patch.object(module, "run") as run:
            module.preflight_remote_mcp("authority-fixture", (), "/srv/codex-workbench")
        self.assertEqual(
            run.call_args.args,
            (
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "authority-fixture",
                "test -x /srv/codex-workbench/app/bin/codex-workbench",
            ),
        )

    def test_authority_dry_run_is_local_and_does_not_install(self) -> None:
        module = self._macos_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            auth_source = home / ".codex" / "auth.json"
            auth_source.parent.mkdir(parents=True)
            auth_source.write_text("{}\n")
            research = root / "research"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = research / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n")
            codex.chmod(0o755)
            host = root / "codex-code-mode-host"
            host.write_text("#!/bin/sh\n")
            host.chmod(0o755)
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="fixture\n", stderr="")

            output = io.StringIO()
            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module, "run", side_effect=fake_run
            ), mock.patch.object(module, "macos_machine_id", return_value="fixture-machine"), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "install_code_as_harness"
            ) as install_harness, mock.patch.object(module, "install_archify") as install_archify, mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macos.py",
                    "--source",
                    str(source),
                    "--state-root",
                    str(root / "state"),
                    "--codex-binary",
                    str(codex),
                    "--research-skill-source",
                    str(research),
                    "--dry-run",
                ],
            ), redirect_stdout(output):
                result = module.main()

            self.assertEqual(result, 0)
            install_harness.assert_not_called()
            install_archify.assert_not_called()
            self.assertFalse((root / "state").exists())
            self.assertTrue(all(command[0] == "git" for command in calls))
            self.assertIn("plan: capabilities=", output.getvalue())
            self.assertIn("passive bundled refresh before services", output.getvalue())
            self.assertIn("plan: radar=", output.getvalue())
            self.assertIn("codex_workbench --home", output.getvalue())
            self.assertIn("radar refresh", output.getvalue())

    def test_macbook_dry_run_skips_ssh_launchctl_and_mcp_mutations(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:3] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module.shutil, "which", return_value="/usr/bin/codex"
            ), mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "install_code_as_harness"
            ) as install_harness, mock.patch.object(module, "install_archify") as install_archify, mock.patch.object(
                module.sys,
                "argv",
                [
                    "install-macbook-client.py",
                    "--source",
                    str(source),
                    "--authority-state-root",
                    "/srv/custom-workbench",
                    "--dry-run",
                ],
            ):
                result = module.main()

            self.assertEqual(result, 0)
            install_harness.assert_not_called()
            install_archify.assert_not_called()
            forbidden = {"ssh", "launchctl"}
            self.assertFalse(any(command and command[0] in forbidden for command in calls))
            self.assertFalse(any(len(command) >= 3 and command[1:3] in (("mcp", "remove"), ("mcp", "add")) for command in calls))

    def test_macbook_main_rolls_back_files_when_launchctl_bootstrap_fails(self) -> None:
        module = self._macbook_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory) / "home"
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if len(command) >= 3 and command[1:3] == ("mcp", "get"):
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
                if command[:2] == ("launchctl", "bootstrap"):
                    raise RuntimeError("injected launchctl failure")
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")

            def write_code_policy(path: Path) -> None:
                policy = home / ".codex" / "AGENTS.md"
                policy.parent.mkdir(parents=True, exist_ok=True)
                policy.write_text("new policy\n")

            def write_archify(path: Path) -> None:
                target = home / ".codex" / "skills" / "archify"
                target.mkdir(parents=True, exist_ok=True)
                (target / "SKILL.md").write_text("new archify\n")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module.shutil, "which", return_value="/usr/bin/codex"
            ), mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
                module, "preflight_global_agent_targets"
            ), mock.patch.object(module, "preflight_managed_agent_skills"), mock.patch.object(
                module, "preflight_remote_mcp"
            ), mock.patch.object(module, "install_code_as_harness", side_effect=write_code_policy), mock.patch.object(
                module, "install_archify", side_effect=write_archify
            ), mock.patch.object(
                module.sys,
                "argv",
                ["install-macbook-client.py", "--source", str(source), "--ssh-transport", "system"],
            ):
                with self.assertRaisesRegex(RuntimeError, "injected launchctl failure"):
                    module.main()

            self.assertFalse((home / ".codex" / "AGENTS.md").exists())
            self.assertFalse((home / ".codex" / "skills" / "archify").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{module.TUNNEL_LABEL}.plist").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{module.HEARTBEAT_LABEL}.plist").exists())
            self.assertFalse(any(".codex-workbench-client-" in path.name for path in home.parent.iterdir()))

    def test_authority_main_rolls_back_global_and_runtime_files_on_failure(self) -> None:
        module = self._macos_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            auth_source = home / ".codex" / "auth.json"
            auth_source.parent.mkdir(parents=True)
            auth_source.write_text("{}\n")
            research = root / "research"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = research / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n")
            codex.chmod(0o755)
            host = root / "codex-code-mode-host"
            host.write_text("#!/bin/sh\n")
            host.chmod(0o755)

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                if command and command[0] == "git":
                    if len(command) > 3 and command[3] == "rev-parse":
                        return subprocess.CompletedProcess(command, 0, stdout="fixture\n", stderr="")
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="no-tag")
                if command and command[0] == "launchctl":
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")
                if command and command[0].endswith("/codex"):
                    raise RuntimeError("injected runtime probe failure")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def write_code_policy(path: Path) -> None:
                policy = home / ".codex" / "AGENTS.md"
                policy.parent.mkdir(parents=True, exist_ok=True)
                policy.write_text("new policy\n")

            def write_archify(path: Path) -> None:
                target = home / ".claude" / "skills" / "archify"
                target.mkdir(parents=True, exist_ok=True)
                (target / "SKILL.md").write_text("new archify\n")

            state_root = root / "state"
            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module.shutil, "which", return_value=None
            ), mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
                module, "macos_machine_id", return_value="fixture-machine"
            ), mock.patch.object(module, "preflight_global_agent_targets"), mock.patch.object(
                module, "preflight_managed_agent_skills"
            ), mock.patch.object(module, "install_code_as_harness", side_effect=write_code_policy), mock.patch.object(
                module, "install_archify", side_effect=write_archify
            ), mock.patch.object(
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
                    "--research-skill-source",
                    str(research),
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "injected runtime probe failure"):
                    module.main()

            self.assertFalse((home / ".codex" / "AGENTS.md").exists())
            self.assertFalse((home / ".claude" / "skills" / "archify").exists())
            self.assertFalse(state_root.exists())

    def test_authority_installer_writes_capability_sidecar_manifest_and_health_checks_it(self) -> None:
        module = self._macos_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            auth_source = home / ".codex" / "auth.json"
            auth_source.parent.mkdir(parents=True)
            auth_source.write_text("{}\n")
            research = root / "research"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = research / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            codex = root / "fixture-codex"
            codex_host = root / "codex-code-mode-host"
            for fixture in (codex, codex_host):
                fixture.write_text("#!/bin/sh\nexit 0\n")
                fixture.chmod(0o755)
            state_root = root / "state"
            state_root.mkdir()
            (state_root / "config.json").write_text(
                json.dumps({"user_setting": "preserve", "capability_refresh_seconds": 1234})
            )
            calls: list[tuple[str, ...]] = []
            refreshes: list[dict[str, object]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[0] == "git":
                    if len(command) > 3 and command[3] == "rev-parse":
                        return subprocess.CompletedProcess(command, 0, stdout="fixture-sha\n", stderr="")
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="no-tag")
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if command[0] == "launchctl":
                    return subprocess.CompletedProcess(command, 0, stdout="fixture-loaded\n", stderr="")
                if command[0].endswith("/runtime/codex"):
                    return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.149.1\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def fixture_refresh(**kwargs: object) -> None:
                self.assertFalse(any(command[:2] == ("launchctl", "bootstrap") for command in calls))
                refreshes.append(kwargs)
                state = kwargs["state_root"]
                assert isinstance(state, Path)
                catalog = state / "capabilities" / "generations"
                catalog.mkdir(parents=True)
                (catalog / "fixture.json").write_text("{}\n")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module.shutil, "which", return_value=None
            ), mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
                module, "macos_machine_id", return_value="fixture-machine"
            ), mock.patch.object(module, "preflight_global_agent_targets"), mock.patch.object(
                module, "preflight_managed_agent_skills"
            ), mock.patch.object(module, "install_code_as_harness"), mock.patch.object(
                module, "install_archify"
            ), mock.patch.object(module, "initial_capability_refresh", side_effect=fixture_refresh), mock.patch.object(
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
                    "--research-skill-source",
                    str(research),
                ],
            ):
                self.assertEqual(module.main(), 0)

            self.assertEqual(len(refreshes), 1)
            refresh = refreshes[0]
            self.assertEqual(refresh["app_root"], state_root / "app")
            self.assertEqual(refresh["codex_home"], state_root / "codex-home")
            self.assertEqual(refresh["codex_binary"], state_root / "runtime" / "codex")
            self.assertIsNone(refresh["claude_binary"])
            config = json.loads((state_root / "config.json").read_text())
            self.assertEqual(config["user_setting"], "preserve")
            self.assertEqual(config["capability_refresh_seconds"], 1234)
            self.assertEqual(config["max_workers"], 8)
            self.assertEqual(config["spark_workers"], 4)
            self.assertEqual(
                config["worktree_recovery"],
                {
                    "enabled": True,
                    "recycle_root": str(state_root / "recycle" / "worktrees"),
                    "restore_root": str(state_root / "restored-worktrees"),
                    "outgoing_root": str(state_root / "recycle" / "outgoing"),
                    "sweep_interval_seconds": 60,
                    "home_presence_ttl_seconds": 600,
                    "retry_backoff_seconds": 900,
                    "compression": "zstd",
                    "require_smb": True,
                },
            )
            self.assertEqual(
                config["performance"],
                {
                    "state_root": str(state_root / "performance"),
                    "baseline_resource": module.PERFORMANCE_BASELINE_RESOURCE,
                    "refresh_interval_seconds": 1234,
                    "refresh_command": [
                        str(state_root / "app" / "scripts" / "python-runtime"),
                        "-m",
                        "codex_workbench",
                        "--home",
                        str(state_root),
                        "capabilities",
                        "refresh",
                        "--activate-safe",
                    ],
                },
            )
            self.assertEqual(
                config["radar"],
                {
                    "producer": module.RADAR_PRODUCER,
                    "upstream": {
                        "repository": module.RADAR_UPSTREAM_REPOSITORY,
                        "tag": module.RADAR_UPSTREAM_TAG,
                        "commit": module.RADAR_UPSTREAM_COMMIT,
                    },
                    "state_root": str(state_root / "radar"),
                    "authorization_receipt": str(state_root / "radar" / "authorization.json"),
                    "refresh_interval_seconds": module.DEFAULT_RADAR_REFRESH_SECONDS,
                    "attribution": module.RADAR_ATTRIBUTION,
                    "refresh_command": [
                        str(state_root / "app" / "scripts" / "python-runtime"),
                        "-m",
                        "codex_workbench",
                        "--home",
                        str(state_root),
                        "radar",
                        "refresh",
                    ],
                    "authority_only": True,
                },
            )
            manifest = json.loads((state_root / "app" / "install-manifest.json").read_text())
            self.assertEqual(
                manifest["capabilities"],
                {
                    "schema_version": module.CAPABILITY_REGISTRY_SCHEMA_VERSION,
                    "policy": module.CAPABILITY_REGISTRY_POLICY,
                    "refresh_interval_seconds": 1234,
                    "sidecar_label": module.CAPABILITY_LABEL,
                    "activation": "safe-only",
                    "initial_refresh": "bundled-safe",
                },
            )
            self.assertNotIn("catalog", manifest["capabilities"])
            self.assertEqual(
                manifest["performance"],
                config["performance"],
            )
            self.assertEqual(
                manifest["worktree_recovery"],
                config["worktree_recovery"],
            )
            self.assertEqual(manifest["radar"], config["radar"])
            self.assertTrue((state_root / "radar").is_dir())
            self.assertFalse((state_root / "radar" / "authorization.json").exists())
            capability_plist = home / "Library" / "LaunchAgents" / f"{module.CAPABILITY_LABEL}.plist"
            payload = plistlib.loads(capability_plist.read_bytes())
            self.assertEqual(payload["StartInterval"], 1234)
            self.assertEqual(payload["EnvironmentVariables"]["CODEX_HOME"], str(state_root / "codex-home"))
            self.assertEqual(payload["EnvironmentVariables"]["CODEX_WORKBENCH_CODEX"], str(state_root / "runtime" / "codex"))
            self.assertEqual(payload["EnvironmentVariables"]["HOME"], str(home))
            launchctl_commands = [command for command in calls if command and command[0] == "launchctl"]
            sidecar_service = f"gui/501/{module.CAPABILITY_LABEL}"
            self.assertTrue(
                any(command[1] == "bootstrap" and str(capability_plist) in command for command in launchctl_commands)
            )
            self.assertTrue(
                any(command[1] == "kickstart" and command[-1] == sidecar_service for command in launchctl_commands)
            )
            self.assertTrue(
                any(command[1] == "print" and command[-1] == sidecar_service for command in launchctl_commands)
            )
            radar_plist = home / "Library" / "LaunchAgents" / f"{module.RADAR_LABEL}.plist"
            radar_payload = plistlib.loads(radar_plist.read_bytes())
            self.assertEqual(radar_payload["StartInterval"], module.DEFAULT_RADAR_REFRESH_SECONDS)
            self.assertEqual(
                radar_payload["ProgramArguments"],
                config["radar"]["refresh_command"],
            )
            radar_service = f"gui/501/{module.RADAR_LABEL}"
            self.assertTrue(
                any(command[1] == "bootstrap" and str(radar_plist) in command for command in launchctl_commands)
            )
            self.assertTrue(
                any(command[1] == "kickstart" and command[-1] == radar_service for command in launchctl_commands)
            )
            self.assertTrue(
                any(command[1] == "print" and command[-1] == radar_service for command in launchctl_commands)
            )

    def test_authority_installer_rolls_back_catalog_when_initial_bundled_refresh_fails(self) -> None:
        module = self._macos_installer_module()
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            home = root / "home"
            auth_source = home / ".codex" / "auth.json"
            auth_source.parent.mkdir(parents=True)
            auth_source.write_text("{}\n")
            research = root / "research"
            for relative in module.RESEARCH_SKILL_REQUIRED_FILES:
                path = research / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            codex = root / "fixture-codex"
            codex_host = root / "codex-code-mode-host"
            for fixture in (codex, codex_host):
                fixture.write_text("#!/bin/sh\nexit 0\n")
                fixture.chmod(0o755)
            state_root = root / "state"
            previous_catalog = state_root / "capabilities" / "generations" / "previous.json"
            previous_catalog.parent.mkdir(parents=True)
            previous_catalog.write_text('{"previous":true}\n')
            previous_radar = state_root / "radar" / "last-known-good.json"
            previous_radar.parent.mkdir(parents=True)
            previous_radar.write_text('{"radar":"previous"}\n')
            original_config = '{"unrelated":"keep"}\n'
            (state_root / "config.json").write_text(original_config)
            calls: list[tuple[str, ...]] = []

            def fake_run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[0] == "git":
                    if len(command) > 3 and command[3] == "rev-parse":
                        return subprocess.CompletedProcess(command, 0, stdout="fixture-sha\n", stderr="")
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="no-tag")
                if command[:2] == ("id", "-u"):
                    return subprocess.CompletedProcess(command, 0, stdout="501\n", stderr="")
                if command[0] == "launchctl":
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="not-loaded")
                if command[0].endswith("/runtime/codex"):
                    return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.149.1\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def fail_refresh(**kwargs: object) -> None:
                state = kwargs["state_root"]
                assert isinstance(state, Path)
                generated = state / "capabilities" / "generations" / "failed.json"
                generated.write_text("{}\n")
                raise SystemExit("initial capability catalog refresh failed: fixture bundled failure")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(
                module.shutil, "which", return_value=None
            ), mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
                module, "macos_machine_id", return_value="fixture-machine"
            ), mock.patch.object(module, "preflight_global_agent_targets"), mock.patch.object(
                module, "preflight_managed_agent_skills"
            ), mock.patch.object(module, "install_code_as_harness"), mock.patch.object(
                module, "install_archify"
            ), mock.patch.object(module, "initial_capability_refresh", side_effect=fail_refresh), mock.patch.object(
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
                    "--research-skill-source",
                    str(research),
                ],
            ):
                with self.assertRaisesRegex(SystemExit, "fixture bundled failure"):
                    module.main()

            self.assertEqual(previous_catalog.read_text(), '{"previous":true}\n')
            self.assertEqual(previous_radar.read_text(), '{"radar":"previous"}\n')
            self.assertFalse((state_root / "capabilities" / "generations" / "failed.json").exists())
            self.assertFalse((state_root / "radar" / "authorization.json").exists())
            self.assertEqual((state_root / "config.json").read_text(), original_config)
            self.assertFalse((state_root / "app").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{module.CAPABILITY_LABEL}.plist").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{module.RADAR_LABEL}.plist").exists())
            self.assertFalse(any(command[:2] == ("launchctl", "bootstrap") for command in calls))


if __name__ == "__main__":
    unittest.main()
