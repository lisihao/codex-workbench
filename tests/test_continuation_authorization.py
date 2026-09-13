"""Loop/store evidence for opt-in task-scoped recovery continuation."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.continuation_authorization import action_authorization
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_hash, canonical_json, now_iso
from codex_workbench.node_recovery import NodeRecoveryReconciler
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.store import WorkbenchStore


CONTINUATION = {
    "same_task_only": True,
    "allow_across_attempts": True,
    "allow_across_revisions": True,
}


class FixtureActions:
    """Fixed internal plans whose preview fields model the native adapter seam."""

    def __init__(self, owner: "ContinuationAuthorizationTests") -> None:
        self.owner = owner
        self.calls: list[str] = []
        self.receipts: dict[str, dict] = {}
        self.unknown_narrow = False
        self.preview_sequence = 0

    def prepare(self, observation: dict, action: str, request_id: str, **_kwargs: object) -> dict:
        plan = {
            "action": action,
            "request_id": request_id,
            "stage_key": action,
            "task_id": observation["task_id"],
            "node_id": observation["node_id"],
            "node_attempt": observation["node_attempt"],
            "task_revision": observation["task_revision"],
        }
        if action in {"narrow_validation", "source_only_recovery"}:
            self.preview_sequence += 1
            digest = lambda label: sha256(f"{label}:{self.preview_sequence}".encode()).hexdigest()
        if action == "narrow_validation":
            plan.update({
                "validation_profile": "dsh-b-ipc-v1",
                "fresh_preview": {
                    "worktree": str(self.owner.root),
                    "fingerprint": digest("preview"),
                    "source_delta_sha256": digest("source"),
                    "install_manifest_sha256": digest("install"),
                    "runtime_fingerprint": digest("runtime"),
                },
                "arguments": {"check_id": "dsh-b-ipc-v1", "dry_run": False},
            })
        elif action == "source_only_recovery":
            plan.update({
                "fresh_preview": {
                    "source_delta_sha256": digest("source"),
                    "install_manifest_sha256": digest("install"),
                    "runtime_fingerprint": digest("runtime"),
                },
                "arguments": {
                    "source_only": True,
                    "dry_run": False,
                    "preserve_untracked": True,
                    "confirm_source_only_extraction": True,
                    "confirm_preserve_unknown_ignored": True,
                },
            })
        return plan

    def execute(self, plan: dict) -> dict | None:
        self.calls.append(plan["action"])
        if plan["action"] == "observe_readiness":
            self.owner.ready = True
            receipt = {
                "ok": True,
                "known_effects": True,
                "stage_succeeded": True,
                "evidence_refs": [],
                "observation_patch": {"readiness_ready": True},
            }
        elif plan["action"] == "narrow_validation" and self.unknown_narrow:
            return None
        else:
            receipt = {
                "ok": True,
                "known_effects": True,
                "stage_succeeded": True,
                "evidence_refs": [],
                "observation_patch": {},
            }
        self.receipts[plan["request_id"]] = receipt
        return receipt

    def reconcile(self, plan: dict) -> dict | None:
        return self.receipts.get(plan["request_id"])


class ContinuationAuthorizationTests(unittest.TestCase):
    """Exercise current-task reauthorization without relaxing action fences."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wb-continuation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("continuation-fixture", "fixture-machine")
        self.task_id = "continuation-parent"
        self.contract = TaskContract(
            self.task_id,
            str(self.root),
            "fixture-base",
            "continue the original bounded recovery objective",
            allowed_scope=("src",),
            retry_limit=0,
        )
        self.store.create_task(
            self.contract,
            [
                NodeSpec("N", self.task_id, "worker", "fixture", "fixture", "blocked", write_scopes=("src",)),
                NodeSpec("V", self.task_id, "verify", "fixture", "fixture", "verify", depends_on=("N",), verifier=True),
            ],
            "create-continuation-fixture",
        )
        self.store.queue_task(self.task_id)
        claimed = self.store.claim_ready_node("continuation-fixture", self.epoch)
        assert claimed is not None
        self.store.settle_claimed(claimed, NodeResult("blocked", "fixture validation failure"))
        self.recovery = NodeRecoveryStore(self.store)
        self.ready = False
        self.validation_complete = False
        self.category = "validation_failure"
        self.actions = FixtureActions(self)
        self.loop: NodeRecoveryReconciler | None = None

    def _configure(
        self,
        actions: tuple[str, ...] = ("observe_readiness", "narrow_validation"),
        *,
        continuation: dict[str, bool] | None = CONTINUATION,
    ) -> dict:
        policy_fields: dict[str, object] = {
            "enabled": True,
            "allowed_actions": actions,
            "validation_profiles": ("dsh-b-ipc-v1",),
        }
        if continuation is not None:
            policy_fields["continuation_authorization"] = continuation
        policy = RecoveryPolicy(**policy_fields)
        row = self.recovery.configure_policy(
            self.task_id,
            policy,
            expected_task_revision=self.store.get_task(self.task_id)["state_revision"],
            actor="fixture-operator",
        )
        self.loop = NodeRecoveryReconciler(
            self.store,
            coordinator_epoch=self.epoch,
            adapters={action: self.actions for action in actions},
            observer=self._observe,
            monotonic=lambda: 0,
        )
        return row

    def _observe(self, store: WorkbenchStore, task_id: str, node_id: str, *, source_event_cursor: int = 0) -> dict:
        task = store.get_task(task_id)
        node = next(item for item in task["nodes"] if item["node_id"] == node_id)
        return {
            "task_id": task_id,
            "node_id": node_id,
            "node_attempt": node["attempt"],
            "attempt": node["attempt"],
            "task_revision": task["state_revision"],
            "task_state": task["state"],
            "node_state": node["state"],
            "node_is_verifier": False,
            "category": self.category,
            "origin": "authority-fixture",
            "phase": "verification",
            "implementation_ready": self.ready,
            "readiness_ready": self.ready,
            "validated_profiles": ["dsh-b-ipc-v1"] if self.validation_complete else [],
            "observation_available": True,
            "source_event_cursor": source_event_cursor,
            "evidence_refs": [],
            "failure_fingerprint": canonical_hash({
                "task": task_id,
                "node": node_id,
                "attempt": node["attempt"],
            }),
        }

    def _episode(self) -> dict:
        rows = self.recovery.list_summary(task_id=self.task_id, limit=10)
        self.assertEqual(len(rows), 1)
        return self.recovery.get_episode(rows[0]["episode_id"])

    def _set_contract(self, **changes: object) -> None:
        task = self.store.get_task(self.task_id)
        contract = {**task["contract"], **changes}
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET contract_json = ?, contract_hash = ?, state_revision = state_revision + 1 "
                "WHERE task_id = ?",
                (canonical_json(contract), canonical_hash(contract), self.task_id),
            )

    def _set_node_write_scopes(self, scopes: list[str]) -> None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT spec_json FROM nodes WHERE task_id = ? AND node_id = 'N'",
                (self.task_id,),
            ).fetchone()
            assert row is not None
            specification = json.loads(str(row["spec_json"]))
            specification["write_scopes"] = scopes
            connection.execute(
                "UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = 'N'",
                (canonical_json(specification), self.task_id),
            )
            connection.execute(
                "UPDATE tasks SET state_revision = state_revision + 1 WHERE task_id = ?",
                (self.task_id,),
            )

    def test_configure_captures_authority_scope_and_first_readiness_remains_legacy_guard(self) -> None:
        row = self._configure()
        capture = row["continuation_capture"]
        self.assertIsInstance(capture, dict)
        assert isinstance(capture, dict)
        self.assertEqual(capture["goal"]["task_id"], self.task_id)
        self.assertEqual(capture["scope"]["allowed_scope"], ["src"])
        self.assertEqual(capture["permissions"], {
            "external_write_permission": False,
            "destructive_action_permission": False,
        })
        self.assertEqual(capture["risk_boundary"]["risk_level"], "low")
        self.assertTrue(capture["risk_boundary"]["reversible"])
        self.assertEqual(capture["risk_boundary"]["severe_adverse_side_effects"], "forbidden")
        self.assertIn("fresh_native_preview", capture["rollback_conditions"])

        assert self.loop is not None
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["observe_readiness"])
        first = self._episode()
        self.assertNotIn("continuation_authorization", first["actions"][0]["action_input"])
        self.assertFalse(first["decision"]["continuation_authorization"]["applicable"])
        self.assertEqual(
            first["decision"]["continuation_authorization"]["reason_kind"],
            "not_applicable_existing_action_guard",
        )

        self.loop._next_sweep = 0
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["observe_readiness", "narrow_validation"])
        applied = self._episode()
        narrow = next(item for item in applied["actions"] if item["stage_key"] == "narrow_validation:dsh-b-ipc-v1")
        binding = narrow["action_input"]["continuation_authorization"]
        self.assertTrue(binding["authorized"])
        self.assertEqual(binding["reason_kind"], "fresh_native_preview_authorized")
        self.assertEqual(binding["goal"]["task_id"], self.task_id)
        self.assertEqual(binding["native_preview"]["kind"], "controlled_validation")
        self.assertEqual(binding["risk_level"], "low")
        self.assertTrue(binding["reversible"])
        self.assertEqual(binding["severe_adverse_side_effects"], "forbidden")
        self.assertIn("native_preview_fingerprint_matches_execution", binding["rollback_conditions"])
        self.assertEqual(applied["decision"]["continuation_authorization"], binding)

    def test_source_only_recovery_uses_the_same_preview_backed_authorization(self) -> None:
        self._configure(actions=("source_only_recovery",))
        self.ready = True
        self.validation_complete = True
        assert self.loop is not None
        self.loop.reconcile_once()
        episode = self._episode()
        action = episode["actions"][0]
        binding = action["action_input"]["continuation_authorization"]
        self.assertEqual(self.actions.calls, ["source_only_recovery"])
        self.assertTrue(binding["authorized"])
        self.assertEqual(binding["native_preview"]["kind"], "source_only_recovery")
        self.assertEqual(binding["action_scope"], {
            "source_only": True,
            "preserve_untracked": True,
        })

    def test_new_attempt_uses_a_new_fresh_plan_but_reuses_same_task_capture(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        assert self.loop is not None
        self.loop.reconcile_once()
        first = self._episode()
        first_action = first["actions"][0]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET attempt = 2 WHERE task_id = ? AND node_id = 'N'",
                (self.task_id,),
            )
            connection.execute(
                "UPDATE tasks SET state_revision = state_revision + 1 WHERE task_id = ?",
                (self.task_id,),
            )
        second = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual(second["node_attempt"], 2)
        self.assertEqual(second["state"], "ready")
        claimed = self.recovery.claim_due(
            second["episode_id"],
            self.loop.owner_id,
            self.epoch,
            expected_revision=second["revision"],
            expected_node_attempt=2,
        )
        assert claimed is not None
        self.loop._execute(claimed)
        rows = self.recovery.list_summary(task_id=self.task_id, limit=10)
        second_episode = self.recovery.get_episode(
            next(item["episode_id"] for item in rows if item["node_attempt"] == 2)
        )
        second_action = second_episode["actions"][0]
        self.assertEqual(second_action["action_input"]["node_attempt"], 2)
        self.assertNotEqual(second_action["action_key"], first_action["action_key"])
        self.assertNotEqual(
            second_action["action_input"]["fresh_preview"]["fingerprint"],
            first_action["action_input"]["fresh_preview"]["fingerprint"],
        )

    def _assert_current_boundary_block(self, changes: dict[str, object], expected: str) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        self._set_contract(**changes)
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual((episode["state"], episode["action"]), ("needs_action", None))
        self.assertEqual(episode["decision"]["reason_kind"], expected)
        self.assertEqual(self.actions.calls, [])

    def test_allowed_scope_change_does_not_inherit_the_capture(self) -> None:
        self._assert_current_boundary_block(
            {"allowed_scope": ["src", "migration"]}, "continuation_scope_changed"
        )

    def test_forbidden_scope_change_does_not_inherit_the_capture(self) -> None:
        self._assert_current_boundary_block(
            {"forbidden_scope": ["src/private"]}, "continuation_scope_changed"
        )

    def test_node_write_scope_change_does_not_inherit_the_capture(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        self._set_node_write_scopes(["src", "migration"])
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual((episode["state"], episode["action"]), ("needs_action", None))
        self.assertEqual(episode["decision"]["reason_kind"], "continuation_scope_changed")

    def test_objective_change_does_not_inherit_the_capture(self) -> None:
        self._assert_current_boundary_block(
            {"objective": "a materially different objective"}, "continuation_goal_changed"
        )

    def test_external_permission_change_does_not_inherit_the_capture(self) -> None:
        self._assert_current_boundary_block(
            {"external_write_permission": True}, "continuation_permission_boundary_changed"
        )

    def test_destructive_permission_change_does_not_inherit_the_capture(self) -> None:
        self._assert_current_boundary_block(
            {"destructive_action_permission": True}, "continuation_permission_boundary_changed"
        )

    def test_false_attempt_flag_blocks_new_attempt(self) -> None:
        self._configure(
            actions=("narrow_validation",),
            continuation={**CONTINUATION, "allow_across_attempts": False},
        )
        self.ready = True
        with self.store.transaction() as connection:
            connection.execute("UPDATE nodes SET attempt = 2 WHERE task_id = ? AND node_id = 'N'", (self.task_id,))
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual(episode["decision"]["reason_kind"], "continuation_node_attempt_changed")

    def test_false_revision_flag_blocks_current_task_revision_change(self) -> None:
        self._configure(
            actions=("narrow_validation",),
            continuation={**CONTINUATION, "allow_across_revisions": False},
        )
        self.ready = True
        with self.store.transaction() as connection:
            connection.execute("UPDATE tasks SET state_revision = state_revision + 1 WHERE task_id = ?", (self.task_id,))
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual(episode["decision"]["reason_kind"], "continuation_task_revision_changed")

    def test_pause_blocks_current_recovery(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        with self.store.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'paused' WHERE task_id = ?", (self.task_id,))
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual(episode["decision"]["reason_kind"], "user_pause")
        self.assertEqual(self.actions.calls, [])

    def test_cancel_blocks_current_recovery(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        with self.store.transaction() as connection:
            connection.execute("UPDATE tasks SET state = 'cancelled' WHERE task_id = ?", (self.task_id,))
        assert self.loop is not None
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual(episode["decision"]["reason_kind"], "user_pause")
        self.assertEqual(self.actions.calls, [])

    def test_current_pending_approval_blocks_but_historical_fail_does_not(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals(approval_id, task_id, kind, request_json, decision, decided_at, created_at) "
                "VALUES(?, ?, 'indeterminate_resolution', ?, 'fail', ?, ?)",
                ("historical-fail", self.task_id, json.dumps({"node_id": "other", "attempt": 1}), now_iso(), now_iso()),
            )
        assert self.loop is not None
        self.assertEqual(self.loop._refresh_node(self.task_id, "N", 0)["state"], "ready")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals(approval_id, task_id, kind, request_json, decision, created_at) "
                "VALUES(?, ?, 'indeterminate_resolution', ?, NULL, ?)",
                ("current-pending", self.task_id, json.dumps({"node_id": "N", "attempt": 1}), now_iso()),
            )
        episode = self.loop._refresh_node(self.task_id, "N", 0)
        self.assertEqual((episode["state"], episode["decision"]["reason_kind"]), (
            "needs_action", "approval_pending",
        ))

    def test_unknown_continuation_effect_reconciles_without_duplicate_execute(self) -> None:
        self._configure(actions=("narrow_validation",))
        self.ready = True
        self.actions.unknown_narrow = True
        assert self.loop is not None
        self.loop.reconcile_once()
        episode = self._episode()
        self.assertEqual(episode["actions"][0]["state"], "unknown")
        self.assertEqual(self.actions.calls, ["narrow_validation"])
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["narrow_validation"])

    def test_legacy_policy_has_no_continuation_metadata(self) -> None:
        row = self._configure(actions=("narrow_validation",), continuation=None)
        self.assertIsNone(row["continuation_capture"])
        self.ready = True
        assert self.loop is not None
        self.loop.reconcile_once()
        episode = self._episode()
        self.assertNotIn("continuation_authorization", episode["actions"][0]["action_input"])
        self.assertNotIn("continuation_authorization", episode["decision"])

    def test_unapproved_capture_release_scope_and_child_goal_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuation_authorization"):
            RecoveryPolicy.from_dict({
                "enabled": True,
                "continuation_authorization": {**CONTINUATION, "capture": {}},
            })
        self._set_contract(external_write_permission=True)
        with self.assertRaisesRegex(ValueError, "no external or destructive permissions"):
            self._configure(actions=("narrow_validation",))
        self._set_contract(external_write_permission=False)
        row = self._configure(actions=("narrow_validation",))
        self.ready = True
        plan = self.actions.prepare(self._observe(self.store, self.task_id, "N"), "narrow_validation", "child-plan")
        task = self.store.get_task(self.task_id)
        assert isinstance(row["continuation_capture"], dict)
        release = action_authorization(
            row["policy"], row["continuation_capture"],
            policy_revision=row["policy_revision"], task=task, node_id="N",
            action_plan={**plan, "arguments": {**plan["arguments"], "release_tag": "v9.9.9"}},
            approval_pending=False, approval_denied=False, observation_available=True,
        )
        self.assertEqual((release["authorized"], release["reason_kind"]), (
            False, "external_release_scope_forbidden",
        ))
        child = action_authorization(
            row["policy"], row["continuation_capture"],
            policy_revision=row["policy_revision"], task=task, node_id="N",
            action_plan={**plan, "task_id": "child-with-external-permission"},
            approval_pending=False, approval_denied=False, observation_available=True,
        )
        self.assertEqual((child["authorized"], child["reason_kind"]), (False, "goal_changed"))


if __name__ == "__main__":
    unittest.main()
