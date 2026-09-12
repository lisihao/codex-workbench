"""Authority-journal and Coordinator integration for durable session notices."""
from pathlib import Path
import tempfile
import unittest

from codex_workbench.authority_service import AuthorityService
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.session_notifications_api import session_notification_tool
from codex_workbench.store import CommandConflictError, WorkbenchStore


class SessionNotificationsAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-notice-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        epoch = self.store.activate_coordinator("fixture", "fixture")
        contract = TaskContract("notice-task", self.temp.name, "fixture", "fixture notification", allowed_scope=("src",))
        self.store.create_task(contract, [
            NodeSpec("worker", contract.task_id, "work", "fixture", "fixture", "work"),
            NodeSpec("verify", contract.task_id, "verify", "fixture", "fixture", "verify", depends_on=("worker",), verifier=True),
        ], "create-notice-task")
        for session in ("fixture-session", "another-session"):
            self.store.record_session_context(
                command_id="context-" + session, request_hash="fixture", source_thread_id=session,
                context_ref="fixture-context", archive_ref="fixture-archive", manifest={},
                repository=self.temp.name, base_sha="fixture", allowed_scopes=("src",), context_excerpt="",
            )
        self.store.bind_task_to_session("fixture-session", contract.task_id)
        with self.store.transaction() as connection:
            self.store._event(connection, "node.blocked", contract.task_id, "worker", {
                "private_diagnostic": "must not leave the event store",
            })
        self.coordinator = Coordinator(self.store, self.root, coordinator_epoch=epoch)
        self.addCleanup(self.coordinator._pool.shutdown, wait=True)
        self.authority = AuthorityService(
            self.store, lambda name, args: session_notification_tool(self.store, name, args), "fixture",
        )

    def read(self, session="fixture-session"):
        return self.authority.dispatch({
            "tool": "workbench_read_session_notifications", "arguments": {"source_thread_id": session},
        })["result"]

    def test_control_turn_projects_without_a_chat_request_and_ack_is_journaled(self):
        before = self.store.get_task("notice-task")
        self.coordinator._reconcile_authority_work()
        notices = self.read()["notifications"]
        self.assertEqual(len(notices), 1)
        self.assertNotIn("private_diagnostic", str(notices))
        self.assertEqual(self.read("another-session")["notifications"], [])
        self.coordinator._reconcile_authority_work()
        self.assertEqual(self.read()["notifications"], notices)
        request = {
            "request_id": "ack-fixture", "tool": "workbench_ack_session_notification",
            "arguments": {"source_thread_id": "fixture-session", "notification_id": notices[0]["notification_id"]},
        }
        first = self.authority.dispatch(request)
        # A subsequent task binding must not change an acknowledgement's
        # request identity for the earlier task's notification.
        later = TaskContract("later-fixture-task", self.temp.name, "fixture", "later task", allowed_scope=("src",))
        self.store.create_task(later, [
            NodeSpec("verify", later.task_id, "verify", "fixture", "fixture", "verify", verifier=True),
        ], "create-later-task")
        self.store.bind_task_to_session("fixture-session", later.task_id)
        self.assertEqual(first["result"], self.authority.dispatch(request)["result"])
        self.assertEqual(self.read()["notifications"], [])
        with self.assertRaises(CommandConflictError):
            self.authority.dispatch({**request, "arguments": {**request["arguments"], "source_thread_id": "another-session"}})
        self.assertEqual(self.store.get_task("notice-task"), before)


if __name__ == "__main__":
    unittest.main()
