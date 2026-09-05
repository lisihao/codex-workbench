from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.executors import CodexExecutor, ExecutionRequest, ProcessExecutor
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


class PhysicalWorktreeExecutionTests(unittest.TestCase):
    @staticmethod
    def _repository(root: Path) -> tuple[Path, str]:
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        (repository / "checked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "checked.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repository, base_sha

    def test_service_persists_and_executes_with_physical_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_sha = self._repository(root)
            state_root = root / "state"
            state_root.mkdir()
            physical_worktrees = root / "physical-worktrees"
            physical_worktrees.mkdir()
            (state_root / "worktrees").symlink_to(physical_worktrees, target_is_directory=True)
            store = WorkbenchStore(state_root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("physical-worktree", "test-machine")
            contract = TaskContract(
                task_id="physical-worktree",
                repository=str(repository),
                base_sha=base_sha,
                objective="exercise physical worktree execution",
                allowed_scope=("checked.txt",),
            )
            node = NodeSpec(
                "worker",
                contract.task_id,
                "physical worktree worker",
                "codex",
                "gpt-5.6-luna",
                "Return the fake structured result.",
                write_scopes=("checked.txt",),
            )
            verifier = NodeSpec(
                "verify",
                contract.task_id,
                "fixture verifier",
                "fixture",
                "fixture",
                "accepted",
                depends_on=(node.node_id,),
                verifier=True,
            )
            store.create_task(contract, [node, verifier], "physical-worktree-create")
            store.queue_task(contract.task_id)
            coordinator = Coordinator(store, state_root, coordinator_epoch=epoch)
            try:
                claimed = coordinator._claim_next_ready_node("physical-worktree-worker")
                self.assertIsNotNone(claimed)
                assert claimed is not None
                captured: dict[str, object] = {}
                original_command = CodexExecutor._command

                def capture_command(
                    binary: str,
                    request: ExecutionRequest,
                    schema_path: Path,
                    output_path: Path,
                ) -> list[str]:
                    captured["request_worktree"] = request.worktree
                    return original_command(binary, request, schema_path, output_path)

                def fake_run(
                    _executor: ProcessExecutor,
                    command: list[str],
                    *,
                    cwd: Path,
                    timeout: int,
                    input_text: str | None = None,
                    environment: dict[str, str] | None = None,
                ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
                    captured["command"] = command
                    captured["cwd"] = cwd
                    output_path = Path(command[command.index("--output-last-message") + 1])
                    output_path.write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "summary": "fake Codex completed",
                                "changed_paths": [],
                                "checks": ["fake-codex"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", ""), {}

                with (
                    patch.object(CodexExecutor, "qualification", return_value=(True, "fake-subscription")),
                    patch.object(CodexExecutor, "_command", new=staticmethod(capture_command)),
                    patch.object(ProcessExecutor, "_run", autospec=True, side_effect=fake_run),
                ):
                    coordinator._execute_claimed(claimed)

                expected = physical_worktrees.resolve(strict=True) / "physical-worktree" / "worker-a1"
                command = captured["command"]
                assert isinstance(command, list)
                self.assertEqual(captured["request_worktree"], expected)
                self.assertEqual(captured["cwd"], expected)
                self.assertEqual(command[command.index("--cd") + 1], str(expected))
                self.assertEqual(captured["cwd"], Path(command[command.index("--cd") + 1]))

                task = store.get_task(contract.task_id)
                allocation = store.list_worktree_allocations()[0]
                worker = next(item for item in task["nodes"] if item["node_id"] == node.node_id)
                self.assertEqual(worker["worktree"], str(expected))
                self.assertEqual(allocation["current_path"], str(expected))
                self.assertNotIn(str(state_root / "worktrees"), str(expected))
            finally:
                coordinator._pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
