from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.delivery import DeliveryError, GitHubDelivery, GitHubDeliveryRequest
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import StateConflictError, WorkbenchStore


class DeliveryTests(unittest.TestCase):
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
        self.epoch = self.store.activate_coordinator("delivery-test", "test-machine")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_accepted_task(self, *, external_write: bool) -> None:
        contract = TaskContract(
            task_id="delivery-task",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="deliver fixture",
            allowed_scope=("result.txt",),
            external_write_permission=external_write,
        )
        node = NodeSpec("verify", "delivery-task", "verify", "fixture", "fixture", "accepted", verifier=True)
        self.store.create_task(contract, [node], "create-delivery")
        self.store.queue_task("delivery-task")
        claimed = self.store.claim_ready_node("delivery-verifier", self.epoch)
        self.store.assign_worktree(
            "delivery-task",
            "verify",
            str(self.worktree),
            attempt=claimed["attempt"],
            coordinator_epoch=claimed["coordinator_epoch"],
            lease_epoch=claimed["lease_epoch"],
        )
        self.store.settle_claimed(claimed, NodeResult("succeeded", "accepted"))

    def test_external_delivery_requires_contract_authorization(self) -> None:
        self.create_accepted_task(external_write=False)
        with self.assertRaisesRegex(StateConflictError, "does not authorize"):
            self.store.begin_delivery(
                "delivery-task",
                "deliver-denied",
                {"task_id": "delivery-task", "command_id": "deliver-denied"},
            )

    def test_unsafe_remote_or_release_tag_is_rejected_before_delivery_creation(self) -> None:
        self.create_accepted_task(external_write=True)
        cases = (
            ("remote option", "--upload-pack=unsafe", None, False),
            ("remote shell syntax", "origin;touch", None, False),
            ("tag option", "origin", "--target=unsafe", True),
            ("tag revision extension", "origin", "v1.2.3^{commit}", True),
            ("tag shell syntax", "origin", "v1.2.3;touch", True),
            ("tag ref namespace", "origin", "refs/tags/v1.2.3", True),
        )
        for label, remote, release_tag, merge in cases:
            with self.subTest(label=label):
                calls: list[list[str]] = []

                def runner(command, **kwargs):
                    calls.append(command)
                    raise AssertionError(f"invalid input reached runner: {command}")

                store = Mock(wraps=self.store)
                delivery = GitHubDelivery(store, ArtifactStore(self.root / "artifacts"), runner=runner)
                request = GitHubDeliveryRequest(
                    task_id="delivery-task",
                    command_id=f"reject-{label}",
                    base_branch="main",
                    remote=remote,
                    merge=merge,
                    release_tag=release_tag,
                )
                with self.assertRaises(ValueError):
                    delivery.deliver(request)
                self.assertEqual(calls, [])
                store.begin_delivery.assert_not_called()

    def test_missing_remote_fails_before_delivery_creation_or_mutating_git(self) -> None:
        self.create_accepted_task(external_write=True)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 2, "", "No such remote")

        store = Mock(wraps=self.store)
        delivery = GitHubDelivery(store, ArtifactStore(self.root / "artifacts"), runner=runner)
        request = GitHubDeliveryRequest(
            task_id="delivery-task",
            command_id="missing-remote",
            base_branch="main",
            remote="upstream",
        )
        with self.assertRaisesRegex(DeliveryError, "does not exist"):
            delivery.deliver(request)

        self.assertEqual(
            calls,
            [["git", "-C", str(self.worktree.resolve()), "remote", "get-url", "--", "upstream"]],
        )
        store.begin_delivery.assert_not_called()

    def test_valid_delivery_uses_safe_argv(self) -> None:
        self.create_accepted_task(external_write=True)
        calls: list[tuple[list[str], dict]] = []
        branch = "codex-workbench/integration/delivery-task"
        commit = "b" * 40
        worktree = str(self.worktree.resolve())

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command == ["git", "-C", worktree, "remote", "get-url", "--", "origin"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/fixture.git\n", "")
            if command[:4] == ["git", "-C", worktree, "switch"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:4] == ["git", "-C", worktree, "status"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:4] == ["git", "-C", worktree, "rev-parse"]:
                return subprocess.CompletedProcess(command, 0, f"{commit}\n", "")
            if command[:4] == ["git", "-C", worktree, "diff"]:
                return subprocess.CompletedProcess(command, 1, "", "")
            if command[:4] == ["git", "-C", worktree, "push"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            if command[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pr/1\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "checks passed\n", "")
            raise AssertionError(command)

        delivery = GitHubDelivery(
            self.store,
            ArtifactStore(self.root / "artifacts"),
            runner=runner,
        )
        receipt = delivery.deliver(
            GitHubDeliveryRequest(
                task_id="delivery-task",
                command_id="safe-argv",
                base_branch="main",
                remote="origin",
            )
        )

        commands = [command for command, _ in calls]
        self.assertEqual(receipt["state"], "ci_passed", receipt)
        self.assertEqual(
            commands[0],
            ["git", "-C", worktree, "remote", "get-url", "--", "origin"],
        )
        self.assertIn(
            ["git", "-C", worktree, "push", "--set-upstream", "--", "origin", f"HEAD:{branch}"],
            commands,
        )
        self.assertIn(
            ["git", "-C", worktree, "diff", "--quiet", self.base_sha, commit, "--"],
            commands,
        )
        self.assertTrue(all(isinstance(command, list) and kwargs["shell"] is False for command, kwargs in calls))

    def test_offline_delivery_reaches_release_and_is_idempotent(self) -> None:
        self.create_accepted_task(external_write=True)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] != "gh":
                return subprocess.run(command, **kwargs)
            if command[:3] == ["gh", "pr", "view"] and command[3].startswith("codex-workbench/"):
                return subprocess.CompletedProcess(command, 1, "", "not found")
            if command[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(command, 0, "https://example.invalid/pr/1\n", "")
            if command[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(command, 0, "checks passed\n", "")
            if command[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(command, 0, "merged\n", "")
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
            if command[:3] == ["gh", "release", "create"]:
                return subprocess.CompletedProcess(command, 0, "released\n", "")
            if command[:3] == ["gh", "release", "view"]:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            raise AssertionError(command)

        delivery = GitHubDelivery(
            self.store,
            ArtifactStore(self.root / "artifacts"),
            runner=runner,
        )
        request = GitHubDeliveryRequest(
            task_id="delivery-task",
            command_id="deliver-1",
            base_branch="main",
            merge=True,
            release_tag="v-fixture",
        )
        receipt = delivery.deliver(request)
        self.assertEqual(receipt["state"], "released", receipt)
        self.assertEqual(delivery.deliver(request)["state"], "released")
        branch_sha = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/codex-workbench/integration/delivery-task"],
            text=True,
        ).strip()
        self.assertEqual(branch_sha, receipt["details"]["commit"])
        self.assertEqual(sum(1 for command in calls if command[:3] == ["gh", "pr", "create"]), 1)
        self.assertIn(
            ["git", "-C", str(self.worktree.resolve()), "remote", "get-url", "--", "origin"],
            calls,
        )
        self.assertIn(
            [
                "gh",
                "release",
                "create",
                "--target",
                "a" * 40,
                "--generate-notes",
                "--",
                "v-fixture",
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
