from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.responsibility import ResponsibilityLedger
from codex_workbench.session_notifications import register_task_session
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


_FUTURE_DEADLINE = "2099-01-01T12:00:00+00:00"
_FUTURE_RECHECK = "2098-01-01T12:00:00+00:00"


class ResponsibilityLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.path = self.root / "state.sqlite"
        self.store = WorkbenchStore(self.path)
        self.store.initialize()
        self.coordinator_epoch = self.store.activate_coordinator(
            "responsibility-test-coordinator",
            "responsibility-test-machine",
        )
        self.ledger = ResponsibilityLedger(self.store)
        self.task_id = "responsibility-task"
        self.source_owner = "source-session"
        self._create_task(self.task_id, source_owner=self.source_owner)

    def _create_task(self, task_id: str, *, source_owner: str | None) -> None:
        contract_kwargs: dict[str, str] = {}
        if source_owner is not None:
            contract_kwargs = {
                "source_thread_id": source_owner,
                "context_bundle_ref": "sha256:" + ("a" * 64),
            }
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.root),
            base_sha="a" * 40,
            objective=f"fixture responsibility task {task_id}",
            allowed_scope=("src",),
            **contract_kwargs,
        )
        worker = NodeSpec(
            "work",
            task_id,
            "work",
            "fixture",
            "fixture",
            "fixture work",
            ordinal=1,
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "verify",
            "fixture",
            "fixture",
            "fixture verify",
            depends_on=("work",),
            verifier=True,
            ordinal=2,
        )
        self.store.create_task(contract, [worker, verifier], f"create-{task_id}")

    def _route(self, task_id: str, owner: str) -> None:
        with self.store.transaction() as connection:
            register_task_session(connection, task_id, owner)

    def _accept_task(self, task_id: str) -> dict:
        self.store.queue_task(
            task_id,
            expected_revision=self.store.get_task(task_id)["state_revision"],
        )
        while True:
            claimed = self.store.claim_ready_node(
                "responsibility-terminal-fixture",
                self.coordinator_epoch,
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["task_id"], task_id)
            self.store.settle_claimed(claimed, NodeResult("succeeded", "fixture accepted"))
            task = self.store.get_task(task_id)
            if task["state"] == "accepted":
                return task

    def _create_delivery(self, task_id: str) -> dict:
        return self.store.create_delivery_objective(
            task_id,
            f"delivery-{task_id}",
            {
                "requested_endpoints": {
                    "github": {"remote": "origin", "base_branch": "main", "publish": True},
                },
                "scope": {"paths": ["src"], "repository": "fixture"},
                "authority": {"delivery": "responsibility-fixture"},
                "budget": {
                    "attempt_limit": 1,
                    "time_budget_seconds": 60,
                    "cost_budget": 1,
                    "base_backoff_seconds": 1,
                    "max_backoff_seconds": 1,
                },
            },
        )

    def _complete_delivery(self, objective_id: str) -> None:
        with self.store.transaction() as connection:
            changed = connection.execute(
                "UPDATE delivery_objectives SET state = 'complete' WHERE objective_id = ?",
                (objective_id,),
            ).rowcount
        self.assertEqual(changed, 1)

    def _open(
        self,
        *,
        task_id: str | None = None,
        goal_id: str = "goal-main",
        command_id: str = "open-main",
        owner: str | None = None,
        deadline: str = _FUTURE_DEADLINE,
    ) -> dict:
        resolved_task_id = task_id or self.task_id
        return self.ledger.open(
            command_id=command_id,
            task_id=resolved_task_id,
            goal_id=goal_id,
            node_id="work",
            attempt=0,
            task_revision=self.store.get_task(resolved_task_id)["state_revision"],
            owner=owner or self.source_owner,
            next_action={"action": "await accountable response"},
            deadline=deadline,
        )

    def _propose(
        self,
        *,
        task_id: str | None = None,
        goal_id: str = "goal-main",
        command_id: str = "propose-main",
        revision: int = 1,
        recipient: str = "recipient-session",
    ) -> dict:
        resolved_task_id = task_id or self.task_id
        return self.ledger.propose_handoff(
            command_id=command_id,
            task_id=resolved_task_id,
            goal_id=goal_id,
            node_id="work",
            attempt=0,
            task_revision=self.store.get_task(resolved_task_id)["state_revision"],
            expected_responsibility_revision=revision,
            current_owner=self.source_owner,
            proposed_owner=recipient,
            next_action={"action": "recipient must explicitly claim"},
            deadline=_FUTURE_DEADLINE,
        )

    def _claim(
        self,
        *,
        task_id: str | None = None,
        goal_id: str = "goal-main",
        command_id: str = "claim-main",
        revision: int = 2,
        claimant: str = "recipient-session",
    ) -> dict:
        resolved_task_id = task_id or self.task_id
        return self.ledger.claim_handoff(
            command_id=command_id,
            task_id=resolved_task_id,
            goal_id=goal_id,
            node_id="work",
            attempt=0,
            task_revision=self.store.get_task(resolved_task_id)["state_revision"],
            expected_responsibility_revision=revision,
            claimant=claimant,
            next_action={"action": "record next accountability step"},
            deadline=_FUTURE_DEADLINE,
        )

    def _defer(
        self,
        *,
        task_id: str | None = None,
        goal_id: str = "goal-main",
        command_id: str = "defer-main",
        revision: int = 1,
        wait_reason: dict | None = None,
        next_recheck_at: str | None = _FUTURE_RECHECK,
    ) -> dict:
        resolved_task_id = task_id or self.task_id
        return self.ledger.defer(
            command_id=command_id,
            task_id=resolved_task_id,
            goal_id=goal_id,
            node_id="work",
            attempt=0,
            task_revision=self.store.get_task(resolved_task_id)["state_revision"],
            expected_responsibility_revision=revision,
            current_owner=self.source_owner,
            wait_reason=wait_reason
            or {
                "wait_kind": "environment",
                "detail": "fixture host is unavailable",
                "release_condition": "fixture host responds",
            },
            next_action={"action": "recheck the declared condition"},
            deadline=_FUTURE_DEADLINE,
            next_recheck_at=next_recheck_at,
        )

    def test_unclaimed_expired_proposal_retains_original_owner(self) -> None:
        self._open()
        proposal = self.ledger.propose_handoff(
            command_id="expired-proposal",
            task_id=self.task_id,
            goal_id="goal-main",
            node_id="work",
            attempt=0,
            task_revision=1,
            expected_responsibility_revision=1,
            current_owner=self.source_owner,
            proposed_owner="recipient-session",
            next_action={"action": "recipient may claim before expiry"},
            deadline="2000-01-01T00:00:00+00:00",
        )
        inspected = self.ledger.inspect(task_id=self.task_id, goal_id="goal-main")

        self.assertEqual(proposal["state"], "handoff_proposed")
        self.assertEqual(inspected["original_owner"], self.source_owner)
        self.assertEqual(inspected["current_owner"], self.source_owner)
        self.assertEqual(inspected["proposed_owner"], "recipient-session")
        self.assertTrue(inspected["deadline_expired"])
        self.assertEqual(inspected["responsibility_revision"], 2)
        with self.assertRaisesRegex(StateConflictError, "expired"):
            self._claim(command_id="expired-claim")
        self.assertEqual(
            self.ledger.inspect(task_id=self.task_id, goal_id="goal-main")["current_owner"],
            self.source_owner,
        )

    def test_claim_is_atomic_for_competing_recipients_and_revision(self) -> None:
        self._open()
        self._propose(recipient="recipient-a")
        self._route(self.task_id, "recipient-a")
        self._route(self.task_id, "recipient-b")

        def claim(claimant: str, command_id: str) -> tuple[str, object]:
            try:
                return "ok", self._claim(command_id=command_id, claimant=claimant)
            except (PermissionError, StateConflictError) as error:
                return "error", error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: claim(*item),
                    (("recipient-a", "claim-a"), ("recipient-b", "claim-b")),
                )
            )

        successful = [result for status, result in results if status == "ok"]
        failures = [result for status, result in results if status == "error"]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], StateConflictError)
        inspected = self.ledger.inspect(task_id=self.task_id, goal_id="goal-main")
        self.assertEqual(inspected["current_owner"], "recipient-a")
        self.assertIsNone(inspected["proposed_owner"])
        self.assertEqual(inspected["responsibility_revision"], 3)

    def test_identical_command_replays_original_receipt_after_later_transition(self) -> None:
        first = self._open(command_id="stable-command")
        self._propose(command_id="later-transition")

        replay = self._open(command_id="stable-command")
        self.assertEqual(replay, first)
        self.assertEqual(
            self.ledger.inspect(task_id=self.task_id, goal_id="goal-main")["state"],
            "handoff_proposed",
        )
        with self.assertRaises(CommandConflictError):
            self.ledger.open(
                command_id="stable-command",
                task_id=self.task_id,
                goal_id="goal-main",
                node_id="work",
                attempt=0,
                task_revision=1,
                owner=self.source_owner,
                next_action={"action": "changed payload"},
                deadline=_FUTURE_DEADLINE,
            )
        with self.store.connection() as connection:
            receipts = connection.execute(
                "SELECT command_id FROM command_receipts WHERE task_id = ?", (self.task_id,)
            ).fetchall()
        self.assertEqual([row["command_id"] for row in receipts], [f"create-{self.task_id}"])

    def test_reopened_store_projects_existing_responsibility_snapshot(self) -> None:
        self._open()
        self._propose(recipient="recipient-reopen")
        self._route(self.task_id, "recipient-reopen")

        reopened_store = WorkbenchStore(self.path)
        reopened_store.initialize()
        reopened = ResponsibilityLedger(reopened_store)
        before = self.ledger.inspect(task_id=self.task_id, goal_id="goal-main")
        after = reopened.inspect(task_id=self.task_id, goal_id="goal-main")
        self.assertEqual(after, before)

        claimed = reopened.claim_handoff(
            command_id="claim-after-reopen",
            task_id=self.task_id,
            goal_id="goal-main",
            node_id="work",
            attempt=0,
            task_revision=1,
            expected_responsibility_revision=2,
            claimant="recipient-reopen",
            next_action={"action": "continue accountability"},
            deadline=_FUTURE_DEADLINE,
        )
        self.assertEqual(claimed["current_owner"], "recipient-reopen")

    def test_expired_deadline_does_not_create_an_automatic_takeover(self) -> None:
        opened = self._open(goal_id="expired-goal", command_id="expired-open", deadline="2000-01-01T00:00:00Z")
        inspected = self.ledger.inspect(task_id=self.task_id, goal_id="expired-goal")

        self.assertEqual(opened["current_owner"], self.source_owner)
        self.assertEqual(inspected["current_owner"], self.source_owner)
        self.assertIsNone(inspected["proposed_owner"])
        self.assertTrue(inspected["deadline_expired"])
        events = self.store.read_events(task_id=self.task_id)
        self.assertEqual(
            [event["event_type"] for event in events if event["event_type"].startswith("responsibility.")],
            ["responsibility.opened"],
        )

    def test_claim_does_not_create_a_recipient_route_or_access(self) -> None:
        self._open(goal_id="route-claim-goal", command_id="route-claim-open")
        self._propose(
            goal_id="route-claim-goal",
            command_id="route-claim-proposal",
            recipient="unrouted-recipient",
        )
        with self.assertRaises(PermissionError):
            self._claim(
                goal_id="route-claim-goal",
                command_id="route-claim-unrouted",
                claimant="unrouted-recipient",
            )
        unchanged = self.ledger.inspect(task_id=self.task_id, goal_id="route-claim-goal")
        self.assertEqual(unchanged["current_owner"], self.source_owner)
        self.assertEqual(unchanged["proposed_owner"], "unrouted-recipient")
        self._route(self.task_id, "unrouted-recipient")
        claimed = self._claim(
            goal_id="route-claim-goal",
            command_id="route-claim-routed",
            claimant="unrouted-recipient",
        )
        self.assertEqual(claimed["current_owner"], "unrouted-recipient")

    def test_pause_and_cancel_prevent_claim_even_after_explicit_wait_rebind(self) -> None:
        self._open(goal_id="paused-goal", command_id="paused-open")
        self._propose(goal_id="paused-goal", command_id="paused-proposal")
        self._route(self.task_id, "recipient-session")
        queued_revision = self.store.queue_task(self.task_id, expected_revision=1)
        paused_revision = self.store.transition_task(
            self.task_id,
            "paused",
            expected_revision=queued_revision,
        )
        deferred = self._defer(
            goal_id="paused-goal",
            command_id="paused-wait",
            revision=2,
            wait_reason={
                "wait_kind": "user_pause",
                "detail": "the user paused this task",
                "release_condition": "user explicitly resumes the task",
                "requires_user_action": True,
            },
            next_recheck_at=None,
        )
        self.assertEqual(paused_revision, 3)
        self.assertEqual(deferred["wait"]["wait_kind"], "user_pause")
        with self.assertRaises(StateConflictError):
            self._claim(goal_id="paused-goal", command_id="paused-claim", revision=3)
        paused = self.ledger.inspect(task_id=self.task_id, goal_id="paused-goal")
        self.assertEqual(paused["current_owner"], self.source_owner)

        cancelled_task = "cancelled-responsibility-task"
        self._create_task(cancelled_task, source_owner=self.source_owner)
        self._open(task_id=cancelled_task, goal_id="cancelled-goal", command_id="cancelled-open")
        self._propose(task_id=cancelled_task, goal_id="cancelled-goal", command_id="cancelled-proposal")
        self._route(cancelled_task, "recipient-session")
        cancelled_revision = self.store.transition_task(
            cancelled_task,
            "cancelled",
            expected_revision=1,
        )
        self._defer(
            task_id=cancelled_task,
            goal_id="cancelled-goal",
            command_id="cancelled-wait",
            revision=2,
        )
        self.assertEqual(cancelled_revision, 2)
        with self.assertRaises(StateConflictError):
            self._claim(task_id=cancelled_task, goal_id="cancelled-goal", command_id="cancelled-claim", revision=3)
        cancelled = self.ledger.inspect(task_id=cancelled_task, goal_id="cancelled-goal")
        self.assertEqual(cancelled["current_owner"], self.source_owner)

    def test_goals_on_one_session_are_independent(self) -> None:
        self._open(goal_id="goal-a", command_id="open-goal-a")
        self._open(goal_id="goal-b", command_id="open-goal-b")
        self._propose(goal_id="goal-a", command_id="propose-goal-a", recipient="recipient-a")

        first = self.ledger.inspect(task_id=self.task_id, goal_id="goal-a")
        second = self.ledger.inspect(task_id=self.task_id, goal_id="goal-b")
        self.assertEqual(first["state"], "handoff_proposed")
        self.assertEqual(first["proposed_owner"], "recipient-a")
        self.assertEqual(second["state"], "open")
        self.assertEqual(second["current_owner"], self.source_owner)
        self.assertIsNone(second["proposed_owner"])

    def test_typed_wait_requires_recheck_before_deadline_or_a_real_user_pause(self) -> None:
        self._open(goal_id="wait-goal", command_id="wait-open")
        wait_reason = {
            "wait_kind": "resource",
            "detail": "fixture capacity is unavailable",
            "release_condition": "capacity is observed",
        }
        with self.assertRaisesRegex(ValueError, "require next_recheck_at"):
            self._defer(
                goal_id="wait-goal",
                command_id="wait-missing-recheck",
                wait_reason=wait_reason,
                next_recheck_at=None,
            )
        with self.assertRaisesRegex(ValueError, "must not be after"):
            self._defer(
                goal_id="wait-goal",
                command_id="wait-after-deadline",
                wait_reason=wait_reason,
                next_recheck_at="2099-01-02T00:00:00+00:00",
            )
        deferred = self._defer(
            goal_id="wait-goal",
            command_id="wait-valid",
            wait_reason=wait_reason,
        )
        self.assertEqual(deferred["wait"], {
            "wait_kind": "resource",
            "detail": "fixture capacity is unavailable",
            "release_condition": "capacity is observed",
            "responsible_owner": self.source_owner,
            "next_recheck_at": _FUTURE_RECHECK,
            "requires_user_action": False,
        })

    def test_open_accepts_a_preexisting_permanent_route_when_contract_has_no_source(self) -> None:
        routed_task = "route-only-task"
        self._create_task(routed_task, source_owner=None)
        self._route(routed_task, "route-session")
        opened = self._open(
            task_id=routed_task,
            goal_id="route-goal",
            command_id="route-open",
            owner="route-session",
        )
        self.assertEqual(opened["current_owner"], "route-session")
        with self.assertRaises(PermissionError):
            self._open(
                task_id=routed_task,
                goal_id="unrouted-goal",
                command_id="unrouted-open",
                owner="unrouted-session",
            )

    def test_accepted_task_waits_for_incomplete_delivery_then_fulfills(self) -> None:
        self._open(goal_id="delivery-goal", command_id="delivery-open")
        objective = self._create_delivery(self.task_id)
        accepted = self._accept_task(self.task_id)

        self.assertEqual(
            self.ledger.reconcile_terminal(self.coordinator_epoch),
            [],
        )
        before_completion = self.ledger.inspect(task_id=self.task_id, goal_id="delivery-goal")
        self.assertEqual(before_completion["state"], "open")
        self.assertEqual(before_completion["current_owner"], self.source_owner)

        self._complete_delivery(objective["objective_id"])
        receipts = self.ledger.reconcile_terminal(self.coordinator_epoch)
        self.assertEqual(len(receipts), 1)
        fulfilled = receipts[0]
        self.assertEqual(fulfilled["state"], "fulfilled")
        self.assertEqual(fulfilled["operation"], "reconcile_terminal")
        self.assertEqual(fulfilled["task_revision"], accepted["state_revision"])
        self.assertEqual(fulfilled["current_owner"], self.source_owner)
        self.assertEqual(fulfilled["terminal_source"]["delivery_state"], "complete")
        self.assertEqual(
            self.ledger.inspect(task_id=self.task_id, goal_id="delivery-goal")["state"],
            "fulfilled",
        )

    def test_cancelled_task_records_non_success_terminal_state(self) -> None:
        self._open(goal_id="cancel-terminal-goal", command_id="cancel-terminal-open")
        self._propose(goal_id="cancel-terminal-goal", command_id="cancel-terminal-proposal")
        objective = self._create_delivery(self.task_id)
        self.store.transition_task(self.task_id, "cancelled", expected_revision=1)

        receipts = self.ledger.reconcile_terminal(self.coordinator_epoch)
        self.assertEqual(len(receipts), 1)
        cancelled = receipts[0]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertNotEqual(cancelled["state"], "fulfilled")
        self.assertEqual(cancelled["terminal_source"]["task_state"], "cancelled")
        self.assertEqual(cancelled["terminal_source"]["delivery_objective_id"], objective["objective_id"])
        self.assertEqual(cancelled["terminal_source"]["delivery_state"], "active")
        self.assertEqual(cancelled["current_owner"], self.source_owner)
        self.assertEqual(cancelled["proposed_owner"], "recipient-session")

    def test_stale_coordinator_epoch_rejects_terminal_reconciliation(self) -> None:
        self._open(goal_id="stale-epoch-goal", command_id="stale-epoch-open")
        stale_epoch = self.coordinator_epoch
        self.store.activate_coordinator(
            "responsibility-replacement-coordinator",
            "responsibility-test-machine",
        )

        with self.assertRaisesRegex(StateConflictError, "stale"):
            self.ledger.reconcile_terminal(stale_epoch)
        self.assertEqual(
            self.ledger.inspect(task_id=self.task_id, goal_id="stale-epoch-goal")["state"],
            "open",
        )

    def test_terminal_reconciliation_is_restart_idempotent(self) -> None:
        self._open(goal_id="restart-terminal-goal", command_id="restart-terminal-open")
        self.store.transition_task(self.task_id, "cancelled", expected_revision=1)
        first = self.ledger.reconcile_terminal(self.coordinator_epoch)
        self.assertEqual(len(first), 1)
        cursor = self.store.health()["cursor"]

        reopened_store = WorkbenchStore(self.path)
        reopened_store.initialize()
        reopened = ResponsibilityLedger(reopened_store)
        self.assertEqual(reopened.reconcile_terminal(self.coordinator_epoch), [])
        self.assertEqual(reopened_store.health()["cursor"], cursor)

    def test_unclaimed_handoff_and_paused_task_are_not_terminal_candidates(self) -> None:
        self._open(goal_id="pending-handoff-goal", command_id="pending-handoff-open")
        self._propose(goal_id="pending-handoff-goal", command_id="pending-handoff-proposal")
        self.assertEqual(self.ledger.reconcile_terminal(self.coordinator_epoch), [])
        pending = self.ledger.inspect(task_id=self.task_id, goal_id="pending-handoff-goal")
        self.assertEqual(pending["current_owner"], self.source_owner)
        self.assertEqual(pending["proposed_owner"], "recipient-session")

        paused_task = "paused-terminal-task"
        self._create_task(paused_task, source_owner=self.source_owner)
        self._open(task_id=paused_task, goal_id="paused-terminal-goal", command_id="paused-terminal-open")
        queued_revision = self.store.queue_task(paused_task, expected_revision=1)
        self.store.transition_task(paused_task, "paused", expected_revision=queued_revision)
        self.assertEqual(self.ledger.reconcile_terminal(self.coordinator_epoch), [])
        self.assertEqual(
            self.ledger.inspect(task_id=paused_task, goal_id="paused-terminal-goal")["state"],
            "open",
        )

    def test_terminal_candidates_are_filtered_before_the_bounded_limit(self) -> None:
        self._open(goal_id="earlier-nonterminal-goal", command_id="earlier-nonterminal-open")
        terminal_task = "later-terminal-task"
        self._create_task(terminal_task, source_owner=self.source_owner)
        self._open(
            task_id=terminal_task,
            goal_id="later-terminal-goal",
            command_id="later-terminal-open",
        )
        self._accept_task(terminal_task)

        receipts = self.ledger.reconcile_terminal(self.coordinator_epoch, limit=1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["task_id"], terminal_task)
        self.assertEqual(
            self.ledger.inspect(task_id=self.task_id, goal_id="earlier-nonterminal-goal")["state"],
            "open",
        )

    def test_list_for_task_paginates_latest_goal_snapshots(self) -> None:
        self._open(goal_id="list-goal-a", command_id="list-open-a")
        self._open(goal_id="list-goal-b", command_id="list-open-b")

        first = self.ledger.list_for_task(self.task_id, limit=1, cursor=0)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.ledger.list_for_task(
            self.task_id,
            limit=1,
            cursor=first["next_cursor"],
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            {first["items"][0]["goal_id"], second["items"][0]["goal_id"]},
            {"list-goal-a", "list-goal-b"},
        )


if __name__ == "__main__":
    unittest.main()
