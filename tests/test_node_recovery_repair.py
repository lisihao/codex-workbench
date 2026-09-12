"""Fixture-only coverage for the bounded journaled tooling-repair action."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.node_recovery import NodeRecoveryReconciler
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_repair import RepairNodeActions
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.submission import enqueue_natural_language_request


def _fingerprint(label: str) -> str:
    return (label.encode().hex() * 64)[:64]


class RepairNodeActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="node-recovery-repair-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repository, check=True)
        source = self.repository / "src" / "codex_workbench"
        source.mkdir(parents=True)
        (source / "runner.py").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repository, check=True)
        self.config = WorkbenchConfig(self.root / "state")
        self.config.initialize()
        self.base = WorkbenchStore(self.config.database)
        self.base.initialize()
        self.epoch = self.base.activate_coordinator("repair-fixture", "fixture-machine")
        self.task_id = "blocked-tooling-parent"
        self._create_parent_task()
        self.recovery = NodeRecoveryStore(self.base)
        self._configure_policy()
        self.server = WorkbenchMCPServer(self.config, self.base)
        self.calls: list[tuple[str, dict[str, object]]] = []

        def invoke(tool: str, arguments: dict[str, object]) -> dict:
            self.calls.append((tool, dict(arguments)))
            response = self.server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            })
            assert response is not None
            return response["result"]

        self.authority = AuthorityService(self.base, invoke, "repair-fixture-authority")
        self.actions = RepairNodeActions(self.config, self.recovery, self.authority)

    def _create_parent_task(self) -> None:
        contract = TaskContract(
            task_id=self.task_id,
            repository=str(self.repository),
            base_sha=self._head(),
            objective="parent implementation stays intact",
            allowed_scope=("src",),
            claude_allowed=False,
        )
        self.base.create_task(
            contract,
            [
                NodeSpec("worker", self.task_id, "implement", "fixture", "fixture", "blocked"),
                NodeSpec(
                    "verify", self.task_id, "verify", "fixture", "fixture", "accepted",
                    depends_on=("worker",), verifier=True,
                ),
            ],
            "create-blocked-tooling-parent",
        )
        with self.base.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET state = 'blocked', state_revision = 7 WHERE task_id = ?",
                (self.task_id,),
            )
            connection.execute(
                """
                UPDATE nodes SET state = 'blocked', attempt = 2
                WHERE task_id = ? AND node_id = 'worker'
                """,
                (self.task_id,),
            )

    def _configure_policy(
        self,
        *,
        enabled: bool = True,
        scopes: tuple[str, ...] = ("src/codex_workbench",),
        actions: tuple[str, ...] = ("request_repair",),
        max_action_attempts: int = 3,
    ) -> None:
        self.recovery.configure_policy(
            self.task_id,
            RecoveryPolicy(
                enabled=enabled,
                allowed_actions=actions,
                max_action_attempts=max_action_attempts,
                repair_repository=str(self.repository),
                repair_allowed_scopes=scopes,
            ),
            expected_task_revision=self._task()["state_revision"],
            actor="repair-fixture-policy",
        )

    def _head(self) -> str:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repository, text=True).strip()

    def _task(self) -> dict:
        return self.base.get_task(self.task_id)

    def _observation(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "task_id": self.task_id,
            "node_id": "worker",
            "node_attempt": 2,
            "task_revision": self._task()["state_revision"],
            "failure_fingerprint": _fingerprint("tooling-defect"),
            "category": "tooling_bug",
            "origin": "tooling_bug",
            "evidence_refs": {"structured-result": "sha256:" + _fingerprint("evidence") + ":result.json"},
        }
        value.update(overrides)
        return value

    @staticmethod
    def _recovery_decision(category: str, stage_key: str) -> dict[str, object]:
        return {
            "category": category,
            "state": "ready",
            "action": "observe_readiness",
            "owner": "authority",
            "reason_kind": "readiness_observation",
            "requires_authorization": False,
            "next_wakeup_at": None,
            "stage_key": stage_key,
        }

    def _record_repeated_failure(
        self,
        *,
        category: str = "environment",
        max_action_attempts: int = 2,
        unknown_final_receipt: bool = False,
        label: str = "repeat-stage",
    ) -> tuple[dict, dict[str, object]]:
        stage_key = "observe_readiness"
        self._configure_policy(
            actions=("observe_readiness", "request_repair"),
            max_action_attempts=max_action_attempts,
        )
        observation = self._observation(
            attempt=2,
            category=category,
            origin=category,
            failure_fingerprint=_fingerprint(label),
            phase="pre_execution",
            source_event_cursor=1,
        )
        episode = self.recovery.record_episode(
            observation,
            self._recovery_decision(category, stage_key),
        )
        for attempt in range(max_action_attempts):
            claimed = self.recovery.claim_due(
                episode["episode_id"],
                "repair-fixture-owner",
                self.epoch,
                expected_revision=episode["revision"],
                expected_node_attempt=2,
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            action_key = f"{label}-repeat-stage-{attempt}"
            intent = self.recovery.begin_action(
                claimed["episode_id"],
                action_key,
                _fingerprint(f"{attempt}-{label}"),
                {"stage_key": stage_key},
                owner_id="repair-fixture-owner",
                coordinator_epoch=self.epoch,
                lease_epoch=claimed["lease_epoch"],
                expected_revision=claimed["revision"],
                expected_node_attempt=2,
            )
            self.assertFalse(intent["existing"])
            self.assertEqual(intent["episode"]["episode_id"], episode["episode_id"])
            unknown = unknown_final_receipt and attempt == max_action_attempts - 1
            episode = self.recovery.settle_action(
                episode["episode_id"],
                action_key,
                owner_id="repair-fixture-owner",
                coordinator_epoch=self.epoch,
                lease_epoch=intent["episode"]["lease_epoch"],
                expected_revision=intent["episode"]["revision"],
                expected_node_attempt=2,
                receipt={"known_effects": not unknown, "stage_succeeded": False},
                next_decision=self._recovery_decision(category, stage_key),
                receipt_state="unknown" if unknown else "completed",
                effect_dispatched=True,
            )["episode"]
        return episode, {
            **observation,
            "repeated_recovery_failure": {
                "episode_id": episode["episode_id"],
                "stage_key": stage_key,
            },
        }

    def _seed_lost_repair_receipt(
        self,
        observation: dict[str, object],
        request_id: str,
    ) -> tuple[RepairNodeActions, dict, list[int]]:
        calls: list[int] = []

        def invoke(_tool: str, _arguments: dict[str, object]) -> dict:
            calls.append(1)
            return {"ok": True}

        authority = AuthorityService(self.base, invoke, "repeat-lost-receipt-authority")
        actions = RepairNodeActions(self.config, self.recovery, authority)
        plan = actions.prepare(observation, "request_repair", request_id)
        authority.dispatch({
            "request_id": plan["request_id"],
            "tool": "workbench_request",
            "task_id": plan["repair_task_id"],
            "arguments": plan["arguments"],
        })
        return actions, plan, calls

    def test_prepare_freezes_exact_head_scopes_parent_identity_and_inherited_claude(self) -> None:
        plan = self.actions.prepare(
            self._observation(summary="secret summary must never reach repair prompt"),
            "request_repair", "repair-authority-request-1",
        )
        self.assertEqual(plan["repository"], str(self.repository.resolve()))
        self.assertEqual(plan["base_sha"], self._head())
        self.assertEqual(plan["allowed_scopes"], ["src/codex_workbench"])
        self.assertEqual(plan["stage_key"], "request_repair")
        self.assertRegex(plan["repair_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertFalse(plan["claude_allowed"])
        self.assertEqual(plan["arguments"]["timeout_seconds"], 900)
        self.assertEqual(plan["arguments"]["retry_limit"], 1)
        self.assertFalse(plan["arguments"]["external_write_permission"])
        self.assertTrue(plan["arguments"]["queue"])
        self.assertNotIn("source_thread_id", plan["arguments"])
        self.assertNotIn("secret summary", json.dumps(plan, sort_keys=True))
        same = self.actions.prepare(self._observation(), "request_repair", "repair-authority-request-2")
        self.assertEqual((plan["repair_task_id"], plan["repair_request_id"]), (same["repair_task_id"], same["repair_request_id"]))

    def test_execute_enqueues_once_and_returns_bounded_link_receipt(self) -> None:
        plan = self.actions.prepare(self._observation(), "request_repair", "repair-authority-request")
        first = self.actions.execute(plan)
        second = self.actions.execute(plan)
        self.assertTrue(first["known_effects"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "workbench_request")
        arguments = self.calls[0][1]
        self.assertEqual(arguments["task_id"], plan["repair_task_id"])
        self.assertEqual(arguments["command_id"], plan["repair_request_id"])
        planning = self.base.get_planning_request(plan["repair_request_id"])
        self.assertEqual(planning["task_id"], plan["repair_task_id"])
        self.assertEqual(planning["request"]["base_sha"], self._head())
        receipt = first["receipt"]
        assert receipt is not None
        self.assertEqual(receipt["observation_patch"], {
            "repair_requested": True,
            "repair_linked": True,
            "repair_task_id": plan["repair_task_id"],
            "repair_request_id": plan["repair_request_id"],
        })
        self.assertNotIn("objective", receipt)

    def test_existing_planning_reservation_is_observed_not_enqueued_again(self) -> None:
        plan = self.actions.prepare(self._observation(), "request_repair", "repair-existing-request")
        args = plan["arguments"]
        enqueue_natural_language_request(
            self.config,
            self.base,
            objective=str(args["objective"]),
            repository=str(args["repository"]),
            allowed_scope=list(args["allowed_scopes"]),
            task_id=str(args["task_id"]),
            command_id=str(args["command_id"]),
            base_sha=str(args["base_sha"]),
            task_type="debugging",
            complexity="low",
            claude_allowed=bool(args["claude_allowed"]),
            timeout_seconds=900,
            retry_limit=1,
            external_write_permission=False,
            queue=True,
        )
        result = self.actions.execute(plan)
        self.assertTrue(result["known_effects"])
        self.assertEqual(result["receipt"]["receipt_source"], "planning_request")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.actions.reconcile(plan), result)

    def test_unknown_enqueue_is_reconciled_only_and_not_replayed(self) -> None:
        calls: list[int] = []

        def invoke(_tool: str, _arguments: dict[str, object]) -> dict:
            calls.append(1)
            raise RuntimeError("lost repair enqueue result")

        authority = AuthorityService(self.base, invoke, "repair-unknown-authority")
        actions = RepairNodeActions(self.config, self.recovery, authority)
        plan = actions.prepare(self._observation(), "request_repair", "repair-unknown-request")
        first = actions.execute(plan)
        self.assertFalse(first["known_effects"])
        self.assertEqual(first["journal_status"], "unknown")
        self.assertEqual(actions.execute(plan), first)
        self.assertEqual(actions.reconcile(plan), first)
        self.assertEqual(calls, [1])

    def test_known_enqueue_failure_is_a_bounded_failed_receipt(self) -> None:
        calls: list[int] = []

        def invoke(_tool: str, _arguments: dict[str, object]) -> dict:
            calls.append(1)
            return {"content": [{"type": "text", "text": "planning queue rejected"}], "isError": True}

        authority = AuthorityService(self.base, invoke, "repair-failed-authority")
        actions = RepairNodeActions(self.config, self.recovery, authority)
        plan = actions.prepare(self._observation(), "request_repair", "repair-failed-request")
        result = actions.execute(plan)
        self.assertTrue(result["known_effects"])
        self.assertEqual(result["journal_status"], "completed")
        self.assertEqual(result["receipt"], {"ok": False, "known_effects": True,
                                          "stage_succeeded": False, "error": "planning queue rejected"})
        self.assertEqual(actions.reconcile(plan), result)
        self.assertEqual(calls, [1])

    def test_repeated_known_stage_failures_enqueue_one_stable_repair_without_relabeling(self) -> None:
        episode, observation = self._record_repeated_failure()

        first = self.actions.prepare(observation, "request_repair", "repeat-repair-request-1")
        second = self.actions.prepare(observation, "request_repair", "repeat-repair-request-2")

        self.assertEqual(first["category"], "environment")
        self.assertEqual(first["repair_trigger"], {
            "episode_id": episode["episode_id"],
            "stage_key": "observe_readiness",
        })
        self.assertEqual(
            (first["repair_task_id"], first["repair_request_id"], first["repair_fingerprint"]),
            (second["repair_task_id"], second["repair_request_id"], second["repair_fingerprint"]),
        )
        self.assertIn("category=environment", first["arguments"]["objective"])
        self.assertNotIn("category=tooling_bug", first["arguments"]["objective"])

        first_receipt = self.actions.execute(first)
        second_receipt = self.actions.execute(second)

        self.assertTrue(first_receipt["known_effects"])
        self.assertTrue(second_receipt["known_effects"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.base.get_planning_request(first["repair_request_id"])["task_id"],
            first["repair_task_id"],
        )
        self.assertEqual(self.actions.reconcile(second), second_receipt)
        self.assertEqual(len(self.calls), 1)

    def test_reconciler_routes_a_durable_repeated_failure_to_one_repair_request(self) -> None:
        episode, observation = self._record_repeated_failure()

        def observe(_store, task_id, node_id, *, source_event_cursor=0):
            self.assertEqual((task_id, node_id), (self.task_id, "worker"))
            return {**observation, "source_event_cursor": source_event_cursor}

        loop = NodeRecoveryReconciler(
            self.base,
            coordinator_epoch=self.epoch,
            adapters={"request_repair": self.actions},
            observer=observe,
        )

        refreshed = loop._refresh_node(self.task_id, "worker", 0)
        dispatched = loop.reconcile_once()

        self.assertEqual((refreshed["category"], refreshed["action"]), ("environment", "request_repair"))
        self.assertEqual(len(dispatched), 1)
        repaired = self.recovery.get_episode(episode["episode_id"])
        self.assertIsNotNone(repaired["repair"])
        self.assertEqual(len(self.calls), 1)
        loop.reconcile_once()
        self.assertEqual(len(self.calls), 1)

    def test_repeated_repair_rejects_forged_or_unknown_stage_evidence(self) -> None:
        episode, observation = self._record_repeated_failure()
        forged = {
            **observation,
            "repeated_recovery_failure": {
                "episode_id": "forged-episode",
                "stage_key": "observe_readiness",
            },
        }

        with self.assertRaisesRegex(StateConflictError, "episode is unavailable"):
            self.actions.prepare(forged, "request_repair", "repeat-forged")

        valid = self.actions.prepare(observation, "request_repair", "repeat-valid")
        tampered_plan = {
            **valid,
            "repair_trigger": {
                "episode_id": episode["episode_id"],
                "stage_key": "narrow_validation:dsh-b-ipc-v1",
            },
        }
        with self.assertRaisesRegex(ValueError, "arguments are not bound"):
            self.actions.reconcile(tampered_plan)

        _unknown_episode, unknown = self._record_repeated_failure(
            unknown_final_receipt=True,
            label="repeat-unknown-stage",
        )
        with self.assertRaisesRegex(StateConflictError, "requires user intervention"):
            self.actions.prepare(unknown, "request_repair", "repeat-unknown")
        self.assertEqual(self.calls, [])

    def test_repeated_repair_requires_complete_explicit_failed_stage_receipts(self) -> None:
        missing_flag, observation = self._record_repeated_failure(label="repeat-missing-flag")
        with self.base.transaction() as connection:
            connection.execute(
                "UPDATE node_recovery_actions SET receipt_json = ? WHERE action_key = ?",
                (json.dumps({"known_effects": True}, sort_keys=True), missing_flag["actions"][0]["action_key"]),
            )
        with self.assertRaisesRegex(StateConflictError, "known failed receipts"):
            self.actions.prepare(observation, "request_repair", "repeat-missing-flag")

        missing_count, observation = self._record_repeated_failure(label="repeat-missing-count")
        with self.base.transaction() as connection:
            connection.execute(
                "DELETE FROM node_recovery_actions WHERE action_key = ?",
                (missing_count["actions"][0]["action_key"],),
            )
        with self.assertRaisesRegex(StateConflictError, "incomplete stage action receipts"):
            self.actions.prepare(observation, "request_repair", "repeat-missing-count")
        self.assertEqual(self.calls, [])

    def test_repeated_repair_rejects_paused_or_missing_request_repair_grant(self) -> None:
        _episode, observation = self._record_repeated_failure()
        with self.base.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'paused' WHERE task_id = ?", (self.task_id,))
        with self.assertRaisesRegex(StateConflictError, "paused"):
            self.actions.prepare(observation, "request_repair", "repeat-paused")
        with self.base.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'cancelled' WHERE task_id = ?", (self.task_id,))
        with self.assertRaisesRegex(StateConflictError, "paused or cancelled"):
            self.actions.prepare(observation, "request_repair", "repeat-cancelled")

    def test_repeated_repair_requires_current_request_repair_grant(self) -> None:
        _episode, observation = self._record_repeated_failure()
        self._configure_policy(actions=("observe_readiness",))

        with self.assertRaisesRegex(StateConflictError, "not authorized"):
            self.actions.prepare(observation, "request_repair", "repeat-no-grant")

    def test_repeated_repair_rejects_an_expired_episode_budget(self) -> None:
        _episode, observation = self._record_repeated_failure()

        with patch(
            "codex_workbench.node_recovery_repair.now_iso",
            return_value="9999-01-01T00:00:00+00:00",
        ):
            with self.assertRaisesRegex(StateConflictError, "time budget is exhausted"):
                self.actions.prepare(observation, "request_repair", "repeat-expired")

    def test_repeated_execute_rejects_a_pending_approval_without_enqueueing(self) -> None:
        _episode, observation = self._record_repeated_failure()
        plan = self.actions.prepare(observation, "request_repair", "repeat-pending-approval")
        with self.base.transaction() as connection:
            connection.execute(
                """
                INSERT INTO approvals(approval_id, task_id, kind, request_json, created_at)
                VALUES(?, ?, 'fixture_pending', '{}', '2026-09-11T00:00:00+00:00')
                """,
                ("repeat-pending-approval", self.task_id),
            )

        result = self.actions.execute(plan)

        self.assertEqual(result["journal_status"], "completed")
        self.assertTrue(result["known_effects"])
        self.assertEqual(
            result["receipt"],
            {
                "ok": False,
                "known_effects": True,
                "stage_succeeded": False,
                "reason_kind": "repair_admission_rejected",
                "observation_patch": {
                    "repair_enqueue_rejected": True,
                    "repair_requested": False,
                    "repair_linked": False,
                },
            },
        )
        self.assertEqual(self.calls, [])

    def test_repeated_reconcile_reads_a_known_lost_receipt_after_pause_or_policy_disable(self) -> None:
        _episode, observation = self._record_repeated_failure()
        actions, plan, calls = self._seed_lost_repair_receipt(observation, "repeat-lost-pause")
        with self.base.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'paused' WHERE task_id = ?", (self.task_id,))
        self._configure_policy(
            enabled=False,
            actions=("observe_readiness", "request_repair"),
        )

        receipt = actions.reconcile(plan)

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt["known_effects"])
        self.assertEqual(receipt["receipt"]["repair_request_id"], plan["repair_request_id"])
        self.assertEqual(calls, [1])
        self.assertEqual(self._task()["state"], "paused")
        self.assertEqual(next(node for node in self._task()["nodes"] if node["node_id"] == "worker")["state"], "blocked")

    def test_repeated_reconcile_reads_a_known_lost_receipt_after_deadline_expiry(self) -> None:
        episode, observation = self._record_repeated_failure()
        actions, plan, calls = self._seed_lost_repair_receipt(observation, "repeat-lost-deadline")
        with self.base.transaction() as connection:
            connection.execute(
                "UPDATE node_recovery_episodes SET time_budget_deadline_at = ? WHERE episode_id = ?",
                ("2020-01-01T00:00:00+00:00", episode["episode_id"]),
            )

        receipt = actions.reconcile(plan)

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt["known_effects"])
        self.assertEqual(receipt["receipt"]["repair_request_id"], plan["repair_request_id"])
        self.assertEqual(calls, [1])
        self.assertEqual(self._task()["state"], "blocked")
        self.assertEqual(next(node for node in self._task()["nodes"] if node["node_id"] == "worker")["state"], "blocked")

    def test_repeated_repair_unknown_enqueue_is_reconciled_without_replay(self) -> None:
        _episode, observation = self._record_repeated_failure()
        calls: list[int] = []

        def invoke(_tool: str, _arguments: dict[str, object]) -> dict:
            calls.append(1)
            raise RuntimeError("lost repeat repair enqueue result")

        actions = RepairNodeActions(
            self.config,
            self.recovery,
            AuthorityService(self.base, invoke, "repeat-unknown-authority"),
        )
        plan = actions.prepare(observation, "request_repair", "repeat-unknown-request")

        first = actions.execute(plan)
        second = actions.execute(plan)

        self.assertFalse(first["known_effects"])
        self.assertEqual(first["journal_status"], "unknown")
        self.assertEqual(second, first)
        self.assertEqual(actions.reconcile(plan), first)
        self.assertEqual(calls, [1])

    def test_policy_pause_scope_and_evidence_rejections_fail_before_enqueue(self) -> None:
        self._configure_policy(enabled=False)
        with self.assertRaisesRegex(StateConflictError, "disabled"):
            self.actions.prepare(self._observation(), "request_repair", "repair-disabled")
        self._configure_policy(enabled=True, scopes=(".git",))
        with self.assertRaisesRegex(ValueError, "Git worktree"):
            self.actions.prepare(self._observation(), "request_repair", "repair-git-scope")
        self._configure_policy()
        with self.assertRaisesRegex(StateConflictError, "explicit tooling_bug"):
            self.actions.prepare(
                self._observation(category="tooling_bug", origin="unknown"),
                "request_repair", "repair-ambiguous",
            )
        with self.base.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'paused' WHERE task_id = ?", (self.task_id,))
        with self.assertRaisesRegex(StateConflictError, "paused"):
            self.actions.prepare(self._observation(), "request_repair", "repair-paused")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
