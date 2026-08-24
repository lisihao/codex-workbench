from __future__ import annotations

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()

