"""Focused fixture coverage for the bounded Authority recovery action adapter."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.authority_service import AUTHORITY_REQUEST_JOURNAL_DDL, AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.node_recovery_actions import JournaledNodeActions
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NODE_RECOVERY_SCHEMA_SQL, NodeRecoveryStore
from codex_workbench.store import StateConflictError, WorkbenchStore


_VALIDATION_FINGERPRINT = "a" * 64
_SOURCE_DELTA = "b" * 64


class JournaledNodeActionsTests(unittest.TestCase):
    """The adapter has no journal of its own; all mutation receipts are Authority rows."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="node-recovery-actions-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = WorkbenchConfig(self.root / "state")
        self.config.install_manifest.parent.mkdir(parents=True)
        self.config.install_manifest.write_text('{"fixture": true}\n', encoding="utf-8")
        self.base = WorkbenchStore(self.root / "state.sqlite")
        self.base.initialize()
        with self.base.connection() as connection:
            connection.executescript(AUTHORITY_REQUEST_JOURNAL_DDL)
            connection.executescript(NODE_RECOVERY_SCHEMA_SQL)
        self.task_id = "blocked-recovery"
        self._create_blocked_task()
        self.recovery = NodeRecoveryStore(self.base)
        self._configure_policy()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.authority = AuthorityService(self.base, self._invoke, "fixture-authority")
        self.actions = JournaledNodeActions(self.config, self.recovery, self.authority)

    def _create_blocked_task(self) -> None:
        contract = TaskContract(
            task_id=self.task_id,
            repository=str(self.root),
            base_sha="fixture-base",
            objective="fixture blocked recovery",
            allowed_scope=("src",),
        )
        self.base.create_task(
            contract,
            [
                NodeSpec("worker", self.task_id, "fixture", "fixture", "fixture", "blocked"),
                NodeSpec(
                    "verify", self.task_id, "verify", "fixture", "fixture", "accepted",
                    depends_on=("worker",), verifier=True,
                ),
            ],
            "create-blocked-recovery",
        )
        with self.base.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET state = 'blocked', state_revision = 7 WHERE task_id = ?",
                (self.task_id,),
            )
            connection.execute(
                """
                UPDATE nodes
                SET state = 'blocked', attempt = 1, worktree = '/fixture/worktree'
                WHERE task_id = ? AND node_id = 'worker'
                """,
                (self.task_id,),
            )

    def _configure_policy(self, *, enabled: bool = True) -> None:
        self.recovery.configure_policy(
            self.task_id,
            RecoveryPolicy(
                enabled=enabled,
                allowed_actions=("narrow_validation", "source_only_recovery"),
                validation_profiles=(
                    "dsh-b-ipc-v1",
                    "dsh-b-pairing-check-v1",
                    "dsh-b-pairing-write-v1",
                ),
            ),
            expected_task_revision=self._task()["state_revision"],
            actor="fixture-policy",
        )

    def _task(self) -> dict:
        return self.base.get_task(self.task_id)

    def _observation(self, **overrides: object) -> dict[str, object]:
        observation: dict[str, object] = {
            "task_id": self.task_id,
            "node_id": "worker",
            "attempt": 1,
            "task_revision": self._task()["state_revision"],
            "validation_profile": "dsh-b-ipc-v1",
        }
        observation.update(overrides)
        return observation

    @staticmethod
    def _mcp(result: dict[str, object]) -> dict[str, object]:
        return {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}]}

    def _invoke(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((tool, dict(arguments)))
        if tool == "workbench_validate_blocked_node":
            if arguments["dry_run"] is True:
                return self._mcp({
                    "ok": True,
                    "worktree": arguments["worktree"],
                    "fingerprint": _VALIDATION_FINGERPRINT,
                    "source_delta_sha256": _SOURCE_DELTA,
                    "changed_paths": ["src/private-should-not-persist.py"] * 256,
                })
            return self._mcp({
                "ok": True,
                "validation_id": arguments["validation_id"],
                "fingerprint": arguments["expected_fingerprint"],
                "source_delta_after": _SOURCE_DELTA,
                "audit_ref": "sha256:fixture-validation-audit:validation.json",
                "plan": {"raw_output": "must not enter action receipt"},
            })
        if tool == "workbench_control_task":
            if arguments["dry_run"] is True:
                return self._mcp({
                    "ok": True,
                    "source_delta_sha256": _SOURCE_DELTA,
                    "changed_paths": ["src/private-should-not-persist.py"] * 256,
                    "ignored_paths": ["generated/private"] * 256,
                })
            return self._mcp({
                "ok": True,
                "source_delta_sha256": arguments["expected_source_delta_sha256"],
                "changed_paths": ["src/private-should-not-persist.py"],
                "recovery": {"raw_output": "must not enter action receipt"},
            })
        raise AssertionError(f"unexpected tool {tool}")

    def _authority_rows(self) -> int:
        with self.base.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM authority_requests").fetchone()[0])

    def test_narrow_validation_binds_bounded_preview_and_reconciles_lost_response_once(self) -> None:
        install_sha = self.actions._install_manifest_sha256()
        plan = self.actions.prepare(
            self._observation(), "narrow_validation", "validation-request-1"
        )

        self.assertEqual(plan["fresh_preview"], {
            "worktree": "/fixture/worktree",
            "fingerprint": _VALIDATION_FINGERPRINT,
            "source_delta_sha256": _SOURCE_DELTA,
            "install_manifest_sha256": install_sha,
            "runtime_fingerprint": None,
        })
        self.assertEqual(self._authority_rows(), 0)
        self.assertEqual(self.calls[0][1]["dry_run"], True)
        self.assertNotIn("changed_paths", plan["fresh_preview"])
        self.assertNotIn("raw_output", json.dumps(plan, sort_keys=True))

        first = self.actions.execute(plan)
        recovered = self.actions.reconcile(plan)
        repeated = self.actions.execute(plan)

        self.assertEqual(first, recovered)
        self.assertEqual(first, repeated)
        self.assertEqual(first["journal_status"], "completed")
        self.assertTrue(first["known_effects"])
        assert first["receipt"] is not None
        self.assertTrue(first["receipt"]["stage_succeeded"])
        self.assertEqual(first["receipt"]["observation_patch"], {
            "last_validation_profile": "dsh-b-ipc-v1",
            "validated_source_delta": _SOURCE_DELTA,
            "validation_audit_ref": "sha256:fixture-validation-audit:validation.json",
            "validated_install_manifest": install_sha,
            "validated_runtime_fingerprint": None,
        })
        self.assertEqual(len(self.calls), 2)
        mutation = self.calls[1][1]
        self.assertEqual(mutation["validation_id"], "validation-request-1")
        self.assertEqual(mutation["expected_fingerprint"], _VALIDATION_FINGERPRINT)
        self.assertFalse(mutation["dry_run"])
        self.assertEqual(self._authority_rows(), 1)

    def test_pairing_write_requires_explicit_profile_and_sets_its_only_confirmation(self) -> None:
        observation = self._observation(validation_profile="dsh-b-pairing-write-v1")

        with self.assertRaisesRegex(StateConflictError, "explicit validation_profile"):
            self.actions.prepare(observation, "narrow_validation", "pairing-request-1")

        plan = self.actions.prepare(
            observation,
            "narrow_validation",
            "pairing-request-2",
            validation_profile="dsh-b-pairing-write-v1",
        )
        self.assertEqual(plan["stage_key"], "narrow_validation:dsh-b-pairing-write-v1")
        self.assertTrue(plan["arguments"]["confirm_pairing_write"])
        self.assertEqual(set(plan["arguments"]) - {"confirm_pairing_write"}, {
            "task_id", "node_id", "expected_revision", "expected_attempt", "worktree",
            "check_id", "reason", "dry_run", "validation_id", "expected_fingerprint",
        })

    def test_source_only_uses_exact_resume_arguments_and_validated_fresh_preview(self) -> None:
        install_sha = self.actions._install_manifest_sha256()
        plan = self.actions.prepare(
            self._observation(
                validated_source_delta=_SOURCE_DELTA,
                validated_install_manifest=install_sha,
            ),
            "source_only_recovery",
            "source-only-request-1",
        )

        self.assertEqual(plan["fresh_preview"], {
            "source_delta_sha256": _SOURCE_DELTA,
            "install_manifest_sha256": install_sha,
        })
        self.assertNotIn("confirm_no_side_effects", plan["arguments"])
        self.assertEqual(plan["arguments"]["action"], "resume")
        self.assertTrue(plan["arguments"]["source_only"])
        self.assertTrue(plan["arguments"]["preserve_untracked"])
        self.assertTrue(plan["arguments"]["confirm_source_only_extraction"])
        self.assertTrue(plan["arguments"]["confirm_preserve_unknown_ignored"])
        self.assertEqual(plan["arguments"]["expected_source_delta_sha256"], _SOURCE_DELTA)

        result = self.actions.execute(plan)

        self.assertEqual(result["journal_status"], "completed")
        self.assertTrue(result["known_effects"])
        assert result["receipt"] is not None
        self.assertEqual(result["receipt"]["historical_effects"], "unknown")
        self.assertEqual(result["receipt"]["observation_patch"], {"recovery_resumed": True})
        mutation = self.calls[1][1]
        self.assertNotIn("confirm_no_side_effects", mutation)
        self.assertEqual(mutation["expected_source_delta_sha256"], _SOURCE_DELTA)

    def test_source_only_rejects_validation_or_installation_drift_before_authorizing(self) -> None:
        with self.assertRaisesRegex(StateConflictError, "source delta differs"):
            self.actions.prepare(
                self._observation(validated_source_delta="c" * 64),
                "source_only_recovery",
                "source-delta-drift",
            )

        prior_manifest = self.actions._install_manifest_sha256()
        self.config.install_manifest.write_text('{"fixture": false}\n', encoding="utf-8")
        with self.assertRaisesRegex(StateConflictError, "installation changed"):
            self.actions.prepare(
                self._observation(validated_install_manifest=prior_manifest),
                "source_only_recovery",
                "install-drift",
            )
        self.assertEqual(self._authority_rows(), 0)

    def test_execute_rejects_revoked_policy_or_changed_task_and_missing_reconcile_never_replays(self) -> None:
        policy_plan = self.actions.prepare(
            self._observation(), "narrow_validation", "revoked-policy-request"
        )
        self._configure_policy(enabled=False)
        with self.assertRaisesRegex(StateConflictError, "policy is disabled"):
            self.actions.execute(policy_plan)
        self.assertIsNone(self.actions.reconcile(policy_plan))
        self.assertEqual(self._authority_rows(), 0)

        self._configure_policy(enabled=True)
        task_plan = self.actions.prepare(
            self._observation(), "narrow_validation", "changed-task-request"
        )
        self.base.transition_task(
            self.task_id,
            "paused",
            expected_revision=self._task()["state_revision"],
        )
        with self.assertRaisesRegex(StateConflictError, "paused or cancelled"):
            self.actions.execute(task_plan)
        self.assertEqual(self._authority_rows(), 0)

    def test_unknown_journal_is_preserved_without_reinvocation(self) -> None:
        plan = self.actions.prepare(
            self._observation(), "narrow_validation", "unknown-request"
        )
        original = self.authority.invoke_callable

        def interrupted(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            if arguments["dry_run"] is True:
                return original(tool, arguments)
            raise RuntimeError("simulated response loss after an uncertain effect")

        self.authority.invoke_callable = interrupted
        first = self.actions.execute(plan)
        second = self.actions.execute(plan)

        self.assertEqual(first["journal_status"], "unknown")
        self.assertFalse(first["known_effects"])
        self.assertIsNone(first["receipt"])
        self.assertEqual(first, second)
        self.assertEqual(self._authority_rows(), 1)
