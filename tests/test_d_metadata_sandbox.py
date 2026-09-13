"""Native leaf-grant evidence; no model or production task is involved."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


CODEX = os.environ.get("WB_SANDBOX_CODEX") or shutil.which("codex")


@unittest.skipUnless(sys.platform == "darwin" and CODEX, "requires macOS Codex sandbox")
class DMetadataSandboxTests(unittest.TestCase):
    def test_exact_missing_note_leaves_allow_creation_not_adjacent_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb-note-sb-") as directory:
            root = Path(directory).resolve()
            worktree = root / "repo"
            scratch = root / "scratch"
            scratch.mkdir()
            notes = worktree / ".agents/notes/implemented/feature"
            notes.mkdir(parents=True)
            (worktree / ".agents/skills").mkdir()
            (worktree / ".git").mkdir()
            existing = {
                "AGENTS.md": "fixture policy\n",
                ".agents/skills/SKILL.md": "fixture skill\n",
                ".git/config": "fixture git configuration\n",
            }
            for name, content in existing.items():
                (worktree / name).write_text(content)
            anchor = notes / "2026-09-13-fixture.md"
            chinese = notes / "2026-09-13-fixture.zh.md"
            sidecar = notes / "2026-09-13-fixture.i18n.yaml"
            paths = (anchor, chinese, sidecar)
            filesystem = {
                ":root": "read", ":tmpdir": "read", ":slash_tmp": "read",
                str(scratch): "write", **{str(path): "write" for path in paths},
            }
            fields = ",".join(json.dumps(key) + "=" + json.dumps(value)
                              for key, value in filesystem.items())
            profile = "permissions.wb-d-metadata-fixture={filesystem={" + fields + "},network={enabled=false}}"
            code = r'''
import errno, json, pathlib
root = pathlib.Path.cwd()
directory = root / '.agents/notes/implemented/feature'
result = {}
for name in ('2026-09-13-fixture.md', '2026-09-13-fixture.zh.md', '2026-09-13-fixture.i18n.yaml'):
    (directory / name).write_text('selected fixture\n')
    result[name] = 'created'
for name in ('AGENTS.md', '.agents/skills/SKILL.md', '.git/config',
             '.agents/notes/implemented/feature/unselected.md'):
    try:
        (root / name).write_text('forbidden')
        result[name] = 'unexpectedly allowed'
    except OSError as error:
        result[name] = error.errno
print(json.dumps(result))
'''
            result = subprocess.run(
                [str(CODEX), "sandbox", "-P", "wb-d-metadata-fixture", "-c", profile,
                 "-C", str(worktree), "--", sys.executable, "-c", code],
                env={**os.environ, "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch)},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            observed = json.loads(result.stdout)
            for path in paths:
                self.assertEqual(observed.pop(path.name), "created")
                self.assertEqual(path.read_text(), "selected fixture\n")
            self.assertTrue(all(value in (errno.EACCES, errno.EPERM)
                                for value in observed.values()), observed)
            for name, content in existing.items():
                self.assertEqual((worktree / name).read_text(), content)
            self.assertFalse((notes / "unselected.md").exists())


if __name__ == "__main__":
    unittest.main()
