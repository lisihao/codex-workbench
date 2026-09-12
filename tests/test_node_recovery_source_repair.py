"""Intent/receipt behavior of the controlled source repair adapter."""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from codex_workbench.node_recovery_source_repair import SourceRepairNodeActions
from codex_workbench.store import StateConflictError


_MODULE = "codex_workbench.node_recovery_source_repair"


class SourceRepairAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions = SourceRepairNodeActions(object())
        self.observation = {
            "task_id": "fixture", "node_id": "worker", "task_revision": 3,
            "node_attempt": 1, "contract_hash": "a" * 64,
            "observation_available": True, "node_state": "blocked",
            "node_is_verifier": False, "category": "validation_failure",
            "readiness_ready": True, "validation_succeeded": False,
        }
        self.preview = {"fingerprint": "b" * 64, "request_fingerprint": "c" * 64}
        self.receipt = {
            "request_id": "repair-fixture", "request_fingerprint": "c" * 64,
            "queued": True, "revision": 4, "next_attempt": 2,
            "authorization_event_cursor": 42,
        }

    def plan(self) -> dict:
        with patch(f"{_MODULE}.blocked_source_repair", return_value=self.preview) as prepare:
            plan = self.actions.prepare(self.observation, "repair_source", "repair-fixture")
        self.assertTrue(prepare.call_args.kwargs["dry_run"])
        return plan

    def test_execute_uses_frozen_preview_and_reports_only_requeue(self) -> None:
        plan = self.plan()
        raw = {**self.receipt, "changed_paths": ["src/private.txt"], "untracked_paths": ["src/private.txt"]}
        with patch(f"{_MODULE}.blocked_source_repair", return_value=raw) as execute:
            result = self.actions.execute(plan)
        self.assertFalse(execute.call_args.kwargs["dry_run"])
        self.assertEqual(execute.call_args.kwargs["expected_fingerprint"], self.preview["fingerprint"])
        self.assertEqual(execute.call_args.kwargs["request_id"], "repair-fixture")
        self.assertTrue(result["observation_patch"]["recovery_resumed"])
        self.assertNotIn("accepted", result)
        self.assertNotIn("changed_paths", result["receipt"])
        self.assertNotIn("untracked_paths", result["receipt"])
        self.assertEqual(result["receipt"], self.receipt)

    def test_missing_receipt_never_replays_the_operation(self) -> None:
        plan = self.plan()
        with patch(f"{_MODULE}.blocked_source_repair") as execute, patch(
            f"{_MODULE}.blocked_source_repair_receipt", return_value=None,
        ):
            self.assertIsNone(self.actions.reconcile(plan))
        execute.assert_not_called()

    def test_rejected_stale_preview_is_not_an_unknown_write(self) -> None:
        plan = self.plan()
        with patch(f"{_MODULE}.blocked_source_repair", side_effect=StateConflictError("fixture stale revision")), patch(
            f"{_MODULE}.blocked_source_repair_receipt", return_value=None,
        ):
            result = self.actions.execute(plan)
        self.assertTrue(result["known_effects"])
        self.assertFalse(result["stage_succeeded"])
        self.assertEqual(result["reason_kind"], "source_repair_rejected")

    def test_lost_response_is_reconciled_with_same_request(self) -> None:
        plan = self.plan()
        with patch(f"{_MODULE}.blocked_source_repair") as execute, patch(
            f"{_MODULE}.blocked_source_repair_receipt", return_value=self.receipt,
        ) as read:
            result = self.actions.reconcile(plan)
        execute.assert_not_called()
        self.assertEqual(read.call_args.args[1], "repair-fixture")
        self.assertTrue(result["known_effects"])

    def test_io_failure_remains_uncertain_until_receipt_lookup(self) -> None:
        plan = self.plan()
        with patch(f"{_MODULE}.blocked_source_repair", side_effect=OSError("fixture response lost")), patch(
            f"{_MODULE}.blocked_source_repair_receipt",
        ) as read:
            with self.assertRaises(OSError):
                self.actions.execute(plan)
        read.assert_not_called()
        with patch(f"{_MODULE}.blocked_source_repair") as execute, patch(
            f"{_MODULE}.blocked_source_repair_receipt", return_value=self.receipt,
        ):
            self.assertTrue(self.actions.reconcile(plan)["stage_succeeded"])
        execute.assert_not_called()

    def test_changed_durable_plan_or_receipt_cannot_dispatch_or_resolve(self) -> None:
        plan = self.plan()
        changed = deepcopy(plan)
        changed["repair_binding"]["arguments"]["expected_revision"] = 99
        with patch(f"{_MODULE}.blocked_source_repair") as execute:
            with self.assertRaisesRegex(ValueError, "durable source repair binding"):
                self.actions.execute(changed)
        execute.assert_not_called()
        with patch(f"{_MODULE}.blocked_source_repair_receipt", return_value={
            **self.receipt, "request_fingerprint": "d" * 64,
        }):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.actions.reconcile(plan)

    def test_unavailable_or_ineligible_observation_does_not_preview(self) -> None:
        for change in (
            {"observation_available": False}, {"node_is_verifier": True},
            {"node_state": "indeterminate"}, {"readiness_ready": False},
            {"category": "unknown_effects"}, {"validation_succeeded": True},
        ):
            with self.subTest(change=change), patch(f"{_MODULE}.blocked_source_repair") as prepare:
                with self.assertRaises(ValueError):
                    self.actions.prepare({**self.observation, **change}, "repair_source", "repair-fixture")
                prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
