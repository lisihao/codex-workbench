from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.delivery import GitHubDelivery, GitHubDeliveryRequest
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


class DeliveryResumeTests(unittest.TestCase):
    """Fixture-only Git/GitHub recovery coverage; no real GitHub calls occur."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.remote = self.root / "remote.git"
        self.repository.mkdir()
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repository, check=True)
        (self.repository / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repository, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.repository, check=True, capture_output=True)
        self.base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repository, text=True).strip()
        self.worktree = self.root / "verifier"
        subprocess.run(
            ["git", "worktree", "add", "-b", "verify", str(self.worktree), self.base_sha],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        (self.worktree / "result.txt").write_text("accepted\n")
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("delivery-resume-test", "test-machine")
        self._create_accepted_task()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_accepted_task(self) -> None:
        contract = TaskContract(
            task_id="delivery-resume-task",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="resume delivery fixture",
            allowed_scope=("result.txt",),
            external_write_permission=True,
        )
        node = NodeSpec(
            "verify",
            "delivery-resume-task",
            "verify",
            "fixture",
            "fixture",
            "accepted",
            verifier=True,
        )
        self.store.create_task(contract, [node], "create-delivery-resume")
        self.store.queue_task("delivery-resume-task")
        claimed = self.store.claim_ready_node("delivery-resume-verifier", self.epoch)
        self.store.assign_worktree(
            "delivery-resume-task",
            "verify",
            str(self.worktree),
            attempt=claimed["attempt"],
            coordinator_epoch=claimed["coordinator_epoch"],
            lease_epoch=claimed["lease_epoch"],
        )
        self.store.settle_claimed(claimed, NodeResult("succeeded", "accepted"))

    def _request(self, command_id: str = "resume-delivery") -> GitHubDeliveryRequest:
        return GitHubDeliveryRequest(
            task_id="delivery-resume-task",
            command_id=command_id,
            base_branch="main",
        )

    def _delivery(self, runner) -> GitHubDelivery:
        return GitHubDelivery(self.store, ArtifactStore(self.root / "artifacts"), runner=runner)

    @staticmethod
    def _no_pr(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "no pull requests found for branch")

    def _remote_branch_sha(self) -> str:
        return subprocess.check_output(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "rev-parse",
                "refs/heads/codex-workbench/integration/delivery-resume-task",
            ],
            text=True,
        ).strip()

    def _seed_durable_merged_receipt(self, request: GitHubDeliveryRequest) -> dict:
        """Model a process loss after merge persistence and before release work."""

        branch = "codex-workbench/integration/delivery-resume-task"
        subprocess.run(
            ["git", "switch", "-C", branch],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=self.worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "fixture integration"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.worktree, text=True).strip()
        subprocess.run(
            ["git", "push", "--set-upstream", "--", "origin", f"HEAD:{branch}"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        )
        receipt = self.store.begin_delivery(request.task_id, request.command_id, request.to_dict())
        lease = self.store.acquire_delivery_lease(request.command_id)
        try:
            for state, details in (
                ("prepared", {"branch": branch, "commit": commit}),
                ("pushed", {"push_log": "fixture push"}),
                ("pr_open", {"pr_url": "https://example.invalid/pull/10"}),
                ("ci_passed", {"ci_log": "fixture checks"}),
                ("merged", {"merge_sha": "a" * 40}),
            ):
                receipt = self.store.update_delivery(
                    request.command_id,
                    state,
                    details,
                    expected_states=(receipt["state"],),
                    fence=lease["fence"],
                )
        finally:
            self.store.release_delivery_lease(request.command_id, lease["fence"])
        return receipt

    def test_push_then_pr_401_resumes_with_one_push(self) -> None:
        authenticated = False
        pr_exists = False
        pushes = 0
        creates = 0

        def runner(command, **kwargs):
            nonlocal authenticated, pr_exists, pushes, creates
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                if not authenticated:
                    return subprocess.CompletedProcess(command, 1, "", "HTTP 401: Bad credentials")
                if pr_exists:
                    return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/1\n", "")
                return self._no_pr(command)
            if command[:3] == ["gh", "pr", "create"]:
                creates += 1
                pr_exists = True
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/1\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request())
        self.assertEqual(first["state"], "failed", first)
        self.assertEqual(pushes, 1)
        self.assertEqual(creates, 0, "HTTP 401 must not be treated as an absent PR")
        self.assertEqual(self._remote_branch_sha(), first["details"]["commit"])

        authenticated = True
        resumed = delivery.deliver(self._request())
        self.assertEqual(resumed["state"], "ci_passed", resumed)
        self.assertEqual(pushes, 1, "resume must reconcile the remote ref, not push again")
        self.assertEqual(creates, 1)
        self.assertEqual(self._remote_branch_sha(), resumed["details"]["commit"])

    def test_existing_pr_is_reused_and_duplicate_call_is_idempotent(self) -> None:
        pushes = 0
        creates = 0

        def runner(command, **kwargs):
            nonlocal pushes, creates
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/7\n", "")
            if command[:3] == ["gh", "pr", "create"]:
                creates += 1
                raise AssertionError("an existing PR must not be created again")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request())
        second = delivery.deliver(self._request())
        self.assertEqual(first["state"], "ci_passed", first)
        self.assertEqual(second["state"], "ci_passed", second)
        self.assertEqual(pushes, 1)
        self.assertEqual(creates, 0)
        self.assertEqual(second["details"]["pr_url"], "https://example.invalid/pull/7")

    def test_prefence_failed_after_push_receipt_reconciles_without_second_push(self) -> None:
        authenticated = False
        pr_exists = False
        pushes = 0

        def runner(command, **kwargs):
            nonlocal authenticated, pr_exists, pushes
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                if not authenticated:
                    return subprocess.CompletedProcess(command, 1, "", "HTTP 401: Bad credentials")
                if pr_exists:
                    return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/6\n", "")
                return self._no_pr(command)
            if command[:3] == ["gh", "pr", "create"]:
                pr_exists = True
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/6\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(command)

        request = self._request("legacy-after-push")
        delivery = self._delivery(runner)
        first = delivery.deliver(request)
        self.assertEqual(first["state"], "failed", first)
        self.assertIn("push_log", first["details"])
        with self.store.transaction() as connection:
            legacy_details = dict(first["details"])
            legacy_details.pop("recovery", None)
            connection.execute(
                "UPDATE delivery_receipts SET details_json = ? WHERE command_id = ?",
                (canonical_json(legacy_details), request.command_id),
            )

        authenticated = True
        resumed = delivery.deliver(request)
        self.assertEqual(resumed["state"], "ci_passed", resumed)
        self.assertEqual(pushes, 1)

    def test_durable_merged_receipt_resumes_release_once_after_unknown_effect(self) -> None:
        request = GitHubDeliveryRequest(
            task_id="delivery-resume-task",
            command_id="merged-release-resume",
            base_branch="main",
            merge=True,
            release_tag="v-resume-fixture",
        )
        seeded = self._seed_durable_merged_receipt(request)
        self.assertEqual(seeded["state"], "merged")
        release_exists = False
        creates = 0

        def runner(command, **kwargs):
            nonlocal release_exists, creates
            if command[0] == "git":
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "release", "view"]:
                if release_exists:
                    return subprocess.CompletedProcess(command, 0, "v-resume-fixture\n", "")
                return subprocess.CompletedProcess(command, 1, "", "release not found")
            if command[:3] == ["gh", "release", "create"]:
                creates += 1
                release_exists = True
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            raise AssertionError(command)

        delivery = self._delivery(runner)
        interrupted = delivery.deliver(request)
        self.assertEqual(interrupted["state"], "indeterminate", interrupted)
        self.assertEqual(creates, 1, "a durable merged receipt must continue to release work")

        resumed = delivery.deliver(request)
        duplicate = delivery.deliver(request)
        self.assertEqual(resumed["state"], "released", resumed)
        self.assertEqual(duplicate["state"], "released", duplicate)
        self.assertEqual(creates, 1, "unknown release creation must be reconciled, not repeated")

    def test_divergent_remote_commit_is_terminal_without_push_or_pr_replay(self) -> None:
        authenticated = False
        pushes = 0
        pr_views = 0
        creates = 0

        def runner(command, **kwargs):
            nonlocal authenticated, pushes, pr_views, creates
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                pr_views += 1
                if not authenticated:
                    return subprocess.CompletedProcess(command, 1, "", "HTTP 401: Bad credentials")
                raise AssertionError("commit mismatch must stop before another PR lookup")
            if command[:3] == ["gh", "pr", "create"]:
                creates += 1
                raise AssertionError("commit mismatch must not create a PR")
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request("divergent-remote"))
        self.assertEqual(first["state"], "failed", first)
        self.assertEqual(pushes, 1)
        self.assertEqual(pr_views, 1)
        self.assertNotEqual(first["details"]["commit"], self.base_sha)

        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "update-ref",
                "refs/heads/codex-workbench/integration/delivery-resume-task",
                self.base_sha,
            ],
            check=True,
            capture_output=True,
        )
        authenticated = True
        resumed = delivery.deliver(self._request("divergent-remote"))
        self.assertEqual(resumed["state"], "failed", resumed)
        self.assertIn("does not match the durable commit", resumed["details"]["error"])
        self.assertEqual(pushes, 1)
        self.assertEqual(pr_views, 1)
        self.assertEqual(creates, 0)

    def test_changed_command_and_stale_or_concurrent_fences_are_rejected(self) -> None:
        request = self._request("fenced-delivery")
        self.store.begin_delivery(request.task_id, request.command_id, request.to_dict())
        first = self.store.acquire_delivery_lease(request.command_id)
        with self.assertRaisesRegex(StateConflictError, "already being resumed"):
            self.store.acquire_delivery_lease(request.command_id)
        self.assertTrue(self.store.release_delivery_lease(request.command_id, first["fence"]))

        replacement = self.store.acquire_delivery_lease(request.command_id)
        with self.assertRaisesRegex(StateConflictError, "lease is stale"):
            self.store.update_delivery(
                request.command_id,
                "prepared",
                {},
                expected_states=("accepted",),
                fence=first["fence"],
            )
        self.assertTrue(self.store.release_delivery_lease(request.command_id, replacement["fence"]))

        changed = GitHubDeliveryRequest(
            task_id=request.task_id,
            command_id=request.command_id,
            base_branch="other-base",
        )
        with self.assertRaises(CommandConflictError):
            self._delivery(lambda command, **kwargs: (_ for _ in ()).throw(AssertionError(command))).deliver(changed)

    def test_failed_before_push_remains_terminal_and_is_not_replayed(self) -> None:
        pushes = 0

        def runner(command, **kwargs):
            nonlocal pushes
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "switch"]:
                    return subprocess.CompletedProcess(command, 1, "", "cannot switch fixture")
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                return subprocess.run(command, **kwargs)
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request("prepare-failure"))
        second = delivery.deliver(self._request("prepare-failure"))
        self.assertEqual(first["state"], "failed", first)
        self.assertEqual(second["state"], "failed", second)
        self.assertIsNone(first["details"].get("recovery"))
        self.assertEqual(pushes, 0)

    def test_timeout_after_push_reconciles_without_blind_replay(self) -> None:
        timeout_after_push = True
        pr_exists = False
        pushes = 0
        creates = 0

        def runner(command, **kwargs):
            nonlocal timeout_after_push, pr_exists, pushes, creates
            if command[0] == "git":
                if command[:4] == ["git", "-C", str(self.worktree.resolve()), "push"]:
                    pushes += 1
                    completed = subprocess.run(command, **kwargs)
                    if timeout_after_push:
                        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                    return completed
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                if pr_exists:
                    return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/8\n", "")
                return self._no_pr(command)
            if command[:3] == ["gh", "pr", "create"]:
                creates += 1
                pr_exists = True
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/8\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request("push-timeout"))
        self.assertEqual(first["state"], "indeterminate", first)
        self.assertEqual(pushes, 1)
        self.assertEqual(self._remote_branch_sha(), first["details"]["commit"])

        timeout_after_push = False
        resumed = delivery.deliver(self._request("push-timeout"))
        self.assertEqual(resumed["state"], "ci_passed", resumed)
        self.assertEqual(pushes, 1, "unknown push effects are reconciled, never replayed")
        self.assertEqual(creates, 1)

    def test_unknown_pr_create_reuses_reconciled_pr_without_duplicate_create(self) -> None:
        pr_exists = False
        creates = 0

        def runner(command, **kwargs):
            nonlocal pr_exists, creates
            if command[0] == "git":
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"]:
                if pr_exists:
                    return subprocess.CompletedProcess(command, 0, "https://example.invalid/pull/9\n", "")
                return self._no_pr(command)
            if command[:3] == ["gh", "pr", "create"]:
                creates += 1
                pr_exists = True
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
            raise AssertionError(command)

        delivery = self._delivery(runner)
        first = delivery.deliver(self._request("pr-create-timeout"))
        self.assertEqual(first["state"], "indeterminate", first)
        self.assertEqual(creates, 1)

        resumed = delivery.deliver(self._request("pr-create-timeout"))
        self.assertEqual(resumed["state"], "ci_passed", resumed)
        self.assertEqual(creates, 1, "a create with unknown effects must be reconciled, not repeated")


if __name__ == "__main__":
    unittest.main()
