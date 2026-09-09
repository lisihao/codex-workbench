from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime, timedelta
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.authority_delivery import (
    BoundedVerificationIdentityObserver,
    TaskDeliveryStageAdapter,
    VerificationObservation,
    build_authority_delivery_lifecycle,
)
from codex_workbench.config import WorkbenchConfig
from codex_workbench.delivery import GitHubDelivery
from codex_workbench.delivery_lifecycle import DeliveryStageContext
from codex_workbench.model import NodeSpec, TaskContract, now_iso
from codex_workbench.service import Coordinator

from tests import test_delivery as delivery_fixtures


class _FixtureVerificationObserver:
    """Return only explicitly requested runtime identities without a subprocess."""

    def observe(
        self,
        _task: dict[str, object],
        _verifier: dict[str, object],
        required_runtimes: tuple[str, ...],
    ) -> VerificationObservation:
        return VerificationObservation(
            identities={
                name: {"fixture": name, "version": "fixture-v1"}
                for name in required_runtimes
            },
            receipt={"fixture": "runtime-observation", "required_runtimes": list(required_runtimes)},
        )


class _QueueStore:
    """Fixture facade proving the adapter uses the existing atomic queue API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def queue_task_with_instruction(
        self,
        task_id: str,
        instruction: str,
        *,
        expected_revision: int,
    ) -> dict[str, object]:
        self.calls.append((task_id, instruction, expected_revision))
        return {"task_id": task_id, "state": "queued", "revision": expected_revision + 1}


class AuthorityDeliveryTests(unittest.TestCase):
    setUp = delivery_fixtures.DeliveryTests.setUp
    tearDown = delivery_fixtures.DeliveryTests.tearDown
    create_accepted_task = delivery_fixtures.DeliveryTests.create_accepted_task

    def _config(self) -> WorkbenchConfig:
        config = WorkbenchConfig(
            self.root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id="fixture-machine",
        )
        config.initialize()
        return config

    def _objective_request(self) -> dict[str, object]:
        return {
            "requested_endpoints": {
                "github": {"remote": "origin", "base_branch": "main", "merge": False},
            },
            "scope": {"repository": str(self.repository), "paths": ["result.txt"]},
            "authority": {"actor": "fixture-user", "reason": "explicit GitHub fixture delivery"},
            "budget": {
                "attempt_limit": 3,
                "time_budget_seconds": 600,
                "cost_budget": 10,
                "base_backoff_seconds": 1,
                "max_backoff_seconds": 2,
            },
        }

    def _create_objective(self, *, authorize: bool) -> dict[str, object]:
        objective = self.store.create_delivery_objective(
            "delivery-task",
            "authority-delivery-objective",
            self._objective_request(),
        )
        if authorize:
            accepted = self.store.get_task("delivery-task")
            self.store.grant_delivery_authorization(
                "delivery-task",
                "authority-delivery-grant",
                scope={"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
                authority={"actor": "fixture-user", "reason": "exact GitHub fixture scope"},
                objective_id=str(objective["objective_id"]),
                expected_task_revision=int(accepted["state_revision"]),
            )
        return objective

    def _github_delivery(
        self,
        calls: list[list[str]],
        *,
        ci_started: threading.Event | None = None,
        ci_release: threading.Event | None = None,
        ci_results: list[tuple[int, str, str]] | None = None,
    ) -> GitHubDelivery:
        worktree = str(self.worktree.resolve())
        integration_commit = "b" * 40
        pushed = False

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal pushed
            calls.append(command)
            if command[:2] == ["git", "-C"] and command[3:] == ["remote", "get-url", "--", "origin"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/fixture.git\n", "")
            if command[:2] == ["git", "-C"] and command[3] == "switch":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["git", "-C"] and command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["git", "-C"] and command[3] == "rev-parse":
                return subprocess.CompletedProcess(command, 0, f"{integration_commit}\n", "")
            if command[:2] == ["git", "-C"] and command[3] == "diff":
                return subprocess.CompletedProcess(command, 1, "", "")
            if command[:2] == ["git", "-C"] and command[3] == "push":
                pushed = True
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["git", "-C"] and command[3] == "ls-remote":
                return subprocess.CompletedProcess(command, 0 if pushed else 2,
                                                   f"{integration_commit}\t{command[-1]}\n" if pushed else "", "")
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            if command[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pr/1\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                self.assertNotIn("--watch", command)
                self.assertLessEqual(_kwargs["timeout"], 30)
                if ci_results:
                    code, stdout, stderr = ci_results.pop(0)
                    return subprocess.CompletedProcess(command, code, stdout, stderr)
                if ci_started is not None:
                    ci_started.set()
                if ci_release is not None and not ci_release.wait(timeout=5):
                    raise AssertionError("fixture CI release was not signaled")
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(f"unexpected GitHub fixture command: {command}")

        return GitHubDelivery(
            self.store,
            ArtifactStore(self.root / "artifacts"),
            runner=runner,
        )

    def _lifecycle(
        self,
        config: WorkbenchConfig,
        delivery: GitHubDelivery,
        *,
        epoch: int,
    ):
        return build_authority_delivery_lifecycle(
            self.store,
            config,
            coordinator_epoch=epoch,
            github_delivery=delivery,
            verification_observer=_FixtureVerificationObserver(),
        )

    def _start_coordinator(self, config: WorkbenchConfig, lifecycle, *, epoch: int):
        coordinator = Coordinator(
            self.store,
            self.root,
            coordinator_epoch=epoch,
            max_workers=1,
            poll_seconds=0.01,
            delivery_lifecycle=lifecycle,
            config=config,
        )
        coordinator.recover()
        thread = threading.Thread(target=coordinator.run_forever, daemon=True)
        thread.start()
        return coordinator, thread

    def _stop_coordinator(self, coordinator: Coordinator, thread: threading.Thread) -> None:
        coordinator.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "fixture Coordinator did not stop")

    def _wait_until(self, predicate, *, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("fixture condition did not become true")

    def _queue_unrelated_fixture_task(self) -> str:
        task_id = "unrelated-fixture-work"
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="prove ordinary fixture work remains dispatchable",
            allowed_scope=("result.txt",),
            retry_limit=0,
        )
        nodes = [
            NodeSpec(
                "work",
                task_id,
                "ordinary fixture work",
                "fixture",
                "fixture",
                "ordinary fixture worker completed",
                write_scopes=("result.txt",),
                ordinal=1,
            ),
            NodeSpec(
                "verify",
                task_id,
                "ordinary fixture verification",
                "fixture",
                "fixture",
                "ordinary fixture verifier accepted",
                depends_on=("work",),
                verifier=True,
                ordinal=2,
            ),
        ]
        self.store.create_task(contract, nodes, "create-unrelated-fixture-work")
        self.store.queue_task(task_id)
        return task_id

    def test_default_authority_never_calls_github_without_scope_grant(self) -> None:
        self.create_accepted_task(external_write=False)
        objective = self._create_objective(authorize=False)
        config = self._config()
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            raise AssertionError(f"unauthorized objective reached GitHub runner: {command}")

        lifecycle = self._lifecycle(
            config,
            GitHubDelivery(self.store, ArtifactStore(self.root / "artifacts"), runner=runner),
            epoch=self.epoch,
        )
        coordinator, thread = self._start_coordinator(config, lifecycle, epoch=self.epoch)
        try:
            self._wait_until(
                lambda: self.store.get_delivery_objective(str(objective["objective_id"]))["state"]
                == "needs_decision"
            )
        finally:
            self._stop_coordinator(coordinator, thread)
        self.assertEqual(calls, [])

    def test_authorized_objective_advances_in_loop_without_blocking_workers(self) -> None:
        self.create_accepted_task(external_write=False)
        objective = self._create_objective(authorize=True)
        config = self._config()
        calls: list[list[str]] = []
        ci_started = threading.Event()
        ci_release = threading.Event()
        lifecycle = self._lifecycle(
            config,
            self._github_delivery(calls, ci_started=ci_started, ci_release=ci_release),
            epoch=self.epoch,
        )
        coordinator, thread = self._start_coordinator(config, lifecycle, epoch=self.epoch)
        try:
            self.assertTrue(ci_started.wait(timeout=5), "fixture CI stage did not start")
            unrelated = self._queue_unrelated_fixture_task()
            self._wait_until(lambda: self.store.get_task(unrelated)["state"] == "accepted")
            ci_release.set()
            self._wait_until(
                lambda: self.store.get_delivery_objective(str(objective["objective_id"]))["state"]
                == "complete"
            )
        finally:
            ci_release.set()
            self._stop_coordinator(coordinator, thread)

        completed = self.store.get_delivery_objective(str(objective["objective_id"]))
        self.assertEqual(completed["identities"]["source"], "b" * 40)
        self.assertEqual(completed["identities"]["build"]["integration_commit"], "b" * 40)
        self.assertEqual(sum(command[:2] == ["git", "-C"] and command[3] == "push" for command in calls), 1)

    def test_restarted_coordinator_resumes_persisted_github_receipt_without_repush(self) -> None:
        self.create_accepted_task(external_write=False)
        objective = self._create_objective(authorize=True)
        config = self._config()
        calls: list[list[str]] = []
        first = self._lifecycle(config, self._github_delivery(calls), epoch=self.epoch)
        for _ in range(4):
            first.reconcile_once()
        staged = self.store.get_delivery_objective(str(objective["objective_id"]))
        self.assertEqual(staged["stage"], "ci")
        self.assertEqual(sum(command[:2] == ["git", "-C"] and command[3] == "push" for command in calls), 1)
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE delivery_objectives SET lease_expires_at = ? WHERE objective_id = ?",
                (now_iso(), objective["objective_id"]),
            )
        restarted_epoch = self.store.activate_coordinator("authority-restarted", "test-machine")
        restarted = self._lifecycle(
            config,
            self._github_delivery(calls),
            epoch=restarted_epoch,
        )
        coordinator, thread = self._start_coordinator(config, restarted, epoch=restarted_epoch)
        try:
            self._wait_until(
                lambda: self.store.get_delivery_objective(str(objective["objective_id"]))["state"]
                == "complete"
            )
        finally:
            self._stop_coordinator(coordinator, thread)

        self.assertEqual(sum(command[:2] == ["git", "-C"] and command[3] == "push" for command in calls), 1)
        self.assertTrue(any(command[:3] == ["gh", "pr", "checks"] for command in calls))

    def test_ci_not_reported_then_pending_resumes_without_repush_or_retry_exhaustion(self) -> None:
        self.create_accepted_task(external_write=False)
        objective = self._create_objective(authorize=True)
        calls = []
        results = [(1, "", "no checks reported on this branch"),
                   (8, '[{"bucket":"pending","name":"fixture","state":"IN_PROGRESS"}]', "")]
        lifecycle = self._lifecycle(self._config(), self._github_delivery(calls, ci_results=results), epoch=self.epoch)
        timestamp = datetime.now(UTC)
        for index in range(8):
            with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=index * 10)).isoformat()):
                lifecycle.reconcile_once()
        current = self.store.get_delivery_objective(objective["objective_id"])
        self.assertEqual(current["state"], "complete", current.get("wait_reason"))
        ci_receipts = [receipt for receipt in current["stage_receipts"] if receipt["stage"] == "ci"]
        self.assertEqual(len(ci_receipts), 1)
        self.assertEqual(ci_receipts[0]["attempt"], 1)
        self.assertEqual(sum(command[:2] == ["git", "-C"] and command[3] == "push" for command in calls), 1)
        self.assertEqual(sum(command[:3] == ["gh", "pr", "create"] for command in calls), 1)
        self.assertEqual(sum(command[:3] == ["gh", "pr", "checks"] for command in calls), 3)

    def test_retryable_implementation_failure_uses_atomic_queue_without_blocked_assertion(self) -> None:
        store = _QueueStore()
        adapter = TaskDeliveryStageAdapter(store, _FixtureVerificationObserver())
        task = {
            "task_id": "repairable-task",
            "state": "needs_fix",
            "state_revision": 7,
            "contract": {
                "retry_limit": 3,
                "external_write_permission": False,
                "destructive_action_permission": False,
            },
            "nodes": [
                {
                    "node_id": "work",
                    "state": "failed",
                    "attempt": 1,
                    "verifier": False,
                    "result": {"status": "failed", "retryable": True, "summary": "fixture retryable failure"},
                },
            ],
        }
        context = DeliveryStageContext(
            objective={"objective_id": "repair-objective", "budget": {"attempt_limit": 3}},
            task=task,
            stage="implement",
            attempt=1,
            dispatch_id="repair-dispatch",
        )
        outcome = adapter.execute_stage(context)
        self.assertEqual(outcome.status, "deferred")
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0][0], "repairable-task")
        self.assertEqual(store.calls[0][2], 7)
        self.assertNotIn("confirm_no_side_effects", store.calls[0][1])

    def test_unrequested_runtime_observation_never_runs_a_launcher(self) -> None:
        config = self._config()
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            raise AssertionError(f"unrequested runtime probe: {command}")

        observation = BoundedVerificationIdentityObserver(config, runner=runner).observe({}, {}, ())
        self.assertEqual(calls, [])
        self.assertEqual(observation.identities, {})
        self.assertEqual(observation.receipt["toolchain"]["node"]["status"], "not-requested")
        self.assertEqual(observation.receipt["toolchain"]["pnpm"]["status"], "not-requested")


if __name__ == "__main__":
    unittest.main()
