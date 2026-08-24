from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


class ServiceTests(unittest.TestCase):
    def test_fixture_dag_reaches_independent_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            contract = TaskContract(
                task_id="e2e",
                repository=str(root),
                base_sha="fixture",
                objective="parallel fixture",
                allowed_scope=("tests",),
            )
            nodes = [
                NodeSpec("a", "e2e", "A", "fixture", "fixture", "A", write_scopes=("tests/a",)),
                NodeSpec("b", "e2e", "B", "fixture", "fixture", "B", write_scopes=("tests/b",)),
                NodeSpec("v", "e2e", "V", "fixture", "fixture", "accepted", depends_on=("a", "b"), verifier=True),
            ]
            store.create_task(contract, nodes, "e2e-create")
            store.queue_task("e2e")
            coordinator = Coordinator(store, root, max_workers=2, poll_seconds=0.01)
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and store.get_task("e2e")["state"] != "accepted":
                time.sleep(0.01)
            coordinator.stop()
            thread.join(timeout=2)
            task = store.get_task("e2e")
            self.assertEqual(task["state"], "accepted")
            self.assertEqual({node["state"] for node in task["nodes"]}, {"accepted"})
            events = store.read_events(task_id="e2e")
            cursors = [event["cursor"] for event in events]
            self.assertEqual(cursors, sorted(cursors))
            self.assertIn("task.state_changed", {event["event_type"] for event in events})

    def test_parallel_worktree_patches_are_composed_for_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            contract = TaskContract(
                task_id="compose",
                repository=str(repository),
                base_sha=base_sha,
                objective="compose parallel changes",
                allowed_scope=("tests",),
            )
            make_a = (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('tests').mkdir(); Path('tests/a.txt').write_text('A')",
            )
            make_b = (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('tests').mkdir(); Path('tests/b.txt').write_text('B')",
            )
            verify = (
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('tests/a.txt').read_text() == 'A'; assert Path('tests/b.txt').read_text() == 'B'",
            )
            nodes = [
                NodeSpec("a", "compose", "A", "deterministic", "local", command=make_a, write_scopes=("tests/a.txt",)),
                NodeSpec("b", "compose", "B", "deterministic", "local", command=make_b, write_scopes=("tests/b.txt",)),
                NodeSpec(
                    "verify",
                    "compose",
                    "verify",
                    "deterministic",
                    "local",
                    command=verify,
                    depends_on=("a", "b"),
                    verifier=True,
                    ordinal=2,
                ),
            ]
            store.create_task(contract, nodes, "compose-create")
            store.queue_task("compose")
            coordinator = Coordinator(store, state, max_workers=2, poll_seconds=0.01)
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and store.get_task("compose")["state"] not in {
                "accepted",
                "blocked",
                "needs_fix",
                "needs_approval",
            }:
                time.sleep(0.02)
            coordinator.stop()
            thread.join(timeout=3)
            task = store.get_task("compose")
            self.assertEqual(task["state"], "accepted", task)
            verifier = next(node for node in task["nodes"] if node["node_id"] == "verify")
            verifier_worktree = Path(verifier["worktree"])
            self.assertEqual((verifier_worktree / "tests/a.txt").read_text(), "A")
            self.assertEqual((verifier_worktree / "tests/b.txt").read_text(), "B")


if __name__ == "__main__":
    unittest.main()
