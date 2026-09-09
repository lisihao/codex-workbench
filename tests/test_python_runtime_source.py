"""The declared launcher cannot accidentally validate another installed copy."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PythonRuntimeSourceTests(unittest.TestCase):
    def test_launcher_owns_package_precedence_without_removing_other_python_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "checkout" / "scripts"
            scripts.mkdir(parents=True)
            launcher = scripts / "python-runtime"
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "python-runtime", launcher)
            for path, marker in ((root / "checkout" / "src", "current"), (root / "installed", "stale")):
                package = path / "codex_workbench"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text(f"marker = {marker!r}\n")
            (root / "installed" / "fixture_extra.py").write_text("marker = 'preserved'\n")
            environment = {**os.environ, "CODEX_WORKBENCH_PYTHON": sys.executable,
                           "PYTHONPATH": str(root / "installed"), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([str(launcher), "-c", "import codex_workbench, fixture_extra; print(codex_workbench.marker, fixture_extra.marker)"],
                                    cwd=root, env=environment, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "current preserved")
