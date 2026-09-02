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
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
HOOKS_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
HOOK_SCRIPT = PLUGIN_ROOT / "scripts" / "wb_hook.py"


class PluginDistributionTests(unittest.TestCase):
    @staticmethod
    def _project_version() -> str:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
        assert match is not None
        return match.group(1)

    def test_marketplace_resolves_the_versioned_plugin(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(marketplace["name"], "codex-workbench")
        self.assertEqual(manifest["name"], "codex-workbench")
        self.assertEqual(manifest["version"], self._project_version())
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/codex-workbench"})
        self.assertEqual(entry["policy"], {"installation": "AVAILABLE", "authentication": "ON_INSTALL"})
        self.assertEqual((ROOT / entry["source"]["path"]).resolve(), PLUGIN_ROOT)

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

            install = subprocess.run(
                [codex, "plugin", "add", "codex-workbench@codex-workbench", "--json"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
                timeout=30,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            receipt = json.loads(install.stdout)
            self.assertEqual(receipt["name"], "codex-workbench")
            self.assertEqual(receipt["version"], self._project_version())
            self.assertTrue(Path(receipt["installedPath"]).is_dir())


if __name__ == "__main__":
    unittest.main()
