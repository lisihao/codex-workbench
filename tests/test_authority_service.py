from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import tempfile
import unittest

from codex_workbench.authority_service import (
    AUTHORITY_REQUEST_JOURNAL_DDL,
    AuthorityService,
    is_read_only_tool,
)
from codex_workbench.model import canonical_hash, now_iso
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


class AuthorityServiceTests(unittest.TestCase):
    """The service records writes before invoking the existing MCP handler."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        with self.store.connection() as connection:
            connection.executescript(AUTHORITY_REQUEST_JOURNAL_DDL)

    @staticmethod
    def _result(text: str = "done") -> dict[str, object]:
        return {"content": [{"type": "text", "text": text}]}

    @staticmethod
    def _envelope(
        request_id: str = "request-1",
        *,
        task_id: str = "task-a",
        arguments: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "request_id": request_id,
            "tool": "workbench_control_task",
            "arguments": arguments or {
                "task_id": task_id,
                "action": "pause",
                "expected_revision": 3,
            },
            "task_id": task_id,
        }

    @staticmethod
    def _validation_envelope(
        request_id: str = "validation-1",
        *,
        task_id: str = "task-a",
        dry_run: bool = False,
        argument_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "task_id": task_id,
            "node_id": "blocked-node",
            "expected_revision": 3,
            "expected_attempt": 1,
            "worktree": "/fixture/worktree",
            "check_id": "dsh-b-ipc-v1",
            "reason": "fixture validation",
            "dry_run": dry_run,
        }
        if not dry_run:
            arguments["validation_id"] = request_id
        if argument_overrides is not None:
            arguments.update(argument_overrides)
        return {
            "request_id": request_id,
            "tool": "workbench_validate_blocked_node",
            "arguments": arguments,
            "task_id": task_id,
        }

    def _service(self, invoke, *, instance_id: str = "authority-a") -> AuthorityService:
        return AuthorityService(self.store, invoke, instance_id)

    def _database_dump(self) -> tuple[str, ...]:
        with self.store.connection() as connection:
            return tuple(connection.iterdump())

    def _insert_task(self, task_id: str) -> None:
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, contract_json, contract_hash, state, state_revision,
                    priority, created_at, updated_at, blocker, verdict
                ) VALUES(?, '{}', ?, 'planned', 1, 0, ?, ?, NULL, NULL)
                """,
                (task_id, f"fixture-{task_id}", timestamp, timestamp),
            )

    def _bind_session(self, source_thread_id: str, active_task_id: str | None) -> None:
        if active_task_id is not None:
            self._insert_task(active_task_id)
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO context_import_receipts(
                    command_id, request_hash, source_thread_id, context_ref,
                    archive_ref, manifest_json, repository, base_sha,
                    allowed_scopes_json, context_excerpt, created_at
                ) VALUES(?, ?, ?, ?, ?, '{}', '/fixture/repository', 'fixture-base',
                         '[]', 'private fixture context', ?)
                """,
                (
                    f"context-{source_thread_id}",
                    f"context-hash-{source_thread_id}",
                    source_thread_id,
                    f"sha256:{source_thread_id}",
                    f"archive:{source_thread_id}",
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_bindings(
                    source_thread_id, context_ref, active_task_id, updated_at
                ) VALUES(?, ?, ?, ?)
                """,
                (source_thread_id, f"sha256:{source_thread_id}", active_task_id, timestamp),
            )

    def test_response_loss_can_read_or_repeat_without_reinvoking(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool, arguments))
            return self._result("persisted MCP result")

        service = self._service(invoke)
        envelope = self._envelope()
        first = service.dispatch(envelope, authenticated_actor="http-session")

        self.assertEqual(first, {
            "request_id": "request-1",
            "state": "completed",
            "result": self._result("persisted MCP result"),
        })
        self.assertEqual(service.get_request("request-1"), first)
        self.assertEqual(
            service.dispatch(envelope, authenticated_actor="http-session"),
            first,
        )
        self.assertEqual(calls, [("workbench_control_task", envelope["arguments"])])
        with self.store.connection() as connection:
            receipt_row = connection.execute(
                "SELECT actor, request_fingerprint FROM authority_requests WHERE request_id = 'request-1'"
            ).fetchone()
        assert receipt_row is not None
        self.assertEqual(receipt_row["actor"], "http-session")
        self.assertNotEqual(receipt_row["request_fingerprint"], "http-session")
        events = self.store.read_events(task_id="task-a")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["authority_request.executing", "authority_request.completed"],
        )
        self.assertNotIn("arguments", events[0]["payload"])
        self.assertNotIn("result", events[1]["payload"])

    def test_concurrent_same_request_id_invokes_once(self) -> None:
        calls = 0
        calls_lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return self._result("once")

        service = self._service(invoke)
        envelope = self._envelope()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(service.dispatch, envelope)
            self.assertTrue(started.wait(timeout=3))
            repeated_future = executor.submit(service.dispatch, envelope)
            repeated = repeated_future.result(timeout=3)
            self.assertEqual(repeated, {"request_id": "request-1", "state": "executing"})
            release.set()
            first = first_future.result(timeout=3)

        self.assertEqual(calls, 1)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(service.get_request("request-1"), first)

    def test_payload_or_binding_conflicts_are_rejected_without_reinvocation(self) -> None:
        calls: list[str] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append(tool)
            return self._result()

        service = self._service(invoke)
        mismatched = self._envelope(
            task_id="task-a",
            arguments={
                "task_id": "task-b",
                "action": "pause",
                "expected_revision": 3,
            },
        )
        with self.assertRaisesRegex(ValueError, "task_id does not match"):
            service.dispatch(mismatched)
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            service.dispatch({**self._envelope(), "actor": "untrusted-client"})

        original = self._envelope()
        service.dispatch(original)
        replacement = self._envelope(
            task_id="task-b",
            arguments={
                "task_id": "task-b",
                "action": "pause",
                "expected_revision": 3,
            },
        )
        with self.assertRaises(CommandConflictError):
            service.dispatch(replacement)

        self.assertEqual(calls, ["workbench_control_task"])
        self.assertEqual(service.get_request("request-1")["state"], "completed")

    def test_continue_session_freezes_active_task_and_rejects_binding_drift(self) -> None:
        self._bind_session("thread-a", "task-old")
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool, arguments))
            return self._result("continued old task")

        service = self._service(invoke)
        envelope = {
            "request_id": "continue-id",
            "tool": "workbench_continue_session",
            "arguments": {
                "source_thread_id": "thread-a",
                "instruction": "continue the frozen task",
            },
            "session_id": "thread-a",
        }
        first = service.dispatch(envelope)

        self.assertEqual(first["state"], "completed")
        self.assertEqual(calls, [(
            "workbench_continue_session",
            {
                "source_thread_id": "thread-a",
                "instruction": "continue the frozen task",
                "expected_task_id": "task-old",
            },
        )])
        with self.store.connection() as connection:
            receipt = connection.execute(
                "SELECT task_id FROM authority_requests WHERE request_id = 'continue-id'"
            ).fetchone()
        assert receipt is not None
        self.assertEqual(receipt["task_id"], "task-old")

        self._insert_task("task-new")
        self.store.bind_task_to_session("thread-a", "task-new")
        with self.assertRaises(CommandConflictError):
            service.dispatch(envelope)
        self.assertEqual(len(calls), 1)

    def test_declared_or_unknown_session_cannot_select_another_task(self) -> None:
        self._bind_session("thread-a", "task-old")
        self._insert_task("task-other")
        calls: list[str] = []
        service = self._service(lambda tool, arguments: calls.append(tool) or self._result())

        with self.assertRaisesRegex(ValueError, "does not match the session active task"):
            service.dispatch({
                "request_id": "wrong-session-task",
                "tool": "workbench_continue_session",
                "arguments": {
                    "source_thread_id": "thread-a",
                    "instruction": "do not redirect this continuation",
                },
                "session_id": "thread-a",
                "task_id": "task-other",
            })
        with self.assertRaisesRegex(ValueError, "expected_task_id does not match"):
            service.dispatch({
                "request_id": "wrong-expected-task",
                "tool": "workbench_continue_session",
                "arguments": {
                    "source_thread_id": "thread-a",
                    "instruction": "do not replace the frozen task",
                    "expected_task_id": "task-other",
                },
                "session_id": "thread-a",
            })
        with self.assertRaisesRegex(ValueError, "no durable session binding"):
            service.dispatch({
                "request_id": "unknown-session",
                "tool": "workbench_continue_session",
                "arguments": {
                    "source_thread_id": "missing-thread",
                    "instruction": "do not create a session",
                },
                "session_id": "missing-thread",
            })

        self.assertEqual(calls, [])
        with self.assertRaises(KeyError):
            service.get_request("wrong-session-task")
        with self.assertRaises(KeyError):
            service.get_request("wrong-expected-task")
        with self.assertRaises(KeyError):
            service.get_request("unknown-session")

    def test_new_workbench_request_keeps_its_new_task_id_independent_of_active_session(self) -> None:
        self._bind_session("thread-a", "task-active")
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool, arguments))
            return self._result("planning reservation accepted")

        service = self._service(invoke)
        envelope = {
            "request_id": "new-planning-request",
            "tool": "workbench_request",
            "arguments": {
                "source_thread_id": "thread-a",
                "task_id": "task-new-reservation",
                "objective": "start a new task from the bound context",
            },
            "session_id": "thread-a",
            "task_id": "task-new-reservation",
        }

        result = service.dispatch(envelope)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(calls, [("workbench_request", envelope["arguments"])])

    def test_request_ids_match_the_public_length_and_control_character_schema(self) -> None:
        calls: list[str] = []
        service = self._service(lambda tool, arguments: calls.append(tool) or self._result())

        with self.assertRaisesRegex(ValueError, "at most 200 characters"):
            service.dispatch(self._envelope("x" * 201))
        with self.assertRaisesRegex(ValueError, "control characters"):
            service.dispatch(self._envelope("request\x1fcontrol"))
        with self.assertRaisesRegex(ValueError, "control characters"):
            service.get_request("request\ncontrol")

        self.assertEqual(calls, [])
        with self.store.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM authority_requests").fetchone()
        assert count is not None
        self.assertEqual(count["count"], 0)

    def test_validation_dry_run_is_read_only_and_run_identity_is_bound(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool, arguments))
            return self._result("validation preview")

        service = self._service(invoke)
        database_before = self._database_dump()
        preview = service.dispatch(self._validation_envelope("validation-preview", dry_run=True))

        self.assertEqual(preview, {
            "request_id": "validation-preview",
            "state": "completed",
            "result": self._result("validation preview"),
        })
        self.assertEqual(self._database_dump(), database_before)
        self.assertTrue(is_read_only_tool("workbench_validate_blocked_node", {"dry_run": True}))
        with self.assertRaises(KeyError):
            service.get_request("validation-preview")
        for invalid in (
            self._validation_envelope(
                "validation-identity",
                argument_overrides={"validation_id": "other-id"},
            ),
            {
                "request_id": "validation-missing-task",
                "tool": "workbench_validate_blocked_node",
                "arguments": {
                    "node_id": "blocked-node",
                    "dry_run": False,
                    "validation_id": "validation-missing-task",
                },
            },
            self._validation_envelope(
                "validation-task-binding",
                task_id="task-a",
                argument_overrides={"task_id": "task-b"},
            ),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.dispatch(invalid)
        self.assertEqual([tool for tool, _ in calls], ["workbench_validate_blocked_node"])

    def test_validation_receipt_is_idempotent_and_never_replays(self) -> None:
        calls = 0

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return self._result("validation run")

        service = self._service(invoke)
        envelope = self._validation_envelope("validation-once", task_id="task-validation")
        first = service.dispatch(envelope)

        self.assertEqual(first, {
            "request_id": "validation-once",
            "state": "completed",
            "result": self._result("validation run"),
        })
        self.assertEqual(service.dispatch(envelope), first)
        self.assertEqual(service.get_request("validation-once"), first)
        self.assertEqual(calls, 1)

    def test_task_fences_block_concurrent_validation_and_other_mutations(self) -> None:
        for first_kind in ("mutation", "validation"):
            with self.subTest(first_kind=first_kind):
                started = threading.Event()
                release = threading.Event()
                calls: list[str] = []

                def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
                    calls.append(tool)
                    started.set()
                    self.assertTrue(release.wait(timeout=3))
                    return self._result(tool)

                service = self._service(invoke, instance_id=f"authority-{first_kind}")
                task_id = f"task-fence-{first_kind}"
                mutation = self._envelope(f"mutation-{first_kind}", task_id=task_id)
                validation = self._validation_envelope(
                    f"validation-{first_kind}", task_id=task_id
                )
                first = mutation if first_kind == "mutation" else validation
                blocked = validation if first_kind == "mutation" else mutation
                with ThreadPoolExecutor(max_workers=1) as executor:
                    running = executor.submit(service.dispatch, first)
                    self.assertTrue(started.wait(timeout=3))
                    with self.assertRaises(StateConflictError):
                        service.dispatch(blocked)
                    release.set()
                    self.assertEqual(running.result(timeout=3)["state"], "completed")

                self.assertEqual(calls, [first["tool"]])
                with self.assertRaises(KeyError):
                    service.get_request(blocked["request_id"])

    def test_validation_recovery_marks_executing_unknown_without_replay(self) -> None:
        envelope = self._validation_envelope("validation-interrupted", task_id="task-validation")
        fingerprint = canonical_hash({
            "tool": envelope["tool"],
            "arguments": envelope["arguments"],
            "task_id": envelope["task_id"],
            "session_id": None,
            "actor": "authenticated",
        })
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO authority_requests(
                    request_id, request_fingerprint, tool, task_id, session_id,
                    actor, state, instance_id, result_json, created_at, updated_at,
                    settled_at
                ) VALUES(?, ?, ?, ?, NULL, ?, 'executing', ?, NULL, ?, ?, NULL)
                """,
                (
                    "validation-interrupted",
                    fingerprint,
                    envelope["tool"],
                    envelope["task_id"],
                    "authenticated",
                    "authority-before-restart",
                    timestamp,
                    timestamp,
                ),
            )

        calls: list[str] = []
        restarted = self._service(
            lambda tool, arguments: calls.append(tool) or self._result(),
            instance_id="authority-after-restart",
        )
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(restarted.get_request("validation-interrupted"), {
            "request_id": "validation-interrupted",
            "state": "unknown",
        })
        self.assertEqual(restarted.dispatch(envelope), {
            "request_id": "validation-interrupted",
            "state": "unknown",
        })
        self.assertEqual(calls, [])

    def test_drain_waits_for_admitted_mutation_and_keeps_reads_available(self) -> None:
        mutation_started = threading.Event()
        release_mutation = threading.Event()
        waiter_started = threading.Event()
        waiter_finished = threading.Event()
        calls: list[str] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append(tool)
            if tool == "workbench_control_task":
                mutation_started.set()
                self.assertTrue(release_mutation.wait(timeout=3))
                return self._result("mutation settled")
            return self._result("read remains available")

        service = self._service(invoke, instance_id="authority-drain")
        held_request = self._envelope("drain-held", task_id="task-drain")
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(service.dispatch, held_request)
            self.assertTrue(mutation_started.wait(timeout=3))
            service.begin_drain()

            with self.assertRaisesRegex(StateConflictError, "draining"):
                service.dispatch(self._envelope("drain-rejected", task_id="task-drain"))
            self.assertEqual(
                service.get_request("drain-held"),
                {"request_id": "drain-held", "state": "executing"},
            )
            read = service.dispatch({"tool": "workbench_list_tasks", "arguments": {}})
            self.assertEqual(read, {
                "state": "completed",
                "result": self._result("read remains available"),
            })

            states_after_wait: list[str] = []

            def wait_for_settlement() -> None:
                waiter_started.set()
                service.wait_for_idle()
                states_after_wait.append(service.get_request("drain-held")["state"])
                waiter_finished.set()

            waiter = threading.Thread(target=wait_for_settlement, daemon=True)
            waiter.start()
            try:
                self.assertTrue(waiter_started.wait(timeout=3))
                self.assertFalse(waiter_finished.wait(timeout=0.2))
                release_mutation.set()
                self.assertEqual(running.result(timeout=3)["state"], "completed")
                self.assertTrue(waiter_finished.wait(timeout=3))
            finally:
                release_mutation.set()
                waiter.join(timeout=3)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(states_after_wait, ["completed"])
        self.assertEqual(calls, ["workbench_control_task", "workbench_list_tasks"])
        with self.assertRaises(KeyError):
            service.get_request("drain-rejected")

    def test_invoke_exception_becomes_unknown_and_is_not_replayed(self) -> None:
        calls = 0

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise RuntimeError("private token must not reach an event")

        service = self._service(invoke)
        result = service.dispatch(self._envelope())

        self.assertEqual(result, {"request_id": "request-1", "state": "unknown"})
        self.assertEqual(service.get_request("request-1"), result)
        self.assertEqual(service.dispatch(self._envelope()), result)
        self.assertEqual(calls, 1)
        event = self.store.read_events(task_id="task-a")[-1]
        self.assertEqual(event["event_type"], "authority_request.unknown")
        self.assertNotIn("private token", str(event["payload"]))

    def test_recovery_marks_leftover_executing_unknown_without_replay(self) -> None:
        envelope = self._envelope("interrupted")
        fingerprint = canonical_hash({
            "tool": envelope["tool"],
            "arguments": envelope["arguments"],
            "task_id": envelope["task_id"],
            "session_id": None,
            "actor": "authenticated",
        })
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO authority_requests(
                    request_id, request_fingerprint, tool, task_id, session_id,
                    actor, state, instance_id, result_json, created_at, updated_at,
                    settled_at
                ) VALUES(?, ?, ?, ?, NULL, ?, 'executing', ?, NULL, ?, ?, NULL)
                """,
                (
                    "interrupted",
                    fingerprint,
                    envelope["tool"],
                    envelope["task_id"],
                    "authenticated",
                    "authority-before-restart",
                    timestamp,
                    timestamp,
                ),
            )
            WorkbenchStore._event(
                connection,
                "authority_request.executing",
                "task-a",
                None,
                {
                    "request_id": "interrupted",
                    "tool": "workbench_control_task",
                    "task_id": "task-a",
                    "session_id": None,
                    "state": "executing",
                },
                created_at=timestamp,
            )

        calls: list[str] = []
        restarted = self._service(
            lambda tool, arguments: calls.append(tool) or self._result(),
            instance_id="authority-after-restart",
        )
        self.assertEqual(
            restarted.get_request("interrupted"),
            {"request_id": "interrupted", "state": "executing"},
        )
        self.assertEqual(calls, [])
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(
            restarted.get_request("interrupted"),
            {"request_id": "interrupted", "state": "unknown"},
        )
        self.assertEqual(restarted.dispatch(envelope), {
            "request_id": "interrupted", "state": "unknown",
        })
        self.assertEqual(calls, [])
        with self.assertRaises(KeyError):
            restarted.get_request("missing-request")

    def test_read_only_tools_and_legal_dry_run_do_not_write_a_journal(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool, arguments))
            return self._result("read only")

        service = self._service(invoke)
        database_before = self._database_dump()
        list_result = service.dispatch({
            "tool": "workbench_list_tasks",
            "arguments": {},
        })
        dry_run_result = service.dispatch({
            "request_id": "preview-id",
            "tool": "workbench_control_task",
            "arguments": {"action": "resume", "dry_run": True},
        })

        self.assertEqual(list_result, {"state": "completed", "result": self._result("read only")})
        self.assertEqual(dry_run_result, {
            "request_id": "preview-id",
            "state": "completed",
            "result": self._result("read only"),
        })
        self.assertEqual(self._database_dump(), database_before)
        self.assertTrue(is_read_only_tool("workbench_control_task", {"dry_run": True}))
        self.assertFalse(is_read_only_tool("workbench_control_task", {"dry_run": False}))
        with self.assertRaises(KeyError):
            service.get_request("preview-id")
        self.assertEqual([tool for tool, _ in calls], [
            "workbench_list_tasks", "workbench_control_task",
        ])

    def test_cas_failure_is_a_completed_mcp_error_without_permission_upgrade(self) -> None:
        calls = 0

        def invoke(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise StateConflictError("task state revision is stale")

        service = self._service(invoke)
        result = service.dispatch(self._envelope())

        expected_mcp_error = {
            "content": [{"type": "text", "text": "task state revision is stale"}],
            "isError": True,
        }
        self.assertEqual(result, {
            "request_id": "request-1",
            "state": "completed",
            "result": expected_mcp_error,
        })
        self.assertEqual(service.get_request("request-1"), result)
        self.assertEqual(service.dispatch(self._envelope()), result)
        self.assertEqual(calls, 1)
        self.assertEqual(self.store.list_approvals(), [])


if __name__ == "__main__":
    unittest.main()
