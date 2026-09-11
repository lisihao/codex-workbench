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

from codex_workbench.artifacts import ArtifactStore
from codex_workbench import controlled_validation as validation
from codex_workbench.controlled_validation import ValidationRuntime, plan_validation, run_validation


CODEX = os.environ.get("WB_SANDBOX_CODEX") or shutil.which("codex")
_configured_node = os.environ.get("WB_VALIDATION_NODE") or shutil.which("node")
NODE = Path(_configured_node).expanduser() if _configured_node else None
_configured_dsh_source = os.environ.get("WB_VALIDATION_DSH_SOURCE")
DSH_ROOT = Path(_configured_dsh_source).expanduser() if _configured_dsh_source else None
DSH_NODE_MODULES = DSH_ROOT / "node_modules" if DSH_ROOT is not None else None
_PAIRING_SOURCES = (
    "scripts/verify-translation-pairing.ts",
    "scripts/translation-pairing-git.ts",
    "scripts/translation-pairing-record.ts",
    "scripts/translation-pairing.ts",
    "scripts/translation-pairing.manifest.json",
)


def _real_launcher_dependencies_available() -> bool:
    return (
        NODE is not None
        and DSH_ROOT is not None
        and DSH_NODE_MODULES is not None
        and NODE.is_file()
        and os.access(NODE, os.X_OK)
        and (DSH_NODE_MODULES / "vitest" / "vitest.mjs").is_file()
        and (DSH_NODE_MODULES / "tsx" / "dist" / "esm" / "index.mjs").is_file()
        and all((DSH_ROOT / relative).is_file() for relative in _PAIRING_SOURCES)
    )


def _copy_pairing_sources(destination: Path) -> None:
    """Copy the real DSH pairing entrypoint and its fixed relative imports."""

    assert DSH_ROOT is not None
    for relative in _PAIRING_SOURCES:
        source = DSH_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


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

    @unittest.skipUnless(
        _real_launcher_dependencies_available(),
        "requires local Node plus installed DSH Vitest/tsx dependencies",
    )
    def test_real_node_vitest_launcher_runs_fixed_fixture_title_with_json_proof(self) -> None:
        """Prove the launcher only; it does not substitute for B's business tests."""

        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        assert DSH_NODE_MODULES is not None
        assert NODE is not None
        (self.worktree / "node_modules").symlink_to(DSH_NODE_MODULES, target_is_directory=True)
        for path, title in validation._IPC_VITEST_CASES:
            target = self.worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "import { it, expect } from 'vitest'\n"
                "import { writeFileSync } from 'node:fs'\n"
                f"it({json.dumps(title)}, () => {{\n"
                f"  expect(process.cwd()).toBe({json.dumps(str(self.worktree))})\n"
                "  for (const target of ['unrelated-source.txt', '.git/config']) {\n"
                "    let denied = false\n"
                "    try { writeFileSync(target, 'forbidden') }\n"
                "    catch (error) { denied = error && ['EACCES', 'EPERM'].includes(error.code) }\n"
                "    expect(denied).toBe(true)\n"
                "  }\n"
                "})\n",
                encoding="utf-8",
            )
        config_before = (self.worktree / ".git" / "config").read_bytes()
        runtime = ValidationRuntime(Path(str(CODEX)).resolve(), NODE.resolve(), NODE.resolve())
        plan = plan_validation(self.worktree, "dsh-b-ipc-v1", runtime)
        first = plan.commands[0]
        self.assertEqual(first.argv[:3], (
            str(NODE.resolve()),
            str((self.worktree / "node_modules" / "vitest" / "vitest.mjs").resolve()),
            "run",
        ))
        artifacts = ArtifactStore(self.root / "artifacts")
        result = run_validation(plan, artifacts).to_dict()
        logs = "\n".join(
            artifacts.verify(command["stderr_ref"]).read_text(encoding="utf-8")
            for command in result["commands"]
        )
        self.assertTrue(result["ok"], (result, logs))
        first_receipt = result["commands"][0]
        self.assertEqual(first_receipt["test_assertion"]["matched_count"], 1)
        self.assertEqual((self.worktree / ".git" / "config").read_bytes(), config_before)
        self.assertFalse((self.worktree / "unrelated-source.txt").exists())

    @unittest.skipUnless(
        _real_launcher_dependencies_available(),
        "requires local Node plus installed DSH Vitest/tsx dependencies",
    )
    def test_real_node_pairing_entrypoint_writes_and_checks_selected_pairs(self) -> None:
        """Run the real DSH pairing entrypoint in an isolated fixture only."""

        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        assert DSH_NODE_MODULES is not None
        assert NODE is not None
        (self.worktree / "node_modules").symlink_to(DSH_NODE_MODULES, target_is_directory=True)
        _copy_pairing_sources(self.worktree)
        anchors = validation._README_ANCHORS
        for anchor in anchors:
            english = self.worktree / anchor
            english.parent.mkdir(parents=True)
            english.write_text(
                "# English fixture\n\n[中文](README.zh.md)\n", encoding="utf-8"
            )
            english.with_name("README.zh.md").write_text(
                "# 中文 fixture\n\n[English](README.md)\n", encoding="utf-8"
            )
            english.with_name("README.i18n.yaml").write_text("", encoding="utf-8")
        config_before = (self.worktree / ".git/config").read_bytes()
        runtime = ValidationRuntime(Path(str(CODEX)).resolve(), NODE.resolve(), NODE.resolve())
        plan = plan_validation(self.worktree, "dsh-b-pairing-write-v1", runtime)
        loader = (self.worktree / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs").resolve()
        script = (self.worktree / "scripts" / "verify-translation-pairing.ts").resolve()
        self.assertEqual(plan.commands[0].argv[:5], (
            str(NODE.resolve()), "--import", loader.as_uri(), str(script), "--write",
        ))
        artifacts = ArtifactStore(self.root / "artifacts")
        result = run_validation(plan, artifacts).to_dict()
        logs = "\n".join(
            artifacts.verify(command["stderr_ref"]).read_text(encoding="utf-8")
            for command in result["commands"]
        )
        self.assertTrue(result["ok"], (result, logs))
        self.assertEqual(len(result["commands"]), 2)
        self.assertEqual((self.worktree / ".git/config").read_bytes(), config_before)
        for anchor in anchors:
            sidecar = (self.worktree / anchor).with_name("README.i18n.yaml")
            self.assertIn("README.md:", sidecar.read_text(encoding="utf-8"))
