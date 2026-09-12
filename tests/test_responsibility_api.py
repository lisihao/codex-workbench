"""Focused tests for the bounded responsibility MCP adapter."""

from pathlib import Path
import tempfile
import unittest

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.responsibility_api import (
    RESPONSIBILITY_TOOL,
    responsibility_tool,
)
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


class ResponsibilityAPITests(unittest.TestCase):
    task_id = "responsibility-fixture"
    owner = "thread-owner"
    recipient = "thread-recipient"
    goal_id = "goal-fixture"
    node_id = "worker"
    deadline = "2999-01-01T00:00:00+00:00"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="responsibility-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        context_ref = "sha256:" + ("a" * 64)
        contract = TaskContract(
            self.task_id,
            self.temp.name,
            "fixture",
            "preserve responsibility fixture",
            ("src",),
            source_thread_id=self.owner,
            context_bundle_ref=context_ref,
        )
        self.store.create_task(
            contract,
            [
                NodeSpec(self.node_id, self.task_id, "work", "fixture", "fixture", "work"),
                NodeSpec(
                    "verify",
                    self.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "verify",
                    depends_on=(self.node_id,),
                    verifier=True,
                ),
            ],
            "create-responsibility-fixture",
        )
        for index, thread_id in enumerate((self.owner, self.recipient), start=1):
            self.store.record_session_context(
                command_id=f"context-{thread_id}",
                request_hash=f"context-hash-{thread_id}",
                source_thread_id=thread_id,
                context_ref=f"sha256:{index:064x}",
                archive_ref=f"archive-{thread_id}",
                manifest={},
                repository=self.temp.name,
                base_sha="fixture",
                allowed_scopes=("src",),
                context_excerpt="",
            )
            self.store.bind_task_to_session(thread_id, self.task_id)

    def _open(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "op": "open",
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "node_id": self.node_id,
            "attempt": 0,
            "task_revision": 1,
            "source_thread_id": self.owner,
            "next_action": {"action": "continue"},
            "deadline": self.deadline,
            "request_id": "responsibility-open-1",
        }
        arguments.update(overrides)
        return arguments

    def _propose(self, **overrides: object) -> dict[str, object]:
        arguments = self._open(
            op="propose_handoff",
            source_thread_id=self.owner,
            next_action="wait for recipient",
            request_id="responsibility-propose-1",
            expected_responsibility_revision=1,
            proposed_owner=self.recipient,
        )
        arguments.update(overrides)
        return arguments

    def _claim(self, **overrides: object) -> dict[str, object]:
        arguments = self._open(
            op="claim_handoff",
            source_thread_id=self.recipient,
            next_action="continue after handoff",
            request_id="responsibility-claim-1",
            expected_responsibility_revision=2,
        )
        arguments.update(overrides)
        return arguments

    def test_schema_is_explicit_and_operation_enum_is_bounded(self):
        schema = RESPONSIBILITY_TOOL["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["op"]["enum"],
            ["list", "inspect", "open", "propose_handoff", "claim_handoff", "defer"],
        )
        self.assertEqual(schema["required"], ["op", "task_id"])

    def test_list_discovers_goal_ids_without_a_mutation_or_goal_id(self):
        responsibility_tool(self.store, self._open())
        before = self.store.health()["cursor"]
        page = responsibility_tool(self.store, {"op": "list", "task_id": self.task_id, "limit": 1})
        self.assertEqual([item["goal_id"] for item in page["items"]], [self.goal_id])
        self.assertEqual(self.store.health()["cursor"], before)

    def test_open_propose_claim_and_inspect_use_real_ledger_without_task_mutation(self):
        before = self.store.get_task(self.task_id)

        opened = responsibility_tool(self.store, self._open())
        self.assertEqual(opened["operation"], "open")
        self.assertEqual(opened["original_owner"], self.owner)
        self.assertEqual(opened["current_owner"], self.owner)
        self.assertEqual(
            opened["command_id"], f"responsibility:{self.task_id}:responsibility-open-1"
        )

        proposed = responsibility_tool(self.store, self._propose())
        self.assertEqual(proposed["state"], "handoff_proposed")
        self.assertEqual(proposed["current_owner"], self.owner)
        self.assertEqual(proposed["proposed_owner"], self.recipient)

        claimed = responsibility_tool(self.store, self._claim())
        self.assertEqual(claimed["state"], "claimed")
        self.assertEqual(claimed["current_owner"], self.recipient)
        self.assertIsNone(claimed["proposed_owner"])

        inspected = responsibility_tool(
            self.store,
            {"op": "inspect", "task_id": self.task_id, "goal_id": self.goal_id},
        )
        self.assertEqual(inspected["state"], "claimed")
        self.assertEqual(inspected["current_owner"], self.recipient)
        self.assertFalse(inspected["deadline_expired"])

        after = self.store.get_task(self.task_id)
        self.assertEqual(after, before)
        self.assertEqual(after["state"], "inbox")
        worker = next(node for node in after["nodes"] if node["node_id"] == self.node_id)
        self.assertEqual(worker["state"], "pending")
        self.assertEqual(worker["attempt"], 0)

    def test_request_id_is_the_business_command_id_and_open_is_idempotent(self):
        arguments = self._open(request_id="same-responsibility-request")
        first = responsibility_tool(self.store, arguments)
        cursor = self.store.health()["cursor"]

        second = responsibility_tool(self.store, arguments)
        self.assertEqual(first, second)
        self.assertEqual(
            second["command_id"], f"responsibility:{self.task_id}:same-responsibility-request"
        )
        self.assertEqual(self.store.health()["cursor"], cursor)

        with self.assertRaises(CommandConflictError):
            responsibility_tool(
                self.store,
                {**arguments, "next_action": "a different action"},
            )
        self.assertEqual(self.store.health()["cursor"], cursor)

    def test_defer_forwards_typed_wait_and_recheck_without_task_mutation(self):
        before = self.store.get_task(self.task_id)
        responsibility_tool(self.store, self._open())
        deferred = responsibility_tool(
            self.store,
            self._open(
                op="defer",
                source_thread_id=self.owner,
                next_action="recheck the fixture resource",
                request_id="responsibility-defer-1",
                expected_responsibility_revision=1,
                wait_reason={
                    "wait_kind": "resource",
                    "detail": "waiting for a fixture resource",
                    "release_condition": "fixture resource is available",
                },
                next_recheck_at="2098-01-01T00:00:00+00:00",
            ),
        )
        self.assertEqual(deferred["state"], "deferred")
        self.assertEqual(deferred["wait"]["wait_kind"], "resource")
        self.assertEqual(deferred["wait"]["responsible_owner"], self.owner)
        self.assertEqual(self.store.get_task(self.task_id), before)

    def test_rejects_unsupported_owner_fields_wrong_owner_and_non_json_values(self):
        before = self.store.get_task(self.task_id)
        for field in ("owner", "current_owner", "claimant"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                responsibility_tool(self.store, {**self._open(), field: "forged-owner"})

        with self.assertRaises(ValueError):
            responsibility_tool(
                self.store,
                {
                    "op": "inspect",
                    "task_id": self.task_id,
                    "goal_id": self.goal_id,
                    "request_id": "forged",
                },
            )
        with self.assertRaises(PermissionError):
            responsibility_tool(
                self.store,
                self._open(source_thread_id="unbound-thread", request_id="wrong-owner"),
            )
        with self.assertRaises(ValueError):
            responsibility_tool(self.store, self._open(next_action=float("nan")))
        with self.assertRaises(ValueError):
            responsibility_tool(self.store, self._open(next_action=["not-an-action"]))

        self.assertEqual(self.store.get_task(self.task_id), before)

    def test_claim_and_defer_reject_caller_supplied_owner_fields(self):
        responsibility_tool(self.store, self._open())
        for arguments, field in (
            (self._propose(current_owner=self.owner), "current_owner"),
            (self._claim(claimant=self.recipient), "claimant"),
            (
                {
                    **self._open(
                        op="defer",
                        source_thread_id=self.owner,
                        next_action="recheck",
                        request_id="responsibility-defer-1",
                        expected_responsibility_revision=1,
                        wait_reason={
                            "wait_kind": "resource",
                            "detail": "waiting for a fixture resource",
                            "release_condition": "fixture resource is available",
                        },
                        next_recheck_at="2098-01-01T00:00:00+00:00",
                    ),
                    "current_owner": self.owner,
                },
                "current_owner",
            ),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                responsibility_tool(self.store, arguments)
        with self.assertRaises(StateConflictError):
            responsibility_tool(self.store, self._claim(source_thread_id=self.owner))


if __name__ == "__main__":
    unittest.main()
