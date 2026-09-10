"""Real macOS containment checks for the operator-only validation recipe.

These run commands, never a model. Linux CI does not provide Seatbelt; macOS
release verification selects the same pinned Codex CLI as the authority.
"""
from __future__ import annotations

import errno
from hashlib import sha1
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


CODEX = os.environ.get("WB_SANDBOX_CODEX") or shutil.which("codex")


@unittest.skipUnless(sys.platform == "darwin" and CODEX, "requires macOS and Codex sandbox CLI")
class ControlledValidationSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wb-sb-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        (self.worktree / ".codex").mkdir()
        self.scratch = self.root / "tmp"
        self.scratch.mkdir()

    def _run(self, code: str, *, writes: tuple[Path, ...] = (), sockets: bool = False):
        filesystem = {
            ":root": "read", ":tmpdir": "read", ":slash_tmp": "read",
            str(self.scratch): "write", **{str(path): "write" for path in writes},
        }
        entries = ",".join(json.dumps(key) + "=" + json.dumps(value)
                           for key, value in filesystem.items())
        # A complete inline table avoids dotted-key quoting ambiguity and
        # replaces all fields instead of inheriting user-configured grants.
        profile = "permissions.wb-controlled-validation={filesystem={" + entries + "},network={enabled=false}}"
        argv = [str(CODEX), "sandbox", "-P", "wb-controlled-validation", "-c", profile,
                "-C", str(self.worktree)]
        if sockets:
            argv += ["--allow-unix-socket", str(self.scratch)]
        argv += ["--", sys.executable, "-c", code]
        environment = {**os.environ, "TMPDIR": str(self.scratch), "TMP": str(self.scratch),
                       "TEMP": str(self.scratch)}
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_scoped_unix_socket_roundtrip_denies_other_sockets_and_tcp(self) -> None:
        blocked = socket.socket(socket.AF_UNIX)
        blocked.bind(str(self.root / "other.sock"))
        blocked.listen(1)
        self.addCleanup(blocked.close)
        result = self._run(r'''
import json, os, pathlib, socket
root = pathlib.Path.cwd().parent
path = str(pathlib.Path(os.environ["TMPDIR"]) / "a.sock")
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(1)
client = socket.socket(socket.AF_UNIX)
client.connect(path)
peer, _ = server.accept()
client.sendall(b"ipc-ok")
result = {"roundtrip": peer.recv(16).decode()}
for name, family, operation, address in (
    ("other_unix_bind", socket.AF_UNIX, "bind", str(root / "no.sock")),
    ("other_unix_connect", socket.AF_UNIX, "connect", str(root / "other.sock")),
    ("tcp_bind", socket.AF_INET, "bind", ("127.0.0.1", 0)),
):
    probe = socket.socket(family)
    try:
        getattr(probe, operation)(address)
        result[name] = "unexpectedly allowed"
    except OSError as error:
        result[name] = error.errno
    finally:
        probe.close()
for name in ("source.txt", ".codex/config.toml"):
    try:
        (pathlib.Path.cwd() / name).write_text("forbidden")
        result[name] = "unexpectedly allowed"
    except OSError as error:
        result[name] = error.errno
print(json.dumps(result))
''', sockets=True)
        self.assertEqual(result.pop("roundtrip"), "ipc-ok")
        self.assertTrue(all(value in (errno.EPERM, errno.EACCES) for value in result.values()), result)

    def test_pairing_blob_and_exact_ref_work_without_git_config_or_branch_access(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        content = b"paired documentation fixture\n"
        digest = sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        common = self.worktree / ".git"
        refs = common / "refs/dsh/translation-pairing/snapshots"
        refs.mkdir(parents=True)
        config = (common / "config").read_bytes()
        result = self._run(r'''
import json, pathlib, subprocess
root = pathlib.Path.cwd()
result = {}
blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                      input=b"paired documentation fixture\n", capture_output=True)
digest = blob.stdout.decode().strip()
result["blob"] = blob.returncode
result["digest"] = digest
for name, ref in (("snapshot", "refs/dsh/translation-pairing/snapshots/" + digest),
                  ("other_snapshot", "refs/dsh/translation-pairing/snapshots/unselected"),
                  ("branch", "refs/heads/forbidden")):
    completed = subprocess.run(["git", "update-ref", ref, digest], capture_output=True)
    result[name] = completed.returncode
try:
    (root / ".git/config").write_text("forbidden")
    result["config"] = "unexpectedly allowed"
except OSError as error:
    result["config"] = error.errno
print(json.dumps(result))
''', writes=(common / "objects" / digest[:2], refs / digest, refs / (digest + ".lock")))
        self.assertEqual(result["blob"], 0, result)
        self.assertEqual(result["digest"], digest)
        self.assertEqual(result["snapshot"], 0, result)
        self.assertNotEqual(result["other_snapshot"], 0, result)
        self.assertNotEqual(result["branch"], 0, result)
        self.assertIn(result["config"], (errno.EPERM, errno.EACCES))
        self.assertEqual((common / "config").read_bytes(), config)
        self.assertEqual(subprocess.check_output(
            ["git", "-C", str(self.worktree), "cat-file", "blob", digest]), content)
