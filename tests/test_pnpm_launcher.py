from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


PHYSICAL_TMP = Path(tempfile.gettempdir()).resolve()


def macos_installer_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.py"
    spec = importlib.util.spec_from_file_location("codex_workbench_pnpm_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PnpmLauncherTests(unittest.TestCase):
    def _fake_node(
        self,
        directory: Path,
        *,
        name: str = "node",
        node_version: str = "v22.13.0",
        pnpm_version: str = "11.25.0",
        hang: bool = False,
    ) -> Path:
        node = directory / name
        if hang:
            body = "#!/bin/sh\n/bin/sleep 1\n"
        else:
            body = (
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                f"  printf '%s\\n' '{node_version}'\n"
                "  exit 0\n"
                "fi\n"
                "if [ -n \"${FIXTURE_NODE_RECORD:-}\" ]; then\n"
                "  printf '%s\\n' \"$0\" >> \"$FIXTURE_NODE_RECORD\"\n"
                "fi\n"
                "entrypoint=$1\n"
                "shift\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                f"  printf '%s\\n' '{pnpm_version}'\n"
                "  exit 0\n"
                "fi\n"
                "exec \"$entrypoint\" \"$@\"\n"
            )
        node.write_text(body, encoding="utf-8")
        node.chmod(0o755)
        return node

    @staticmethod
    def _path_trap(directory: Path) -> Path:
        trap = directory / "node"
        trap.write_text(
            "#!/bin/sh\n"
            "printf 'PATH node selected\\n' >> \"$FIXTURE_PATH_TRAP_RECORD\"\n"
            "exit 97\n",
            encoding="utf-8",
        )
        trap.chmod(0o755)
        return trap

    def test_launcher_uses_bound_node_with_spaces_and_forwards_every_argument(self) -> None:
        module = macos_installer_module()
        with tempfile.TemporaryDirectory(
            prefix="pnpm launcher 'quoted' ", dir=PHYSICAL_TMP
        ) as directory:
            root = Path(directory)
            node_directory = root / "node dir"
            node_directory.mkdir()
            node = self._fake_node(node_directory, name="node with spaces")
            endpoint = root / "runtime with spaces" / "pnpm.mjs"
            endpoint.parent.mkdir()
            endpoint.write_text(
                "#!/bin/sh\n"
                ": > \"$FIXTURE_PNPM_RECORD\"\n"
                "for argument in \"$@\"; do\n"
                "  printf '<%s>\\n' \"$argument\" >> \"$FIXTURE_PNPM_RECORD\"\n"
                "done\n",
                encoding="utf-8",
            )
            endpoint.chmod(0o755)
            launcher = root / "app with spaces" / "bin" / "pnpm"
            launcher.parent.mkdir(parents=True)
            module.write_pnpm_launcher(
                launcher,
                node_binary=node.resolve(),
                pnpm_entrypoint=endpoint.resolve(),
            )
            trap_directory = root / "path trap"
            trap_directory.mkdir()
            self._path_trap(trap_directory)
            node_record = root / "node record"
            pnpm_record = root / "pnpm record"
            trap_record = root / "trap record"
            arguments = ("install", "--flag=$literal", "space argument", "", "quote'argument")
            result = subprocess.run(
                [str(launcher), *arguments],
                cwd=root,
                env={
                    "PATH": str(trap_directory),
                    "FIXTURE_NODE_RECORD": str(node_record),
                    "FIXTURE_PNPM_RECORD": str(pnpm_record),
                    "FIXTURE_PATH_TRAP_RECORD": str(trap_record),
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(node_record.read_text(encoding="utf-8").strip(), str(node.resolve()))
            self.assertFalse(trap_record.exists())
            self.assertEqual(
                pnpm_record.read_text(encoding="utf-8").splitlines(),
                ["<install>", "<--flag=$literal>", "<space argument>", "<>", "<quote'argument>"],
            )
            launcher_source = launcher.read_text(encoding="utf-8")
            self.assertNotIn("$PATH", launcher_source)
            self.assertNotIn("/usr/bin/env node", launcher_source)

    def test_preflight_uses_explicit_node_and_checks_supported_versions(self) -> None:
        module = macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            node_directory = root / "node"
            node_directory.mkdir()
            node = self._fake_node(node_directory)
            entrypoint = root / "pnpm.mjs"
            entrypoint.write_text("not evaluated by fake Node\n", encoding="utf-8")
            trap_directory = root / "path trap"
            trap_directory.mkdir()
            self._path_trap(trap_directory)
            trap_record = root / "trap record"

            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(trap_directory),
                    "FIXTURE_PATH_TRAP_RECORD": str(trap_record),
                },
                clear=False,
            ):
                module.validate_pnpm_node_runtime(node.resolve(), entrypoint, "11.25.0")
            self.assertFalse(trap_record.exists())

            old_node = self._fake_node(
                root,
                name="old-node",
                node_version="v22.12.0",
            )
            with self.assertRaisesRegex(SystemExit, "requires Node >= 22.13.0"):
                module.validate_pnpm_node_runtime(old_node.resolve(), entrypoint, "11.25.0")

            wrong_pnpm_node = self._fake_node(
                root,
                name="wrong-pnpm-node",
                pnpm_version="11.25.1",
            )
            with self.assertRaisesRegex(SystemExit, "expected 11.25.0, got 11.25.1"):
                module.validate_pnpm_node_runtime(
                    wrong_pnpm_node.resolve(), entrypoint, "11.25.0"
                )

    def test_configured_node_rejects_missing_relative_and_nonexecutable_paths(self) -> None:
        module = macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            node = self._fake_node(root)
            nonexecutable = root / "not-executable-node"
            nonexecutable.write_text("#!/bin/sh\n", encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "absolute path"):
                module.configured_pnpm_node_binary("node", {})
            with self.assertRaisesRegex(SystemExit, "unavailable"):
                module.configured_pnpm_node_binary(str(root / "missing-node"), {})
            with self.assertRaisesRegex(SystemExit, "not executable"):
                module.configured_pnpm_node_binary(str(nonexecutable), {})
            self.assertEqual(
                module.configured_pnpm_node_binary(
                    None, {module.PNPM_NODE_CONFIG_KEY: str(node)}
                ),
                node.resolve(),
            )
            with mock.patch.object(module, "DEFAULT_PNPM_NODE_BINARY", node):
                self.assertEqual(module.configured_pnpm_node_binary(None, {}), node.resolve())

    def test_preflight_timeout_is_bounded(self) -> None:
        module = macos_installer_module()
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            node = self._fake_node(root, hang=True)
            entrypoint = root / "pnpm.mjs"
            entrypoint.write_text("fixture\n", encoding="utf-8")
            with mock.patch.object(module, "PNPM_PREFLIGHT_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(SystemExit, "timed out after 0.01 seconds"):
                    module.validate_pnpm_node_runtime(node.resolve(), entrypoint, "11.25.0")


if __name__ == "__main__":
    unittest.main()
