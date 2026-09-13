from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench import __version__
from codex_workbench.authority_service import AUTHORITY_REQUEST_JOURNAL_DDL, AuthorityService
from codex_workbench.connection_evidence import AUTHORITY_SERVICE_PROTOCOL
from codex_workbench.model import now_iso
from codex_workbench.store import WorkbenchStore


class RequestLifecycleEvidenceTests(unittest.TestCase):
    """Authority events distinguish admission, action, and recorded outcomes."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = WorkbenchStore(Path(self.temporary_directory.name) / "state.sqlite")
        self.store.initialize()
        with self.store.connection() as connection:
            connection.executescript(AUTHORITY_REQUEST_JOURNAL_DDL)

    @staticmethod
    def _envelope(request_id: str = "request-1", task_id: str = "task-a") -> dict[str, object]:
        return {
            "request_id": request_id,
            "tool": "workbench_control_task",
            "arguments": {
                "task_id": task_id,
                "action": "pause",
                "expected_revision": 3,
            },
            "task_id": task_id,
        }

    @staticmethod
    def _result(text: str = "done") -> dict[str, object]:
        return {"content": [{"type": "text", "text": text}]}

    @staticmethod
    def _identity(instance_id: str) -> dict[str, str]:
        return {
            "instance_id": instance_id,
            "version": __version__,
            "protocol": AUTHORITY_SERVICE_PROTOCOL,
        }

    def test_admission_event_persists_loaded_authority_identity(self) -> None:
        service = AuthorityService(
            self.store,
            lambda tool, arguments: self._result(),
            "authority-admission",
        )

        service.dispatch(self._envelope())

        events = self.store.read_events(task_id="task-a")
        self.assertEqual(events[0]["event_type"], "authority_request.executing")
        self.assertEqual(events[0]["payload"], {
            "request_id": "request-1",
            "tool": "workbench_control_task",
            "task_id": "task-a",
            "session_id": None,
            "state": "executing",
            "phase": "admitted",
            "origin_authority": self._identity("authority-admission"),
            "external_effects": "unknown",
        })
        for event in events:
            self.assertNotIn("arguments", event["payload"])
            self.assertNotIn("result", event["payload"])
            self.assertNotIn("actor", event["payload"])

    def test_action_start_is_visible_inside_callback_and_never_replayed(self) -> None:
        calls = 0

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            events = self.store.read_events(task_id="task-a")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["authority_request.executing", "authority_request.action_started"],
            )
            self.assertEqual(events[-1]["payload"]["phase"], "action_started")
            self.assertEqual(
                events[-1]["payload"]["origin_authority"],
                self._identity("authority-action"),
            )
            return self._result("one action")

        service = AuthorityService(self.store, invoke, "authority-action")
        envelope = self._envelope()
        first = service.dispatch(envelope)
        self.assertEqual(service.dispatch(envelope), first)
        self.assertEqual(service.get_request("request-1"), first)

        events = self.store.read_events(task_id="task-a")
        self.assertEqual(calls, 1)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "authority_request.executing",
                "authority_request.action_started",
                "authority_request.completed",
            ],
        )
        self.assertEqual(
            sum(event["event_type"] == "authority_request.action_started" for event in events),
            1,
        )

    def test_lost_success_response_uses_receipt_lookup_without_another_action_event(self) -> None:
        calls = 0

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return self._result("persisted result")

        service = AuthorityService(self.store, invoke, "authority-lookup")
        dispatched = service.dispatch(self._envelope())
        events_before_lookup = self.store.read_events(task_id="task-a")

        self.assertEqual(service.get_request("request-1"), dispatched)
        self.assertEqual(calls, 1)
        self.assertEqual(self.store.read_events(task_id="task-a"), events_before_lookup)

    def test_recovery_does_not_fabricate_an_old_origin_version(self) -> None:
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO authority_requests(
                    request_id, request_fingerprint, tool, task_id, session_id,
                    actor, state, instance_id, result_json, created_at, updated_at,
                    settled_at
                ) VALUES(?, 'fixture-fingerprint', 'workbench_control_task', ?, NULL,
                         'fixture-actor', 'executing', ?, NULL, ?, ?, NULL)
                """,
                ("old-request", "task-a", "authority-before-restart", timestamp, timestamp),
            )

        restarted = AuthorityService(
            self.store,
            lambda tool, arguments: self._result(),
            "authority-after-restart",
        )
        self.assertEqual(restarted.recover_interrupted(), 1)

        event = self.store.read_events(task_id="task-a")[-1]
        self.assertEqual(event["event_type"], "authority_request.unknown")
        self.assertEqual(event["payload"]["phase"], "outcome_unknown")
        self.assertEqual(event["payload"]["origin_authority"], {
            "instance_id": "authority-before-restart",
            "version": None,
            "protocol": None,
        })
        self.assertEqual(
            event["payload"]["settled_by_authority"],
            self._identity("authority-after-restart"),
        )

    def test_result_and_unknown_events_keep_external_effects_unknown(self) -> None:
        completed = AuthorityService(
            self.store,
            lambda tool, arguments: self._result(),
            "authority-completed",
        )
        completed.dispatch(self._envelope("completed-request", "task-completed"))
        failed = AuthorityService(
            self.store,
            lambda tool, arguments: (_ for _ in ()).throw(RuntimeError("fixture failure")),
            "authority-unknown",
        )
        failed.dispatch(self._envelope("unknown-request", "task-unknown"))

        completed_event = self.store.read_events(task_id="task-completed")[-1]
        unknown_event = self.store.read_events(task_id="task-unknown")[-1]
        self.assertEqual(completed_event["event_type"], "authority_request.completed")
        self.assertEqual(completed_event["payload"]["phase"], "result_recorded")
        self.assertEqual(unknown_event["event_type"], "authority_request.unknown")
        self.assertEqual(unknown_event["payload"]["phase"], "outcome_unknown")
        self.assertEqual(completed_event["payload"]["external_effects"], "unknown")
        self.assertEqual(unknown_event["payload"]["external_effects"], "unknown")


if __name__ == "__main__":
    unittest.main()
