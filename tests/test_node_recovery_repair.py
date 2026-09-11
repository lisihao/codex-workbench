"""Fixture-only coverage for the bounded journaled tooling-repair action."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.authority_service import AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeSpec, TaskContract
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

    def _configure_policy(self, *, enabled: bool = True, scopes: tuple[str, ...] = ("src/codex_workbench",)) -> None:
        self.recovery.configure_policy(
            self.task_id,
            RecoveryPolicy(
                enabled=enabled,
                allowed_actions=("request_repair",),
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
