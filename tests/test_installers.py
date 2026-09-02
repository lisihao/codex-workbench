from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import plistlib
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
            ):
                result = module.main()

            self.assertEqual(result, 0)
            install_harness.assert_not_called()
            install_archify.assert_not_called()
            self.assertFalse((root / "state").exists())
            self.assertTrue(all(command[0] == "git" for command in calls))

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


if __name__ == "__main__":
    unittest.main()
