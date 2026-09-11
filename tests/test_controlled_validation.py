"""Focused policy and runner checks for the fixed controlled validations."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench import controlled_validation as validation
from codex_workbench.artifacts import ArtifactStore
from codex_workbench.config import WorkbenchConfig


class _FakePopen:
    """A deterministic sandbox child that writes only its planned Vitest report."""

    calls: list["_FakePopen"] = []
    mode = "passed"

    def __init__(self, argv, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 42420 + len(type(self).calls)
        self.stdout = io.BytesIO(b"controlled stdout\n")
        self.stderr = io.BytesIO(b"controlled stderr\n")
        self.wait_calls: list[float] = []
        type(self).calls.append(self)
        child = self.argv[self.argv.index("--") + 1:]
        report_argument = next(
            (argument for argument in child if argument.startswith("--outputFile=")),
            None,
        )
        if report_argument is not None and type(self).mode in {"passed", "skipped"}:
            title = child[child.index("-t") + 1]
            report = Path(report_argument.removeprefix("--outputFile="))
            report.write_text(json.dumps({
                "testResults": [{"assertionResults": [{
                    "title": title,
                    "status": type(self).mode,
                }]}],
            }), encoding="utf-8")

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_calls.append(timeout)
        if type(self).mode == "timeout" and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if type(self).mode == "nonzero":
            return 7
        if type(self).mode == "timeout":
            return -signal_term()
        return 0


def signal_term() -> int:
    """Keep the fake child independent of platform-specific process constants."""

    return 15


class ControlledValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wb-controlled-validation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.worktree)], check=True)
        self._write_inputs()
        self.codex = self._executable("codex")
        self.pnpm = self._executable("pnpm")
        self.node = self._executable("node")
        self.runtime = validation.ValidationRuntime(self.codex, self.pnpm, self.node)
        self.artifacts = ArtifactStore(self.root / "artifacts")
        _FakePopen.calls = []
        _FakePopen.mode = "passed"

    def _executable(self, name: str) -> Path:
        target = self.root / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o700)
        return target

    def _write_inputs(self) -> None:
        self.vitest_entrypoint = self.worktree / "node_modules" / "vitest" / "vitest.mjs"
        self.vitest_entrypoint.parent.mkdir(parents=True, exist_ok=True)
        self.vitest_entrypoint.write_text("export {}\n", encoding="utf-8")
        self.tsx_entrypoint = (
            self.worktree / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
        )
        self.tsx_entrypoint.parent.mkdir(parents=True, exist_ok=True)
        self.tsx_entrypoint.write_text("export {}\n", encoding="utf-8")
        self.pairing_entrypoint = self.worktree / "scripts" / "verify-translation-pairing.ts"
        self.pairing_entrypoint.parent.mkdir(parents=True, exist_ok=True)
        self.pairing_entrypoint.write_text("export {}\n", encoding="utf-8")
        for index, anchor in enumerate(validation._README_ANCHORS, start=1):
            source = self.worktree / anchor
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"English {index} {anchor}\n", encoding="utf-8")
            source.with_name("README.zh.md").write_text(
                f"Chinese {index} {anchor}\n", encoding="utf-8"
            )
            source.with_name("README.i18n.yaml").write_text(
                f"pair-{index}: initial\n", encoding="utf-8"
            )
        for path, _title in validation._IPC_VITEST_CASES:
            target = self.worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("describe('fixed fixture', () => {})\n", encoding="utf-8")

    def _run_ipc(self, mode: str = "passed") -> dict[str, object]:
        _FakePopen.calls = []
        _FakePopen.mode = mode
        plan = validation.plan_validation(self.worktree, "dsh-b-ipc-v1", self.runtime)
        with patch.object(validation.subprocess, "Popen", _FakePopen):
            return validation.run_validation(plan, self.artifacts).to_dict()

    def test_plan_is_a_static_whitelist_with_precise_pairing_grants(self) -> None:
        ipc = validation.plan_validation(self.worktree, "dsh-b-ipc-v1", self.runtime)
        self.assertTrue(ipc.allow_unix_socket)
        self.assertEqual(len(ipc.commands), 6)
        vitest = self.vitest_entrypoint.resolve()
        for number, (command, (_path, title)) in enumerate(
            zip(ipc.commands, validation._IPC_VITEST_CASES, strict=True), start=1
        ):
            self.assertEqual(command.argv[:3], (str(self.runtime.node_binary), str(vitest), "run"))
            self.assertEqual(command.expected_test_title, title)
            self.assertEqual(command.report_file, f"<private-scratch>/case-{number}.json")
            self.assertIn("--reporter=json", command.argv)
            self.assertNotIn("--cache", command.argv)
            self.assertIn("--no-cache", command.argv)
            self.assertIn("--configLoader=runner", command.argv)
            self.assertEqual(
                command.to_dict()["entrypoint_sha256"],
                {str(vitest): sha256(vitest.read_bytes()).hexdigest()},
            )
        self.assertEqual(ipc.to_dict()["sandbox"]["private_scratch"], "<private-scratch>")

        write = validation.plan_validation(self.worktree, "dsh-b-pairing-write-v1", self.runtime)
        check = validation.plan_validation(self.worktree, "dsh-b-pairing-check-v1", self.runtime)
        self.assertTrue(check.allow_unix_socket)
        self.assertEqual(len(write.readme_bindings), 5)
        self.assertEqual(len(write.commands), 2)
        loader = self.tsx_entrypoint.resolve()
        script = self.pairing_entrypoint.resolve()
        prefix = (str(self.runtime.node_binary), "--import", loader.as_uri(), str(script))
        self.assertEqual(write.commands[0].argv[:5], (*prefix, "--write"))
        self.assertEqual(write.commands[1].argv[:4], prefix)
        self.assertEqual(write.commands[0].argv[5:], validation._README_ANCHORS)
        self.assertEqual(write.commands[1].argv[4:], validation._README_ANCHORS)
        expected_entrypoints = {
            str(loader): sha256(loader.read_bytes()).hexdigest(),
            str(script): sha256(script.read_bytes()).hexdigest(),
        }
        self.assertEqual(write.commands[0].to_dict()["entrypoint_sha256"], expected_entrypoints)
        self.assertEqual(write.commands[1].to_dict()["entrypoint_sha256"], expected_entrypoints)
        assert write.git_common_dir is not None
        self.assertNotIn(write.git_common_dir, write.grants)
        self.assertNotIn(write.git_common_dir / "refs", write.grants)
        self.assertNotIn(write.git_common_dir / "refs" / "heads", write.grants)
        for binding in write.readme_bindings:
            self.assertIn(write.worktree / binding.sidecar_path, write.grants)
            for blob in (binding.source_blob_sha1, binding.translated_blob_sha1):
                self.assertIn(write.git_common_dir / "objects" / blob[:2], write.grants)
                ref = write.git_common_dir / "refs" / "dsh" / "translation-pairing" / "snapshots" / blob
                self.assertIn(ref, write.grants)
                self.assertIn(ref.with_name(ref.name + ".lock"), write.grants)

    def test_only_fixed_checks_paths_and_non_symlink_inputs_are_accepted(self) -> None:
        with self.assertRaisesRegex(validation.ControlledValidationError, "unsupported"):
            validation.plan_validation(self.worktree, "sh -c anything", self.runtime)
        with self.assertRaisesRegex(validation.ControlledValidationError, "invalid"):
            validation._safe_relative_path(self.worktree, "../outside")

        sidecar = self.worktree / validation._README_ANCHORS[0]
        sidecar = sidecar.with_name("README.i18n.yaml")
        outside = self.root / "outside.i18n.yaml"
        outside.write_text("outside\n", encoding="utf-8")
        sidecar.unlink()
        sidecar.symlink_to(outside)
        with self.assertRaisesRegex(validation.ControlledValidationError, "symlink"):
            validation.plan_validation(self.worktree, "dsh-b-pairing-check-v1", self.runtime)

    def test_runtime_manifest_identity_is_static_and_rechecked_before_running(self) -> None:
        config = WorkbenchConfig(state_root=self.root / "state", deployment_role="authority")
        config.install_manifest.parent.mkdir(parents=True)
        config.install_manifest.write_text(json.dumps({
            "codex_binary": str(self.codex),
            "pnpm_recovery_runtime": {
                "binary": str(self.pnpm),
                "node_binary": str(self.node),
            },
        }), encoding="utf-8")
        runtime = validation.resolve_runtime(config)
        identity = runtime.to_dict()["executable_identity"]
        self.assertEqual(identity["pnpm"]["st_ino"], self.pnpm.stat().st_ino)
        plan = validation.plan_validation(self.worktree, "dsh-b-ipc-v1", runtime)
        replacement = self.root / "node-replacement"
        replacement.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        replacement.chmod(0o700)
        os.replace(replacement, self.node)
        with patch.object(validation.subprocess, "Popen") as popen:
            result = validation.run_validation(plan, self.artifacts).to_dict()
        self.assertFalse(result["ok"])
        self.assertIn("node_binary changed", result["error"])
        popen.assert_not_called()

    def test_missing_or_drifted_entrypoint_never_reaches_subprocess(self) -> None:
        self.vitest_entrypoint.unlink()
        with self.assertRaisesRegex(validation.ControlledValidationError, "entrypoint"):
            validation.plan_validation(self.worktree, "dsh-b-ipc-v1", self.runtime)

        self.vitest_entrypoint.write_text("export {}\n", encoding="utf-8")
        plan = validation.plan_validation(self.worktree, "dsh-b-ipc-v1", self.runtime)
        self.vitest_entrypoint.write_text("export { changed }\n", encoding="utf-8")
        with patch.object(validation.subprocess, "Popen") as popen:
            result = validation.run_validation(plan, self.artifacts).to_dict()
        self.assertFalse(result["ok"])
        self.assertIn("plan changed", result["error"])
        popen.assert_not_called()

        self.tsx_entrypoint.unlink()
        with self.assertRaisesRegex(validation.ControlledValidationError, "entrypoint"):
            validation.plan_validation(self.worktree, "dsh-b-pairing-check-v1", self.runtime)

    def test_runner_uses_private_environment_exact_argv_and_closes_pipes(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "HTTP_PROXY": "http://proxy"}):
            result = self._run_ipc()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(_FakePopen.calls), 6)
        environment = _FakePopen.calls[0].kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertEqual(environment["HOME"], environment["TMPDIR"] + "/home")
        self.assertEqual(environment["DSH_HOME"], environment["TMPDIR"] + "/dsh")
        for process in _FakePopen.calls:
            self.assertFalse(process.kwargs["shell"])
            self.assertTrue(process.kwargs["start_new_session"])
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertLessEqual(process.wait_calls[0], 60)
            self.assertIn("--allow-unix-socket", process.argv)
            profile = process.argv[process.argv.index("-c") + 1]
            self.assertIn("network={enabled=false}", profile)
            self.assertNotIn("node_modules", profile)
        command = result["commands"][0]
        self.assertEqual(command["test_assertion"]["matched_count"], 1)
        self.assertTrue(self.artifacts.verify(command["stdout_ref"]).is_file())

    def test_zero_exit_skip_nonzero_and_timeout_are_failed_receipts(self) -> None:
        skipped = self._run_ipc("skipped")
        self.assertFalse(skipped["ok"])
        self.assertEqual(skipped["commands"][0]["exit_code"], 0)
        self.assertIn("did not pass", skipped["commands"][0]["error"])
        self.assertTrue(skipped["commands"][1]["error"].startswith("skipped"))

        nonzero = self._run_ipc("nonzero")
        self.assertFalse(nonzero["ok"])
        self.assertEqual(nonzero["commands"][0]["exit_code"], 7)
        self.assertIsNone(nonzero["commands"][0]["error"])

        with patch.object(validation.os, "killpg") as killpg:
            timed_out = self._run_ipc("timeout")
        self.assertFalse(timed_out["ok"])
        self.assertTrue(timed_out["commands"][0]["timed_out"])
        self.assertEqual(killpg.call_args.args[0], _FakePopen.calls[0].pid)

    def test_tampered_plan_never_reaches_subprocess(self) -> None:
        plan = validation.plan_validation(self.worktree, "dsh-b-ipc-v1", self.runtime)
        tampered = replace(
            plan,
            commands=(validation.ValidationCommand("arbitrary", ("/bin/sh", "-c", "id")),),
        )
        with patch.object(validation.subprocess, "Popen") as popen:
            result = validation.run_validation(tampered, self.artifacts).to_dict()
        self.assertFalse(result["ok"])
        self.assertIn("plan changed", result["error"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
