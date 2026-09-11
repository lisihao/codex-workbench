from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.dirty_worktree_recovery import (
    CommandOutcome,
    DirtyWorktreeRecovery,
    PnpmOfflineMaterializer,
)
from codex_workbench.worktrees import WorktreeManager


class _FixtureMaterializer(PnpmOfflineMaterializer):
    """Return a bounded, already-verified pnpm receipt without running pnpm."""

    def __init__(self) -> None:
        self.receipt = {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "package_manager": "pnpm@11.25.0",
            "pnpm_version": "11.25.0",
            "lockfile_sha256": "a" * 64,
            "template": {"state": "hit", "key": "b" * 64},
            "commands": [
                {
                    "command": ["pnpm", "--version"],
                    "exit_code": 0,
                    "stdout": "11.25.0\\n",
                    "stderr": "",
                }
            ],
        }

    def materialize(self, _worktree: Path, *, timeout_seconds: int) -> dict[str, object]:
        assert timeout_seconds > 0
        return json.loads(json.dumps(self.receipt))


class RecoveryAcceptanceFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "Fixture")
        (self.repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
        for name in ("fixture.mjs", "second.mjs", "other.mjs"):
            (self.repository / name).write_text(
                f"export const fixture = {name!r};\n", encoding="utf-8"
            )
        self._git("add", "src/value.txt", "fixture.mjs", "second.mjs", "other.mjs")
        self._git("commit", "-m", "base")
        self.base_sha = self._git("rev-parse", "HEAD")
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.worktrees = WorktreeManager(self.root / "worktrees")
        self.materializer = _FixtureMaterializer()
        self.runner_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs.get("env")
            assert isinstance(environment, dict)
            self.runner_calls.append((tuple(args), dict(environment)))
            return subprocess.CompletedProcess(args, 0, "fixture acceptance passed\n", "")

        self.recovery = DirtyWorktreeRecovery(
            self.artifacts,
            self.worktrees,
            materializer=self.materializer,
            runner=runner,
        )
        self.runtime_bin = self.root / "runtime-bin"
        self.runtime_bin.mkdir()
        self.node = self.runtime_bin / "node"
        self._write_runtime("#!/bin/sh\nexit 0\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def _write_runtime(self, contents: str) -> None:
        self.node.write_text(contents, encoding="utf-8")
        self.node.chmod(self.node.stat().st_mode | stat.S_IXUSR)

    def _child_environment(self) -> dict[str, str]:
        return {
            "PATH": str(self.runtime_bin),
            "HOME": "/private/secret-home-must-not-be-recorded",
            "OPENAI_API_KEY": "must-not-be-recorded",
        }

    def _capture_recovery(self) -> tuple[Path, dict[str, object]]:
        source = self.worktrees.prepare(
            str(self.repository), self.base_sha, "fingerprint", "worker", 1
        )
        (source / "src" / "value.txt").write_text("patched\n", encoding="utf-8")
        recovery = self.recovery.capture(
            repository=str(self.repository),
            base_sha=self.base_sha,
            worktree=str(source),
            branch=self.worktrees.branch_name("fingerprint", "worker", 1),
            attempt=1,
            expected_changed_paths=("src/value.txt",),
        )
        return source, recovery

    def _run_with_fixed_environment(
        self,
        declared: tuple[str, ...],
        *,
        evidence_inputs: dict[str, object],
        prior_evidence_fingerprints: tuple[str, ...] = (),
    ) -> CommandOutcome:
        with patch(
            "codex_workbench.dirty_worktree_recovery.codex_subscription_environment",
            side_effect=lambda **_kwargs: self._child_environment(),
        ):
            return self.recovery._run_command(
                declared,
                self.repository,
                5,
                evidence_inputs=evidence_inputs,
                prior_evidence_fingerprints=prior_evidence_fingerprints,
            )

    def test_prepare_logs_bound_recovery_inputs_without_private_environment(self) -> None:
        source, recovery = self._capture_recovery()
        target = self.worktrees.prepare(
            str(self.repository), self.base_sha, "fingerprint", "worker", 2
        )
        with patch(
            "codex_workbench.dirty_worktree_recovery.codex_subscription_environment",
            side_effect=lambda **_kwargs: self._child_environment(),
        ):
            outcome = self.recovery.prepare(
                repository=str(self.repository),
                source_worktree=str(source),
                target_worktree=str(target),
                target_branch=self.worktrees.branch_name("fingerprint", "worker", 2),
                target_attempt=2,
                recovery=recovery,
                acceptance_commands=("node fixture.mjs", "node second.mjs"),
                timeout_seconds=5,
            )

        self.assertEqual(outcome.status, "succeeded", outcome.summary)
        log = json.loads(
            self.artifacts.verify(outcome.artifacts["test-log"]).read_text(encoding="utf-8")
        )
        command = log["acceptance_commands"][0]
        second_command = log["acceptance_commands"][1]
        evidence = command["evidence_inputs"]
        self.assertEqual(evidence["recovery"]["comparison_tree"], self._git("rev-parse", f"{self.base_sha}^{{tree}}"))
        self.assertEqual(evidence["recovery"]["base_sha"], self.base_sha)
        self.assertEqual(evidence["recovery"]["patch_sha256"], recovery["patch_sha256"])
        self.assertIsNone(evidence["recovery"]["dependency_input_ref"])
        self.assertEqual(evidence["materialization"]["lockfile_sha256"], "a" * 64)
        self.assertEqual(evidence["materialization"]["template"], {"state": "hit", "key": "b" * 64})
        self.assertRegex(command["evidence_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertTrue(evidence["coverage"]["complete"])
        self.assertEqual(evidence["command"]["declared_argv"], ["node", "fixture.mjs"])
        self.assertEqual(evidence["command"]["effective_argv"], ["node", "fixture.mjs"])
        self.assertEqual(set(evidence["runtime"]["node"]["stat"]), {"dev", "ino", "size", "mtime_ns"})
        self.assertEqual(
            evidence["runtime"]["node_script_entry"]["status"], "resolved"
        )
        self.assertRegex(
            evidence["runtime"]["node_script_entry"]["launcher_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertNotIn("HOME", evidence["environment"])
        self.assertNotIn("OPENAI_API_KEY", evidence["environment"])
        self.assertEqual(evidence["execution_scope"]["cross_worktree_reuse"], "not-supported")
        self.assertEqual(
            evidence["coverage"]["scope"]["kind"],
            "recorded-recovery-command-inputs-v1",
        )
        self.assertEqual(
            evidence["coverage"]["scope"]["dependency_closure"], "not-recorded"
        )
        self.assertEqual(
            evidence["coverage"]["scope"]["environment"], "fixed-child-fields-only"
        )
        self.assertFalse(evidence["coverage"]["reuse_authorized"])
        self.assertEqual(
            second_command["evidence_inputs"]["prior_successful_command_fingerprints"],
            [command["evidence_fingerprint"]],
        )

    def test_fingerprint_binds_patch_command_runtime_and_success_chain(self) -> None:
        source, recovery = self._capture_recovery()
        inputs = self.recovery._acceptance_evidence_inputs(
            comparison_tree=self._git("rev-parse", f"{self.base_sha}^{{tree}}"),
            recovery=recovery,
            materialization=self.materializer.receipt,
        )
        first = self._run_with_fixed_environment(("node", "fixture.mjs"), evidence_inputs=inputs)
        second = self._run_with_fixed_environment(("node", "fixture.mjs"), evidence_inputs=inputs)
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)

        (source / "src" / "value.txt").write_text("different source patch\n", encoding="utf-8")
        changed_recovery = self.recovery.capture(
            repository=str(self.repository),
            base_sha=self.base_sha,
            worktree=str(source),
            branch=self.worktrees.branch_name("fingerprint", "worker", 1),
            attempt=1,
            expected_changed_paths=("src/value.txt",),
        )
        patch_changed = self.recovery._acceptance_evidence_inputs(
            comparison_tree=self._git("rev-parse", f"{self.base_sha}^{{tree}}"),
            recovery=changed_recovery,
            materialization=self.materializer.receipt,
        )
        self.assertNotEqual(
            first.evidence_fingerprint,
            self._run_with_fixed_environment(("node", "fixture.mjs"), evidence_inputs=patch_changed).evidence_fingerprint,
        )
        self.assertNotEqual(
            first.evidence_fingerprint,
            self._run_with_fixed_environment(("node", "other.mjs"), evidence_inputs=inputs).evidence_fingerprint,
        )
        self._write_runtime("#!/bin/sh\nprintf '%s\\n' changed\nexit 0\n")
        runtime_changed = self._run_with_fixed_environment(("node", "fixture.mjs"), evidence_inputs=inputs)
        self.assertNotEqual(first.evidence_fingerprint, runtime_changed.evidence_fingerprint)
        (self.repository / "fixture.mjs").write_text(
            "export const fixture = 'changed';\n", encoding="utf-8"
        )
        script_changed = self._run_with_fixed_environment(
            ("node", "fixture.mjs"), evidence_inputs=inputs
        )
        self.assertNotEqual(runtime_changed.evidence_fingerprint, script_changed.evidence_fingerprint)
        chained = self._run_with_fixed_environment(
            ("node", "fixture.mjs"),
            evidence_inputs=inputs,
            prior_evidence_fingerprints=(first.evidence_fingerprint,),
        )
        assert chained.evidence_inputs is not None
        self.assertEqual(
            chained.evidence_inputs["prior_successful_command_fingerprints"],
            [first.evidence_fingerprint],
        )
        self.assertNotEqual(first.evidence_fingerprint, chained.evidence_fingerprint)

    def test_incomplete_runtime_coverage_is_explicit(self) -> None:
        _source, recovery = self._capture_recovery()
        inputs = self.recovery._acceptance_evidence_inputs(
            comparison_tree=self._git("rev-parse", f"{self.base_sha}^{{tree}}"),
            recovery=recovery,
            materialization=self.materializer.receipt,
        )
        with patch(
            "codex_workbench.dirty_worktree_recovery.codex_subscription_environment",
            return_value={"PATH": ""},
        ):
            outcome = self.recovery._run_command(
                ("missing-command",), self.repository, 5, evidence_inputs=inputs
            )
        assert outcome.evidence_inputs is not None
        self.assertFalse(outcome.evidence_inputs["coverage"]["complete"])
        self.assertIn("command entry", outcome.evidence_inputs["coverage"]["incomplete_reasons"])

    def test_private_pnpm_shim_does_not_make_identical_inputs_drift(self) -> None:
        _source, recovery = self._capture_recovery()
        inputs = self.recovery._acceptance_evidence_inputs(
            comparison_tree=self._git("rev-parse", f"{self.base_sha}^{{tree}}"),
            recovery=recovery,
            materialization=self.materializer.receipt,
        )
        pnpm_runtime = self.runtime_bin / "pnpm-runtime"
        pnpm_runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pnpm_runtime.chmod(pnpm_runtime.stat().st_mode | stat.S_IXUSR)
        with patch.dict(
            os.environ,
            {
                "PATH": str(self.runtime_bin),
                "CODEX_WORKBENCH_PNPM": str(pnpm_runtime),
            },
            clear=True,
        ):
            first = self.recovery._run_command(
                ("pnpm", "--version"), self.repository, 5, evidence_inputs=inputs
            )
            second = self.recovery._run_command(
                ("pnpm", "--version"), self.repository, 5, evidence_inputs=inputs
            )
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)
        assert first.evidence_inputs is not None
        self.assertEqual(
            first.evidence_inputs["environment"]["fixed"]["ZDOTDIR"],
            "private-recovery-pnpm-shim",
        )

    def test_legacy_command_outcome_omits_optional_evidence_fields(self) -> None:
        legacy = CommandOutcome(("git", "diff", "--check"), 0, "", "")
        self.assertEqual(
            legacy.to_dict(),
            {
                "command": ["git", "diff", "--check"],
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
