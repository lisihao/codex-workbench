from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_ROOT = ROOT / "plugins" / "codex-workbench"
PROVIDER_PLUGIN_ROOT = ROOT / "plugins" / "codex-radar-provider"
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
PROVIDER_MANIFEST_PATH = PROVIDER_PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
PROVIDER_UPSTREAM_LOCK_PATH = PROVIDER_PLUGIN_ROOT / "upstream-lock.json"
HOOKS_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
HOOK_SCRIPT = PLUGIN_ROOT / "scripts" / "wb_hook.py"
PROVIDER_LICENSE_PATH = PROVIDER_PLUGIN_ROOT / "LICENSE-WineChord-Codex-Radar"


class PluginDistributionTests(unittest.TestCase):
    @staticmethod
    def _project_version() -> str:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
        assert match is not None
        return match.group(1)

    @staticmethod
    def _normalize_markdown_body(text: str) -> str:
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    lines = lines[index + 1 :]
                    break
        return "\n".join(line.rstrip() for line in lines).strip()

    def test_marketplace_resolves_the_versioned_plugins(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        provider_manifest = json.loads(
            PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        upstream_lock = json.loads(
            PROVIDER_UPSTREAM_LOCK_PATH.read_text(encoding="utf-8")
        )

        self.assertEqual(marketplace["name"], "codex-workbench")
        self.assertEqual(manifest["name"], "codex-workbench")
        self.assertEqual(manifest["version"], self._project_version())
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        self.assertEqual(provider_manifest["name"], "codex-radar-provider")
        self.assertEqual(provider_manifest["version"], "0.1.0")
        self.assertEqual(provider_manifest["license"], "MIT")

        self.assertIsInstance(marketplace["plugins"], list)
        plugin_entries = {entry["name"]: entry for entry in marketplace["plugins"]}
        self.assertEqual(
            set(plugin_entries.keys()),
            {"codex-workbench", "codex-radar-provider"},
        )

        workbench_entry = plugin_entries["codex-workbench"]
        self.assertEqual(
            workbench_entry["source"],
            {"source": "local", "path": "./plugins/codex-workbench"},
        )
        self.assertEqual(
            workbench_entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual((ROOT / workbench_entry["source"]["path"]).resolve(), PLUGIN_ROOT)

        provider_entry = plugin_entries["codex-radar-provider"]
        self.assertEqual(
            provider_entry["source"],
            {"source": "local", "path": "./plugins/codex-radar-provider"},
        )
        self.assertEqual(
            provider_entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(
            (ROOT / provider_entry["source"]["path"]).resolve(),
            PROVIDER_PLUGIN_ROOT,
        )

        self.assertEqual(
            sorted(
                p.name
                for p in (PROVIDER_PLUGIN_ROOT / "skills").iterdir()
                if p.is_dir()
            ),
            ["codex-radar-provider", "codex-radar-sync"],
        )
        self.assertTrue(
            (PROVIDER_PLUGIN_ROOT / "skills" / "codex-radar-provider" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (PROVIDER_PLUGIN_ROOT / "skills" / "codex-radar-sync" / "SKILL.md").is_file()
        )

        self.assertEqual(upstream_lock["repository"], "https://github.com/WineChord/codex-radar")
        self.assertEqual(upstream_lock["tag"], "v0.1.69")
        self.assertEqual(
            upstream_lock["commit"], "4c83973df6b17e6b18b0b56e8735168580fea12b"
        )
        self.assertEqual(
            upstream_lock["source_path"],
            "vendor/WineChord-codex-radar/skills/codex-radar-sync/SKILL.md",
        )
        self.assertEqual(
            upstream_lock["projection_path"],
            "skills/codex-radar-sync/SKILL.md",
        )
        self.assertEqual(upstream_lock["software_license"], "MIT")

        vendor_skill_path = PROVIDER_PLUGIN_ROOT / upstream_lock["source_path"]
        projection_skill_path = PROVIDER_PLUGIN_ROOT / upstream_lock["projection_path"]
        self.assertTrue(vendor_skill_path.is_file())
        self.assertTrue(projection_skill_path.is_file())
        self.assertEqual(
            self._normalize_markdown_body(vendor_skill_path.read_text(encoding="utf-8")),
            self._normalize_markdown_body(
                projection_skill_path.read_text(encoding="utf-8")
            ),
        )
        self.assertTrue(PROVIDER_LICENSE_PATH.is_file())
        self.assertIn("MIT License", PROVIDER_LICENSE_PATH.read_text(encoding="utf-8"))

    def test_hook_configuration_points_to_the_packaged_script(self) -> None:
        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

        self.assertIn("$PLUGIN_ROOT/scripts/wb_hook.py", command)
        self.assertTrue(HOOK_SCRIPT.is_file())
        self.assertTrue((PLUGIN_ROOT / "skills" / "WB" / "SKILL.md").is_file())

    @unittest.skipUnless(sys.platform == "darwin", "macOS hook interpreter check")
    def test_hook_parses_with_macos_system_python(self) -> None:
        interpreter = Path("/usr/bin/python3")
        self.assertTrue(interpreter.is_file())
        result = subprocess.run(
            [str(interpreter), "-m", "py_compile", str(HOOK_SCRIPT)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_marketplace_installs_only_into_a_temporary_codex_home(self) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex CLI is unavailable")
        with tempfile.TemporaryDirectory(prefix="codex-workbench-plugin-") as directory:
            home = Path(directory)
            codex_home = home / ".codex"
            codex_home.mkdir()
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["CODEX_HOME"] = str(codex_home)
            add_marketplace = subprocess.run(
                [codex, "plugin", "marketplace", "add", str(ROOT), "--json"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
                timeout=30,
            )
            self.assertEqual(add_marketplace.returncode, 0, add_marketplace.stderr)
            marketplace_receipt = json.loads(add_marketplace.stdout)
            self.assertEqual(marketplace_receipt["marketplaceName"], "codex-workbench")

            installed_plugins = [
                ("codex-workbench@codex-workbench", self._project_version()),
                ("codex-radar-provider@codex-workbench", "0.1.0"),
            ]
            installed_names = set()
            for plugin_id, expected_version in installed_plugins:
                install = subprocess.run(
                    [codex, "plugin", "add", plugin_id, "--json"],
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                receipt = json.loads(install.stdout)
                self.assertEqual(receipt["name"], plugin_id.split("@")[0])
                self.assertEqual(receipt["version"], expected_version)
                self.assertTrue(Path(receipt["installedPath"]).is_dir())
                installed_names.add(receipt["name"])

            list_plugins = subprocess.run(
                [codex, "plugin", "list", "--json"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
                timeout=30,
            )
            self.assertEqual(list_plugins.returncode, 0, list_plugins.stderr)
            plugin_items = json.loads(list_plugins.stdout)
            installed_items = plugin_items.get("installed", [])
            self.assertIsInstance(installed_items, list)
            listed_names = set(
                item.get("name")
                for item in installed_items
                if isinstance(item, dict) and "name" in item
            )
            self.assertTrue(installed_names.issubset(listed_names))


if __name__ == "__main__":
    unittest.main()
