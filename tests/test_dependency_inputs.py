from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from codex_workbench.dependency_inputs import (
    DependencyInput,
    DependencyInputError,
    accepted_ancestor_nodes,
    changed_paths_since_input_tree,
    effective_spec_with_dependency_input,
    write_input_tree,
)
from codex_workbench.artifacts import ArtifactStore
from codex_workbench.evidence import reusable_evidence_key
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


class DependencyInputTests(unittest.TestCase):
    def test_dependency_closure_rejects_unaccepted_and_missing_nodes(self) -> None:
        unaccepted = {
            "task_id": "task",
            "contract": {"base_sha": "base"},
            "nodes": [
                {"node_id": "a", "state": "running", "depends_on": (), "result": None},
                {
                    "node_id": "b",
                    "state": "accepted",
                    "depends_on": ("a",),
                    "attempt": 1,
                    "result": {"artifacts": {}, "changed_paths": ()},
                },
            ],
        }
        with self.assertRaisesRegex(DependencyInputError, "expected accepted"):
            accepted_ancestor_nodes(unaccepted, "b")

        missing = {
            **unaccepted,
            "nodes": [
                {
                    "node_id": "b",
                    "state": "accepted",
                    "depends_on": ("missing",),
                    "attempt": 1,
                    "result": {"artifacts": {}, "changed_paths": ()},
                }
            ],
        }
        with self.assertRaisesRegex(DependencyInputError, "missing from task"):
            accepted_ancestor_nodes(missing, "b")

    def test_tree_baseline_excludes_inherited_paths_and_includes_untracked_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._init_repository(repository)
            (repository / "inherited.txt").write_text("input\n")
            subprocess.run(["git", "add", "inherited.txt"], cwd=repository, check=True)
            input_tree = write_input_tree(repository)
            (repository / "worker.txt").write_text("output\n")

            self.assertEqual(changed_paths_since_input_tree(repository, input_tree), {"worker.txt"})

    def test_dependency_cache_binds_content_closure_not_provenance_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._init_repository(repository)
            artifacts = ArtifactStore(repository / "artifacts")
            first_patch_ref = artifacts.put_bytes(b"first patch", "patch")
            changed_patch_ref = artifacts.put_bytes(b"changed patch", "patch")
            (repository / "input.txt").write_text("first input\n")
            subprocess.run(["git", "add", "input.txt"], cwd=repository, check=True)
            first_tree = write_input_tree(repository)
            (repository / "input.txt").write_text("changed input\n")
            subprocess.run(["git", "add", "input.txt"], cwd=repository, check=True)
            changed_tree = write_input_tree(repository)
            contract = {
                "repository": str(repository),
                "base_sha": self._git(repository, "rev-parse", "HEAD"),
                "objective": "verify input",
                "allowed_scope": ("README.md",),
                "forbidden_scope": (),
                "required_artifacts": (),
                "acceptance_commands": (),
                "verification_tier": "L2",
                "governance_profile": "code-as-harness/v1",
            }
            spec = {
                "task_id": "task",
                "node_id": "verify",
                "executor": "deterministic",
                "model": "fixture",
                "verifier": True,
                "read_scopes": ("README.md",),
                "write_scopes": (),
            }
            first = DependencyInput(
                first_tree,
                {
                    "task_id": "first-task",
                    "node_id": "first-node",
                    "contract_base_sha": contract["base_sha"],
                    "input_tree_sha": first_tree,
                    "ancestors": [{"node_id": "first-ancestor", "patch_ref": first_patch_ref}],
                },
            )
            same_content_new_identity = DependencyInput(
                first_tree,
                {
                    "task_id": "second-task",
                    "node_id": "second-node",
                    "contract_base_sha": contract["base_sha"],
                    "input_tree_sha": first_tree,
                    "ancestors": [{"node_id": "second-ancestor", "patch_ref": first_patch_ref}],
                },
            )
            changed_content = DependencyInput(
                changed_tree,
                {
                    "task_id": "third-task",
                    "node_id": "third-node",
                    "contract_base_sha": contract["base_sha"],
                    "input_tree_sha": changed_tree,
                    "ancestors": [{"node_id": "third-ancestor", "patch_ref": changed_patch_ref}],
                },
            )

            first_key = reusable_evidence_key(
                contract,
                effective_spec_with_dependency_input(spec, first),
                repository,
            )
            same_content_key = reusable_evidence_key(
                contract,
                effective_spec_with_dependency_input(
                    {**spec, "task_id": "second-task", "node_id": "second-node"},
                    same_content_new_identity,
                ),
                repository,
            )
            changed_content_key = reusable_evidence_key(
                contract,
                effective_spec_with_dependency_input(spec, changed_content),
                repository,
            )
            self.assertEqual(first_key, same_content_key)
            self.assertNotEqual(first_key, changed_content_key)
            self.assertNotIn("dependency_input", spec)
            self.assertEqual(
                set(effective_spec_with_dependency_input(spec, first)["dependency_input"]),
                {"contract_base_sha", "input_tree_sha", "ancestor_patch_refs"},
            )

    def test_real_temporary_git_dag_passes_only_accepted_ancestor_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            self._init_repository(repository)
            base_sha = self._git(repository, "rev-parse", "HEAD")
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            contract = TaskContract(
                task_id="dependency-flow",
                repository=str(repository),
                base_sha=base_sha,
                objective="pass accepted dependency output through independent worktrees",
                allowed_scope=("a.txt", "b.txt", "c.txt", "d.txt"),
                required_artifacts=(),
                verifier_model="fixture",
                retry_limit=0,
                verification_tier="L2",
            )
            nodes = [
                NodeSpec(
                    "a",
                    contract.task_id,
                    "contract",
                    "deterministic",
                    "fixture",
                    command=self._command("from pathlib import Path; Path('a.txt').write_text('a\\n')"),
                    write_scopes=("a.txt",),
                ),
                NodeSpec(
                    "b",
                    contract.task_id,
                    "backend",
                    "deterministic",
                    "fixture",
                    command=self._command(
                        "from pathlib import Path; "
                        "assert Path('a.txt').read_text() == 'a\\n'; "
                        "Path('b.txt').write_text('b\\n')"
                    ),
                    depends_on=("a",),
                    read_scopes=("a.txt",),
                    write_scopes=("b.txt",),
                ),
                NodeSpec(
                    "c",
                    contract.task_id,
                    "interface",
                    "deterministic",
                    "fixture",
                    command=self._command(
                        "from pathlib import Path; "
                        "assert Path('a.txt').read_text() == 'a\\n'; "
                        "assert not Path('b.txt').exists(); "
                        "Path('c.txt').write_text('c\\n')"
                    ),
                    depends_on=("a",),
                    read_scopes=("a.txt",),
                    write_scopes=("c.txt",),
                ),
                NodeSpec(
                    "d",
                    contract.task_id,
                    "documentation",
                    "deterministic",
                    "fixture",
                    command=self._command(
                        "from pathlib import Path; "
                        "assert [Path(name).read_text() for name in ('a.txt', 'b.txt', 'c.txt')] "
                        "== ['a\\n', 'b\\n', 'c\\n']; "
                        "Path('d.txt').write_text('d\\n')"
                    ),
                    depends_on=("b", "c"),
                    read_scopes=("a.txt", "b.txt", "c.txt"),
                    write_scopes=("d.txt",),
                ),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "deterministic",
                    "fixture",
                    command=self._command(
                        "from pathlib import Path; "
                        "assert [Path(name).read_text() for name in ('a.txt', 'b.txt', 'c.txt', 'd.txt')] "
                        "== ['a\\n', 'b\\n', 'c\\n', 'd\\n']"
                    ),
                    depends_on=("a", "b", "c", "d"),
                    read_scopes=("a.txt", "b.txt", "c.txt", "d.txt"),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "dependency-flow-create")
            store.queue_task(contract.task_id)
            coordinator = self._run_to_terminal(store, state, contract.task_id)

            task = store.get_task(contract.task_id)
            self.assertEqual(task["state"], "accepted", task)
            node_by_id = {node["node_id"]: node for node in task["nodes"]}
            self.assertEqual(node_by_id["a"]["result"]["changed_paths"], ["a.txt"])
            self.assertEqual(node_by_id["b"]["result"]["changed_paths"], ["b.txt"])
            self.assertEqual(node_by_id["c"]["result"]["changed_paths"], ["c.txt"])
            self.assertEqual(node_by_id["d"]["result"]["changed_paths"], ["d.txt"])

            def receipt(node_id: str) -> dict:
                ref = node_by_id[node_id]["result"]["artifacts"]["dependency-input"]
                return json.loads(coordinator.artifacts.verify(ref).read_text())

            self.assertEqual([item["node_id"] for item in receipt("b")["ancestors"]], ["a"])
            self.assertEqual([item["node_id"] for item in receipt("c")["ancestors"]], ["a"])
            self.assertEqual(
                [item["node_id"] for item in receipt("d")["ancestors"]], ["a", "b", "c"]
            )
            self.assertEqual(
                [item["node_id"] for item in receipt("verify")["ancestors"]],
                ["a", "b", "c", "d"],
            )
            self.assertEqual(receipt("d")["contract_base_sha"], base_sha)
            self.assertNotEqual(receipt("d")["input_tree_sha"], base_sha)

            verifier_worktree = Path(node_by_id["verify"]["worktree"])
            self.assertEqual((verifier_worktree / "a.txt").read_text(), "a\n")
            self.assertEqual((verifier_worktree / "b.txt").read_text(), "b\n")
            self.assertEqual((verifier_worktree / "c.txt").read_text(), "c\n")
            self.assertFalse((repository / "a.txt").exists())
            self.assertEqual(self._git(repository, "rev-parse", "HEAD"), base_sha)

    def test_worker_mutating_an_inherited_path_is_rejected_against_input_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            self._init_repository(repository)
            base_sha = self._git(repository, "rev-parse", "HEAD")
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            contract = TaskContract(
                task_id="dependency-scope",
                repository=str(repository),
                base_sha=base_sha,
                objective="reject worker edits to inherited input",
                allowed_scope=("a.txt", "b.txt"),
                required_artifacts=(),
                verifier_model="fixture",
                retry_limit=0,
                verification_tier="L2",
            )
            nodes = [
                NodeSpec(
                    "a",
                    contract.task_id,
                    "contract",
                    "deterministic",
                    "fixture",
                    command=self._command("from pathlib import Path; Path('a.txt').write_text('a\\n')"),
                    write_scopes=("a.txt",),
                ),
                NodeSpec(
                    "b",
                    contract.task_id,
                    "bad backend",
                    "deterministic",
                    "fixture",
                    command=self._command(
                        "from pathlib import Path; "
                        "Path('a.txt').write_text('mutated\\n'); Path('b.txt').write_text('b\\n')"
                    ),
                    depends_on=("a",),
                    read_scopes=("a.txt",),
                    write_scopes=("b.txt",),
                ),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "blocked",
                    depends_on=("a", "b"),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "dependency-scope-create")
            store.queue_task(contract.task_id)
            self._run_to_terminal(store, state, contract.task_id)

            task = store.get_task(contract.task_id)
            bad_worker = next(node for node in task["nodes"] if node["node_id"] == "b")
            self.assertEqual(task["state"], "needs_fix", task)
            self.assertEqual(bad_worker["state"], "failed")
            self.assertEqual(bad_worker["result"]["changed_paths"], ["a.txt", "b.txt"])
            self.assertIn("outside node write scopes", bad_worker["result"]["summary"])

    @staticmethod
    def _command(source: str) -> tuple[str, ...]:
        return (sys.executable, "-c", source)

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=repository, text=True).strip()

    @classmethod
    def _init_repository(cls, repository: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        (repository / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)

    @staticmethod
    def _run_to_terminal(store: WorkbenchStore, state: Path, task_id: str) -> Coordinator:
        epoch = store.activate_coordinator(f"run-{task_id}", "test-machine")
        coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1, poll_seconds=0.01)
        thread = threading.Thread(target=coordinator.run_forever)
        thread.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and store.get_task(task_id)["state"] not in {
            "accepted",
            "blocked",
            "needs_fix",
            "needs_approval",
        }:
            time.sleep(0.02)
        coordinator.stop()
        thread.join(timeout=3)
        return coordinator


if __name__ == "__main__":
    unittest.main()
