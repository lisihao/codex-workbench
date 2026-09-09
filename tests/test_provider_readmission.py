from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.service import Coordinator
from tests import test_failed_attempt_recovery as recovery_fixtures
from tests import test_service as service_fixtures


class ProviderReadmissionTests(recovery_fixtures._FailedAttemptRecoveryFixture, unittest.TestCase):
    def _retry(self, *, reason: str, kind: str, recover_files: bool = True):
        contract = TaskContract(
            task_id="provider-readmission", repository=str(self.repository),
            base_sha=self.base_sha, objective="retry the owned provider failure",
            allowed_scope=("src",), retry_limit=1,
        )
        worker = NodeSpec("worker", contract.task_id, "worker", "claude", "sonnet",
                          "bounded work", write_scopes=("src",))
        verifier = NodeSpec("verify", contract.task_id, "verify", "fixture", "fixture",
                            "accepted", depends_on=("worker",), verifier=True)
        self.store.create_task(contract, [worker, verifier], "create-readmission")
        self.store.queue_task(contract.task_id)
        first = self.store.claim_ready_node("first", self.epoch)
        source = None
        if recover_files:
            source = self.worktrees.prepare(contract.repository, contract.base_sha, contract.task_id, "worker", 1)
            self.store.assign_worktree(contract.task_id, "worker", str(source), attempt=1,
                                      coordinator_epoch=self.epoch, lease_epoch=first["lease_epoch"])
            (source / "src/value.txt").write_text("owned recovery content\n")
            (source / "src/new.txt").write_text("owned untracked content\n")
        self.store.record_node_route(
            contract.task_id, "worker", executor="codex", model="gpt-5.6-terra",
            payload={"attempt": 1, "from": "claude", "to": "codex", "model": "gpt-5.6-terra",
                     "reason": reason, "fallback_kind": kind},
            attempt=1, coordinator_epoch=self.epoch, lease_epoch=first["lease_epoch"],
        )
        current = next(n for n in self.store.get_task(contract.task_id)["nodes"] if n["node_id"] == "worker")
        self.assertEqual((current["state"], current["attempt"], current["effective_executor"]), ("running", 1, "codex"))
        self.store.settle_claimed(first, NodeResult(
            "failed", "fallback transport ended; owned retry authorized", retryable=True,
            actual_model="gpt-5.6-terra", result_kind="worker",
            changed_paths=("src/new.txt", "src/value.txt") if source else (),
        ))
        second = self.store.claim_ready_node("second", self.epoch)
        self.assertEqual(second["attempt"], 2)
        if source:
            self.assertEqual((source / "src/value.txt").read_text(), "owned recovery content\n")
            self.assertEqual((source / "src/new.txt").read_text(), "owned untracked content\n")
            self.assertIsNotNone(second.get("failed_attempt_recovery"))
        return contract, second

    def test_cli_envelope_failure_reconsiders_claude_after_owned_recovery(self) -> None:
        contract, second = self._retry(
            reason="Claude structured result rejected: CLI JSON response must be an object",
            kind="claude-executor-failed",
        )
        self.assertEqual((second["spec"]["executor"], second["spec"]["model"]), ("claude", "sonnet"))
        started = [e for e in self.store.read_events(task_id=contract.task_id) if e["event_type"] == "node.started"][-1]
        self.assertEqual(started["payload"]["provider_readmission"]["source_attempt"], 1)
        self.assertTrue(started["payload"]["provider_readmission"]["requires_current_admission"])
        decision = Coordinator._claude_decision(second["spec"], contract.to_dict(), None)
        self.assertEqual(decision.action, "codex")
        self.assertIn("quota is unknown", decision.reason)
        quota = QuotaSnapshot(observed_at=now_iso(), auth_ok=True,
                              auth_method="native-subscription", five_hour_remaining=25,
                              weekly_all_remaining=80, weekly_sonnet_remaining=80,
                              **service_fixtures.compatible_provenance())
        protected = Coordinator._claude_decision(second["spec"], contract.to_dict(), quota)
        self.assertEqual(protected.action, "codex")
        forbidden = contract.to_dict()
        forbidden["claude_allowed"] = False
        forbidden.pop("strategy", None)
        denied = Coordinator._claude_decision(second["spec"], forbidden,
                                             replace(quota, five_hour_remaining=80))
        self.assertEqual(denied.action, "codex")
        self.assertIn("does not admit Claude", denied.reason)

    def test_cli_failure_without_worktree_also_rechecks_next_attempt(self) -> None:
        _, second = self._retry(reason="Claude structured result rejected: CLI output is not JSON",
                                kind="claude-executor-failed", recover_files=False)
        self.assertEqual(second["spec"]["executor"], "claude")

    def test_quota_fallback_remains_codex(self) -> None:
        _, second = self._retry(reason="protected quota reserve", kind="quota-or-auth-policy")
        self.assertEqual(second["spec"]["executor"], "codex")

    def test_business_failure_does_not_invalidate_fallback(self) -> None:
        _, second = self._retry(reason="requested acceptance failed", kind="claude-executor-failed")
        self.assertEqual(second["spec"]["executor"], "codex")

    def _execute_readmitted_attempt(self, remaining: int) -> list[str]:
        _, second = self._retry(
            reason="Claude structured result rejected: CLI JSON response must be an object",
            kind="claude-executor-failed",
        )
        self.store.write_quota(QuotaSnapshot(
            observed_at=now_iso(), auth_ok=True, auth_method="native-subscription",
            five_hour_remaining=remaining, weekly_all_remaining=80, weekly_sonnet_remaining=80,
            **service_fixtures.compatible_provenance(),
        ))
        calls = []

        class Executor:
            def __init__(self, provider: str):
                self.provider = provider

            def execute(self, request):
                calls.append(self.provider)
                assert request.worktree is not None
                assert (request.worktree / "src/value.txt").read_text() == "owned recovery content\n"
                assert (request.worktree / "src/new.txt").read_text() == "owned untracked content\n"
                return NodeResult("succeeded", "fixture provider completed", result_kind="worker",
                                  actual_model=request.spec["model"], checks=("fixture-check",))

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch,
                                  max_workers=1, config=self.config)
        try:
            with patch.object(coordinator, "_executor", side_effect=Executor):
                coordinator._execute_claimed(second)
        finally:
            coordinator._pool.shutdown(wait=True)
            coordinator._delivery_pool.shutdown(wait=True)
        return calls

    def test_runtime_executes_readmitted_claude_after_restoring_owned_files(self) -> None:
        self.assertEqual(self._execute_readmitted_attempt(80), ["claude"])

    def test_runtime_protected_quota_never_calls_readmitted_claude(self) -> None:
        self.assertEqual(self._execute_readmitted_attempt(25), ["codex"])
