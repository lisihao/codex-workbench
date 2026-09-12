"""Durable fixture coverage for the independent node recovery projection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NODE_RECOVERY_SCHEMA_SQL, NodeRecoveryStore
from codex_workbench.store import StateConflictError, WorkbenchStore


def _fingerprint(label: str) -> str:
    return (label.encode().hex() * 64)[:64]


class NodeRecoveryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="node-recovery-store-")
        self.addCleanup(self.temp.cleanup)
        self.base = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.base.initialize()
        with self.base.connection() as connection:
            connection.executescript(NODE_RECOVERY_SCHEMA_SQL)
        self.epoch = self.base.activate_coordinator("node-recovery-fixture", "fixture-machine")
        self.task_id = "recovery-task"
        contract = TaskContract(
            task_id=self.task_id,
            repository=str(Path(self.temp.name).resolve()),
            base_sha="fixture-base",
            objective="durable blocked recovery fixture",
            allowed_scope=("src",),
        )
        nodes = [
            NodeSpec("worker", self.task_id, "implement", "fixture", "fixture", "ok"),
            NodeSpec(
                "verify", self.task_id, "verify", "fixture", "fixture", "accepted",
                depends_on=("worker",), verifier=True,
            ),
        ]
        self.base.create_task(contract, nodes, "create-recovery-task")
        self.store = NodeRecoveryStore(self.base)
        self.policy = RecoveryPolicy(
            enabled=True,
            allowed_actions=("observe_readiness", "narrow_validation", "request_repair"),
            validation_profiles=("dsh-b-ipc-v1",),
            max_action_attempts=3,
            time_budget_seconds=900,
            backoff_seconds=30,
            max_backoff_seconds=300,
            repair_repository=str(Path(self.temp.name).resolve()),
            repair_allowed_scopes=("src",),
        )
        self.policy_row = self.store.configure_policy(
            self.task_id,
            self.policy,
            expected_task_revision=self._task_revision(),
            actor="fixture-operator",
        )

    def _task_revision(self) -> int:
        return self.base.get_task(self.task_id)["state_revision"]

    def _observation(
        self,
        label: str = "a",
        *,
        cursor: int = 1,
        phase: str = "blocked_observed",
        profile: str = "dsh-b-ipc-v1",
    ) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "node_id": "worker",
            "attempt": 0,
            "task_revision": self._task_revision(),
            "failure_fingerprint": _fingerprint(label),
            "origin": "authority_validation",
            "phase": phase,
            "source_event_cursor": cursor,
            "evidence_refs": {"validation": "sha256:" + _fingerprint(label) + ":validation.json"},
            "validation_profile": profile,
            "validation_succeeded": False,
            "repair_requested": False,
        }

    @staticmethod
    def _decision(
        action: str | None = "observe_readiness",
        *,
        state: str = "ready",
        reason: str = "readiness_observation",
        category: str = "environment",
        stage_key: str | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "category": category,
            "state": state,
            "action": action,
            "owner": "authority" if state != "needs_action" else "user",
            "reason_kind": reason,
            "requires_authorization": state == "needs_action",
            "next_wakeup_at": None,
        }
        if stage_key is not None:
            value["stage_key"] = stage_key
        return value

    def _record(self, label: str = "a", **kwargs: object) -> dict:
        return self.store.record_episode(self._observation(label, **kwargs), self._decision())

    def _claim(self, episode: dict) -> dict:
        claimed = self.store.claim_due(
            episode["episode_id"], "fixture-owner", self.epoch,
            expected_revision=episode["revision"], expected_node_attempt=episode["node_attempt"],
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return claimed

    def test_default_policy_is_disabled_and_configuration_is_task_revision_fenced(self) -> None:
        other = "default-policy-task"
        contract = TaskContract(
            task_id=other, repository=str(Path(self.temp.name).resolve()), base_sha="base",
            objective="other", allowed_scope=("src",),
        )
        self.base.create_task(contract, [
            NodeSpec("worker", other, "implement", "fixture", "fixture", "ok"),
            NodeSpec("verify", other, "verify", "fixture", "fixture", "accepted", depends_on=("worker",), verifier=True),
        ], "create-default-policy-task")
        default = self.store.get_policy(other)
        self.assertFalse(default["policy"]["enabled"])
        self.assertEqual(default["policy_revision"], 0)
        with self.assertRaisesRegex(StateConflictError, "task revision"):
            self.store.configure_policy(other, self.policy, expected_task_revision=99, actor="operator")

    def test_episode_deduplicates_unchanged_observation_without_new_progress_or_notification(self) -> None:
        decision = self._decision(None, state="needs_action", reason="authorization_required")
        observation = self._observation("dedupe")
        first = self.store.record_episode(observation, decision)
        events_before = self.base.read_events(task_id=self.task_id)
        repeated = self.store.record_episode(observation, decision)
        events_after = self.base.read_events(task_id=self.task_id)
        self.assertEqual(first["episode_id"], repeated["episode_id"])
        self.assertEqual(first["revision"], repeated["revision"])
        self.assertEqual(first["last_material_progress_at"], repeated["last_material_progress_at"])
        self.assertEqual(events_before, events_after)
        self.assertEqual(
            sum(event["event_type"] == "node_recovery.needs_action" for event in events_after),
            1,
        )

    def test_claim_intent_and_settlement_use_stage_local_attempt_budget(self) -> None:
        episode = self._record("stages")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "request-observe", _fingerprint("intent-observe"),
            {"stage_key": "observe_readiness", "input": "fixed"},
            owner_id="fixture-owner", coordinator_epoch=self.epoch,
            lease_epoch=claimed["lease_epoch"], expected_revision=claimed["revision"],
            expected_node_attempt=0,
        )
        self.assertFalse(intent["existing"])
        self.assertEqual(intent["episode"]["action_attempts"], 0)
        duplicate = self.store.begin_action(
            intent["episode"]["episode_id"], "request-observe-replayed", _fingerprint("intent-observe"),
            {"stage_key": "observe_readiness", "input": "fixed"},
            owner_id="fixture-owner", coordinator_epoch=self.epoch,
            lease_epoch=intent["episode"]["lease_epoch"], expected_revision=intent["episode"]["revision"],
            expected_node_attempt=0,
        )
        self.assertTrue(duplicate["existing"])
        self.assertEqual(duplicate["action"]["action_key"], "request-observe")
        settled = self.store.settle_action(
            intent["episode"]["episode_id"], "request-observe",
            owner_id="fixture-owner", coordinator_epoch=self.epoch,
            lease_epoch=intent["episode"]["lease_epoch"], expected_revision=intent["episode"]["revision"],
            expected_node_attempt=0, receipt={"stage_succeeded": True, "check": "ready"},
            next_decision=self._decision(
                "narrow_validation", reason="validation_authorized",
                stage_key="narrow_validation:dsh-b-ipc-v1",
            ),
            effect_dispatched=True,
        )
        current = settled["episode"]
        self.assertEqual(current["stage_attempts"]["observe_readiness"], 1)
        self.assertEqual(current["current_stage_key"], "narrow_validation:dsh-b-ipc-v1")
        self.assertEqual(current["action_attempts"], 0)
        self.assertEqual(current["action"], "narrow_validation")
        self.assertEqual(self.store.metrics(task_id=self.task_id)["dispatched_action_count"], 1)

    def test_stage_budget_resets_only_after_a_successful_stage_transition(self) -> None:
        one_attempt = RecoveryPolicy(
            enabled=True,
            allowed_actions=("observe_readiness", "narrow_validation"),
            validation_profiles=("dsh-b-ipc-v1",),
            max_action_attempts=1,
        )
        self.store.configure_policy(
            self.task_id, one_attempt, expected_task_revision=self._task_revision(), actor="one-attempt"
        )
        episode = self._record("stage-budget")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "stage-budget-observe", _fingerprint("stage-budget-observe"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        settled = self.store.settle_action(
            episode["episode_id"], "stage-budget-observe", owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=intent["episode"]["lease_epoch"],
            expected_revision=intent["episode"]["revision"], expected_node_attempt=0,
            receipt={"stage_succeeded": True}, effect_dispatched=True,
            next_decision=self._decision(
                "narrow_validation", reason="validation_authorized",
                stage_key="narrow_validation:dsh-b-ipc-v1",
            ),
        )["episode"]
        self.assertEqual(settled["stage_attempts"], {"observe_readiness": 1})
        self.assertEqual(settled["action_attempts"], 0)
        next_claim = self._claim(settled)
        next_intent = self.store.begin_action(
            next_claim["episode_id"], "stage-budget-validate", _fingerprint("stage-budget-validate"),
            {"stage_key": "narrow_validation:dsh-b-ipc-v1"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=next_claim["lease_epoch"],
            expected_revision=next_claim["revision"], expected_node_attempt=0,
        )
        exhausted = self.store.settle_action(
            next_claim["episode_id"], "stage-budget-validate", owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=next_intent["episode"]["lease_epoch"],
            expected_revision=next_intent["episode"]["revision"], expected_node_attempt=0,
            receipt={"stage_succeeded": False}, effect_dispatched=True,
            next_decision=self._decision(
                "narrow_validation", reason="validation_authorized",
                stage_key="narrow_validation:dsh-b-ipc-v1",
            ),
        )["episode"]
        self.assertEqual(exhausted["state"], "needs_action")
        self.assertEqual(exhausted["action_attempts"], 1)
        self.assertEqual(exhausted["stage_attempts"]["narrow_validation:dsh-b-ipc-v1"], 1)

    def test_settlement_keeps_receipt_after_the_action_advances_node_attempt(self) -> None:
        episode = self._record("attempt-drift")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "attempt-drift-action", _fingerprint("attempt-drift-action"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        with self.base.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET attempt = 1 WHERE task_id = ? AND node_id = 'worker'",
                (self.task_id,),
            )
        settled = self.store.settle_action(
            episode["episode_id"], "attempt-drift-action", owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=intent["episode"]["lease_epoch"],
            expected_revision=intent["episode"]["revision"], expected_node_attempt=0,
            receipt={"stage_succeeded": True, "result": "action advanced the node"},
            effect_dispatched=True, next_decision=self._decision(None, state="waiting", reason="fresh_observation"),
        )
        self.assertEqual(settled["action"]["state"], "completed")
        self.assertEqual(settled["action"]["receipt"]["result"], "action advanced the node")
        worker = next(node for node in self.base.get_task(self.task_id)["nodes"] if node["node_id"] == "worker")
        self.assertEqual(worker["attempt"], 1)

    def test_interrupted_intent_becomes_unknown_and_is_not_replayed(self) -> None:
        episode = self._record("interrupted")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "request-interrupted", _fingerprint("intent-interrupted"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        self.assertFalse(intent["existing"])
        self.assertEqual(self.store.recover_interrupted(), 1)
        recovered = self.store.get_episode(episode["episode_id"])
        self.assertEqual(recovered["actions"][0]["state"], "unknown")
        self.assertEqual(recovered["state"], "needs_action")
        self.assertIsNone(recovered["action"])
        self.assertIsNone(self.store.claim_due(
            recovered["episode_id"], "fixture-owner", self.epoch,
            expected_revision=recovered["revision"], expected_node_attempt=0,
        ))
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_verified_journal_receipt_reconciles_unknown_intent_without_replay(self) -> None:
        episode = self._record("reconcile")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "request-reconcile", _fingerprint("intent-reconcile"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        action = intent["action"]
        self.store.recover_interrupted()
        unknown = self.store.get_episode(episode["episode_id"])
        reconciled = self.store.reconcile_action_receipt(
            unknown["episode_id"], action["action_key"], action["action_fingerprint"],
            coordinator_epoch=self.epoch,
            expected_intent_coordinator_epoch=action["intent_coordinator_epoch"],
            expected_intent_lease_epoch=action["intent_lease_epoch"],
            expected_revision=unknown["revision"], expected_node_attempt=0,
            receipt={"stage_succeeded": True, "journal_receipt": "verified"},
            next_decision=self._decision(None, state="waiting", reason="reconciled_wait"),
            effect_dispatched=True,
        )
        self.assertEqual(reconciled["action"]["state"], "completed")
        self.assertEqual(reconciled["action"]["receipt"]["journal_receipt"], "verified")
        events = self.base.read_events(task_id=self.task_id)
        self.assertTrue(any(event["event_type"] == "node_recovery.action_interrupted" for event in events))
        self.assertTrue(any(event["event_type"] == "node_recovery.action_reconciled" for event in events))
        self.assertEqual(self.store.metrics(task_id=self.task_id)["total_action_count"], 1)

    def test_receipt_patch_and_repeated_blocked_observation_are_monotonic(self) -> None:
        episode = self._record("patch")
        claimed = self._claim(episode)
        intent = self.store.begin_action(
            claimed["episode_id"], "request-patch", _fingerprint("intent-patch"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        settled = self.store.settle_action(
            episode["episode_id"], "request-patch", owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=intent["episode"]["lease_epoch"],
            expected_revision=intent["episode"]["revision"], expected_node_attempt=0,
            receipt={
                "stage_succeeded": True,
                "observation_patch": {
                    "readiness_ready": True,
                    "validation_succeeded": True,
                    "repair_requested": True,
                    "source_event_cursor": 9,
                },
            },
            next_decision=self._decision(None, state="waiting", reason="after_readiness"),
            effect_dispatched=True,
        )["episode"]
        before_progress = settled["last_material_progress_at"]
        repeated = self.store.record_episode(
            self._observation("patch", cursor=1), self._decision("observe_readiness")
        )
        self.assertTrue(repeated["observation"]["readiness_ready"])
        self.assertTrue(repeated["observation"]["validation_succeeded"])
        self.assertTrue(repeated["observation"]["repair_requested"])
        self.assertEqual(repeated["source_event_cursor"], 9)
        self.assertEqual(repeated["decision"]["reason_kind"], "after_readiness")
        self.assertEqual(repeated["last_material_progress_at"], before_progress)
        refreshed = self.store.record_episode(
            self._observation("patch", cursor=10), self._decision("observe_readiness")
        )
        claimed = self._claim(refreshed)
        second = self.store.begin_action(
            claimed["episode_id"], "request-patch-invalidate", _fingerprint("intent-patch-invalidate"),
            {"stage_key": "observe_readiness"}, owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
            expected_revision=claimed["revision"], expected_node_attempt=0,
        )
        invalidated = self.store.settle_action(
            claimed["episode_id"], "request-patch-invalidate", owner_id="fixture-owner",
            coordinator_epoch=self.epoch, lease_epoch=second["episode"]["lease_epoch"],
            expected_revision=second["episode"]["revision"], expected_node_attempt=0,
            receipt={
                "stage_succeeded": True,
                "observation_patch": {
                    "readiness_ready": False,
                    "validation_succeeded": False,
                    "evidence_refs": {"new-input": "sha256:" + _fingerprint("new-input") + ":input.json"},
                },
            },
            next_decision=self._decision(None, state="waiting", reason="input_changed"),
            effect_dispatched=True,
        )["episode"]
        self.assertFalse(invalidated["observation"]["readiness_ready"])
        self.assertFalse(invalidated["observation"]["validation_succeeded"])
        self.assertIn("validation", invalidated["evidence_refs"])
        self.assertIn("new-input", invalidated["evidence_refs"])

    def test_due_timer_updates_decision_without_refreshing_material_progress(self) -> None:
        observation = self._observation("timer")
        waiting = self._decision(None, state="waiting", reason="backoff")
        waiting["next_wakeup_at"] = "2000-01-01T00:00:00+00:00"
        first = self.store.record_episode(observation, waiting)
        progressed = self.store.record_episode(observation, self._decision("observe_readiness"))
        self.assertEqual(progressed["state"], "ready")
        self.assertEqual(progressed["action"], "observe_readiness")
        self.assertGreater(progressed["revision"], first["revision"])
        self.assertEqual(progressed["last_material_progress_at"], first["last_material_progress_at"])
        events = self.base.read_events(task_id=self.task_id)
        self.assertTrue(any(event["event_type"] == "node_recovery.timer_decision_updated" for event in events))

    def test_enabled_task_sweep_is_bounded_and_cursor_paged(self) -> None:
        page = self.store.list_enabled_tasks(limit=1)
        self.assertEqual([row["task_id"] for row in page["tasks"]], [self.task_id])
        self.assertIsNone(page["next_cursor"])
        self.store.configure_policy(
            self.task_id,
            RecoveryPolicy(enabled=False, allowed_actions=("observe_readiness",)),
            expected_task_revision=self._task_revision(), actor="disable",
        )
        self.assertEqual(self.store.list_enabled_tasks(limit=100)["tasks"], [])

    def test_pause_fences_claim_but_preserves_existing_episode_evidence(self) -> None:
        episode = self._record("paused")
        self.base.transition_task(self.task_id, "queued", expected_revision=self._task_revision())
        self.base.transition_task(self.task_id, "paused", expected_revision=self._task_revision())
        self.assertIsNone(self.store.claim_due(
            episode["episode_id"], "fixture-owner", self.epoch,
            expected_revision=episode["revision"], expected_node_attempt=0,
        ))
        suspended = self.store.get_episode(episode["episode_id"])
        self.assertEqual(suspended["state"], "suspended")
        self.assertEqual(suspended["observation"]["failure_fingerprint"], _fingerprint("paused"))
        self.assertIsNone(suspended["owner_id"])

    def test_newer_or_disabled_policy_prevents_existing_episode_claim(self) -> None:
        episode = self._record("policy")
        changed = RecoveryPolicy(
            enabled=False,
            allowed_actions=("observe_readiness",),
            validation_profiles=(),
        )
        self.store.configure_policy(
            self.task_id, changed, expected_task_revision=self._task_revision(), actor="operator-2"
        )
        self.assertIsNone(self.store.claim_due(
            episode["episode_id"], "fixture-owner", self.epoch,
            expected_revision=episode["revision"], expected_node_attempt=0,
        ))

    def test_repair_link_is_one_per_failure_and_verified_deployment_is_recorded(self) -> None:
        episode = self._record("repair")
        claimed = self._claim(episode)
        deployment = _fingerprint("deployment")
        linked = self.store.link_repair(
            claimed["episode_id"], owner_id="fixture-owner", coordinator_epoch=self.epoch,
            lease_epoch=claimed["lease_epoch"], expected_revision=claimed["revision"],
            repair_request_id="repair-request-1", repair_task_id="repair-task-1",
            repair_fingerprint=deployment,
        )
        self.assertEqual(linked["repair_request_id"], "repair-request-1")
        repeated = self.store.link_repair(
            linked["episode_id"], owner_id="fixture-owner", coordinator_epoch=self.epoch,
            lease_epoch=linked["lease_epoch"], expected_revision=linked["revision"],
            repair_request_id="repair-request-1", repair_task_id="repair-task-1",
            repair_fingerprint=deployment,
        )
        self.assertEqual(repeated["revision"], linked["revision"])
        deployed = self.store.mark_repair_deployed(
            linked["episode_id"], expected_revision=linked["revision"], expected_node_attempt=0,
            verified_deployment_fingerprint=_fingerprint("verified-deployment"),
            expected_repair_fingerprint=deployment,
            evidence_refs={"deployment": "sha256:" + deployment + ":deployment.json"},
            verified_by="fixture-deployment-verifier",
        )
        self.assertEqual(deployed["repair"]["repair_fingerprint"], deployment)
        self.assertEqual(deployed["repair"]["verified_deployment_fingerprint"], _fingerprint("verified-deployment"))
        self.assertIsNotNone(deployed["repair"]["deployed_at"])

    def test_cursor_cas_and_bounded_documents_reject_unsafe_history(self) -> None:
        self.assertEqual(self.store.read_cursor(), 0)
        self.assertEqual(self.store.advance_cursor(0, 4), 4)
        with self.assertRaises(StateConflictError):
            self.store.advance_cursor(0, 5)
        with self.assertRaisesRegex(ValueError, "changed_paths"):
            self.store.record_episode(
                {**self._observation("unsafe"), "changed_paths": ["src/secret.py"]}, self._decision()
            )
        with self.assertRaisesRegex(StateConflictError, "stage"):
            episode = self._record("stage-input")
            claimed = self._claim(episode)
            self.store.begin_action(
                claimed["episode_id"], "bad-stage", _fingerprint("bad-stage"),
                {"stage_key": "narrow_validation:dsh-b-ipc-v1"}, owner_id="fixture-owner",
                coordinator_epoch=self.epoch, lease_epoch=claimed["lease_epoch"],
                expected_revision=claimed["revision"], expected_node_attempt=0,
            )


if __name__ == "__main__":
    unittest.main()
