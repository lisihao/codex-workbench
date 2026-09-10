from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.executors import ExecutionRequest
from codex_workbench.model import NodeResult
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


class FailurePathObservationTests(unittest.TestCase):
    """Observed failure paths must keep source edits separate from ignored residue."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        (self.repository / "lib").mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"],
            cwd=self.repository,
            check=True,
        )
        (self.repository / ".gitignore").write_text(
            "node_modules/\n__pycache__/\n", encoding="utf-8"
        )
        (self.repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
        (self.repository / "lib" / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "src/value.txt", "lib/tracked.txt"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        self.base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.state_root = self.root / "state"
        config = WorkbenchConfig(
            self.state_root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        config.initialize()
        self.store = WorkbenchStore(config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("failure-observation", "fixture-machine")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(self) -> ExecutionRequest:
        return ExecutionRequest(
            task_id="failure-observation",
            node_id="worker",
            attempt=1,
            contract={
                "base_sha": self.base_sha,
                "external_write_permission": False,
                "destructive_action_permission": False,
            },
            spec={},
            worktree=self.repository,
            input_tree_sha=self.base_sha,
        )

    def test_ignored_pnpm_residue_stays_out_of_changed_paths_with_bounded_evidence(self) -> None:
        (self.repository / "src" / "new.ts").write_text("source delta\n", encoding="utf-8")
        (self.repository / "lib" / "tracked.txt").write_text("tracked delta\n", encoding="utf-8")
        residue = self.repository / "node_modules" / "lib" / ".pnpm-store"
        residue.mkdir(parents=True)
        for index in range(12):
            (residue / f"entry-{index:02d}.json").write_text("ignored\n", encoding="utf-8")

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            observed = coordinator._with_observed_failure_paths(
                self._request(),
                NodeResult("blocked", "worker stopped after local source changes"),
            )
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(observed.changed_paths, ("lib/tracked.txt", "src/new.ts"))
        self.assertFalse(observed.retryable)
        residue_ref = observed.artifacts["ignored-worktree-residue"]
        receipt = json.loads(self.store.artifacts.verify(residue_ref).read_text(encoding="utf-8"))
        self.assertEqual(receipt["kind"], "ignored-worktree-residue")
        self.assertEqual(receipt["ignored_path_count"], 12)
        self.assertEqual(len(receipt["sample_paths"]), 8)
        self.assertEqual(len(receipt["paths_sha256"]), 64)
        self.assertNotIn("entry-11.json", json.dumps(receipt, sort_keys=True))

    def test_known_python_bytecode_remains_a_recovery_path(self) -> None:
        (self.repository / "src" / "value.txt").write_text("source delta\n", encoding="utf-8")
        bytecode = self.repository / "tests" / "__pycache__"
        bytecode.mkdir(parents=True)
        (bytecode / "fixture.cpython-313.pyc").write_bytes(b"bytecode")

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            observed = coordinator._with_observed_failure_paths(
                self._request(),
                NodeResult("blocked", "worker stopped after local source changes"),
            )
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed.changed_paths,
            ("src/value.txt", "tests/__pycache__/fixture.cpython-313.pyc"),
        )
        self.assertTrue(observed.retryable)
        self.assertNotIn("ignored-worktree-residue", observed.artifacts)
