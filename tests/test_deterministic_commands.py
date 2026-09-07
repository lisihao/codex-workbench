from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.executors import DeterministicExecutor, ExecutionRequest
from codex_workbench.model import deterministic_acceptance_command_index


class DeterministicCommandPolicyTests(unittest.TestCase):
    def test_contract_index_uses_exact_argv_not_prefix_or_shell_equivalence(self) -> None:
        commands = ("python -m unittest", "git diff --check")

        self.assertEqual(
            deterministic_acceptance_command_index(
                ("git", "diff", "--check"),
                commands,
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "must exactly match"):
            deterministic_acceptance_command_index(
                ("/bin/sh", "-c", "git diff --check"),
                commands,
            )

    def test_executor_rejects_undeclared_argv_before_process_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeterministicExecutor(ArtifactStore(root / "artifacts"))
            request = ExecutionRequest(
                task_id="task",
                node_id="mechanical",
                attempt=1,
                contract={
                    "acceptance_commands": ["python -m unittest"],
                    "timeout_seconds": 30,
                },
                spec={
                    "command": ["/bin/sh", "-c", "touch /tmp/planner-controlled"],
                    "verifier": False,
                },
                worktree=root,
            )

            with patch.object(executor, "_run") as run:
                with self.assertRaisesRegex(ValueError, "must exactly match"):
                    executor.execute(request)

            run.assert_not_called()

    def test_executor_runs_exact_declared_argv(self) -> None:
        command = (sys.executable, "-c", "print('ok')")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = DeterministicExecutor(ArtifactStore(root / "artifacts"))
            request = ExecutionRequest(
                task_id="task",
                node_id="mechanical",
                attempt=1,
                contract={
                    "acceptance_commands": [shlex.join(command)],
                    "timeout_seconds": 30,
                },
                spec={"command": list(command), "verifier": False},
                worktree=root,
            )
            completed = subprocess.CompletedProcess(list(command), 0, "ok\n", "")

            with patch.object(executor, "_run", return_value=(completed, {})) as run:
                result = executor.execute(request)

            self.assertEqual(result.status, "succeeded")
            run.assert_called_once_with(list(command), cwd=root, timeout=30)


if __name__ == "__main__":
    unittest.main()
