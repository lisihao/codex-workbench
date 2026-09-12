"""Observe existing Authority scheduling independently of the chat active pointer."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.responsibility import ResponsibilityLedger
from codex_workbench.session_notifications import list_session_notifications
from tests import test_session_objectives as inventory_fixture


class ContinuationBackgroundTests(unittest.TestCase):
    def test_new_active_goal_and_authority_restart_do_not_reexecute_accepted_work(self):
        fixture = inventory_fixture.SessionObjectiveInventoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture._record_context("multi-goal-session")
        tasks = ("earlier-goal", "current-goal")
        ledger = ResponsibilityLedger(fixture.store)
        for task_id in tasks:
            fixture.store.create_task(TaskContract(
                task_id, str(fixture.root), "fixture-base", "finish existing goal", ("src",),
            ), [
                NodeSpec("work", task_id, "work", "fixture", "fixture", "done", write_scopes=("src",)),
                NodeSpec("verify", task_id, "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
            ], "create-" + task_id)
            fixture.store.bind_task_to_session("multi-goal-session", task_id)
            fixture.store.queue_task(task_id)
            task = fixture.store.get_task(task_id)
            ledger.open(
                command_id="open-" + task_id, task_id=task_id, goal_id=task_id,
                node_id="work", attempt=0, task_revision=task["state_revision"],
                owner="multi-goal-session", next_action="await_task_acceptance",
                deadline="2099-01-01T00:00:00+00:00",
            )
        self.assertEqual({item["task_id"] for item in fixture._session("multi-goal-session")["objectives"]}, set(tasks))
        for cycle in range(2):
            epoch = fixture.store.activate_coordinator("background-cycle-" + str(cycle), "fixture")
            coordinator = Coordinator(fixture.store, fixture.root, coordinator_epoch=epoch, max_workers=1, poll_seconds=0.01)
            claim_observed = threading.Event()
            original_claim = coordinator._claim_next_ready_node

            def observe_claim(*args, **kwargs):
                result = original_claim(*args, **kwargs)
                claim_observed.set()
                return result

            claim_probe = patch.object(coordinator, "_claim_next_ready_node", side_effect=observe_claim)
            claim_probe.start()
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            try:
                self.assertTrue(claim_observed.wait(timeout=2))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(
                        fixture.store.get_task(task_id)["state"] == "accepted"
                        and ledger.inspect(task_id=task_id, goal_id=task_id)["state"] == "fulfilled"
                        for task_id in tasks
                    ):
                        break
                    time.sleep(0.01)
                for task_id in tasks:
                    self.assertEqual(fixture.store.get_task(task_id)["state"], "accepted")
                    self.assertEqual(ledger.inspect(task_id=task_id, goal_id=task_id)["state"], "fulfilled")
            finally:
                coordinator.stop()
                thread.join(timeout=3)
                claim_probe.stop()
                self.assertFalse(thread.is_alive())
        with fixture.store.connection() as connection:
            started = connection.execute("SELECT task_id, node_id, COUNT(*) FROM events WHERE event_type='node.started' GROUP BY task_id,node_id").fetchall()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE event_type='responsibility.fulfilled'").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE event_type='responsibility.reconcile_failed'").fetchone()[0], 0)
        self.assertEqual(sorted(tuple(row) for row in started), [
            (task_id, node_id, 1) for task_id in sorted(tasks) for node_id in ("verify", "work")
        ])
        self.assertEqual(fixture._session("multi-goal-session")["objectives"], [])
        notifications = list_session_notifications(fixture.store, "multi-goal-session", limit=50)
        self.assertEqual(sum(item["event_type"] == "responsibility.fulfilled" for item in notifications["notifications"]), 2)
