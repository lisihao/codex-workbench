"""Keep frozen handoff materialization compatible with supported pnpm majors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.dirty_worktree_recovery import PnpmOfflineMaterializer


class LockfileMaterializerCompatibilityTests(unittest.TestCase):
    def test_only_pnpm_eleven_receives_its_pm_failure_option(self) -> None:
        for version in ('10.31.0', '11.25.0'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'package.json').write_text(json.dumps({'packageManager': 'pnpm@' + version}))
                (root / 'pnpm-lock.yaml').write_text("lockfileVersion: '9.0'\n")
                calls = []
                def runner(args, **kwargs):
                    calls.append(tuple(args))
                    if args[-1] != '--version':
                        modules = Path(kwargs['cwd']) / 'node_modules'
                        (modules / '.bin').mkdir(parents=True)
                        (modules / '.modules.yaml').write_text('layoutVersion: 5\n')
                    return subprocess.CompletedProcess(args, 0, version + '\n', '')
                with patch.dict(os.environ, {'CODEX_WORKBENCH_PNPM_STORE': ''}):
                    result = PnpmOfflineMaterializer(binary=sys.executable, runner=runner).materialize(root, timeout_seconds=10)
                install = calls[1]
                self.assertIn('--offline', install)
                self.assertIn('--frozen-lockfile', install)
                self.assertIn('--ignore-scripts', install)
                self.assertEqual('--pm-on-fail=ignore' in install, version.startswith('11.'))
                self.assertEqual(result['pnpm_version'], version)
