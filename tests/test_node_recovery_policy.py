from __future__ import annotations

from datetime import datetime
import unittest

from codex_workbench.node_recovery_policy import (
    RecoveryPolicy,
    classify_failure,
    plan_recovery,
)


NOW = "2026-09-11T12:00:00+00:00"


def observation(category: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "task_state": "blocked",
        "node_state": "blocked",
        "category": category,
        "phase": "validation",
        "implementation_ready": True,
        "action_attempts": 0,
        "elapsed_seconds": 0,
        "now": NOW,
        "repair_linked": False,
        "repair_deployed_verified": False,
    }
    value.update(overrides)
    return value


class RecoveryPolicyTests(unittest.TestCase):
    def test_authorized_blocked_worker_can_enter_source_repair_before_acceptance_passes(self) -> None:
        policy = RecoveryPolicy(enabled=True, allowed_actions=("repair_source",))
        failed_check = observation(
            "validation_failure", readiness_ready=True, node_is_verifier=False,
            implementation_ready=False, validation_succeeded=False,
        )
        decision = plan_recovery(policy, failed_check)
        self.assertEqual((decision["state"], decision["action"]), ("ready", "repair_source"))
        self.assertFalse(decision["requires_authorization"])

        for change in (
            {"readiness_ready": False},
            {"node_is_verifier": True},
            {"validation_succeeded": True},
            {"node_state": "indeterminate"},
            {"task_state": "paused"},
            {"task_state": "cancelled"},
            {"category": "unknown_effects"},
            {"approval_denied": True},
            {"action_attempts": policy.max_action_attempts},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(plan_recovery(policy, {**failed_check, **change})["action"], "repair_source")
        disabled = RecoveryPolicy(enabled=True, allowed_actions=("observe_readiness",))
        self.assertNotEqual(plan_recovery(disabled, failed_check)["action"], "repair_source")

    def test_policy_defaults_round_trip_and_strict_config(self) -> None:
        policy = RecoveryPolicy()
        self.assertEqual(policy.to_dict()["allowed_actions"], ["observe_readiness"])
        self.assertEqual(RecoveryPolicy.from_dict(policy.to_dict()), policy)
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            RecoveryPolicy.from_dict({"unexpected": True})
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            RecoveryPolicy(max_action_attempts=4)
        with self.assertRaisesRegex(ValueError, "repair_repository"):
            RecoveryPolicy(
                enabled=True,
                allowed_actions=("request_repair",),
            )

    def test_policy_rejects_unknown_actions_profiles_and_bad_backoff(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported allowed"):
            RecoveryPolicy(allowed_actions=("shell",))
        with self.assertRaisesRegex(ValueError, "unsupported validation"):
            RecoveryPolicy(validation_profiles=("dsh-b-unknown-v1",))
        with self.assertRaisesRegex(ValueError, "at least"):
            RecoveryPolicy(backoff_seconds=30, max_backoff_seconds=20)

    def test_classification_uses_explicit_source_and_code_not_summary(self) -> None:
        self.assertEqual(
            classify_failure({
                "source": "authority_readiness", "origin": "environment",
                "code": "missing-dependency", "summary": "auth",
            }),
            "dependency",
        )
        self.assertEqual(
            classify_failure({
                "source": "authority_readiness", "origin": "environment",
                "check_id": "pnpm:linker", "code": "missing-dependency",
            }),
            "dependency",
        )
        self.assertEqual(
            classify_failure({"source": "authority_readiness", "code": "toolchain-mismatch"}),
            "environment",
        )
        self.assertEqual(
            classify_failure({"source": "authority_validation", "code": "transport-timeout"}),
            "network",
        )
        self.assertEqual(
            classify_failure({"source": "authority_validation", "code": "authentication-required"}),
            "auth",
        )
        self.assertEqual(
            classify_failure({"source": "authority_validation", "code": "validation-failure", "implementation_ready": True}),
            "validation_failure",
        )
        self.assertEqual(
            classify_failure({"source": "authority_validation", "code": "tooling-bug"}),
            "tooling_bug",
        )

    def test_classification_keeps_unknown_and_model_attribution_conservative(self) -> None:
        self.assertEqual(classify_failure({"source": "unknown", "origin": "auth", "code": "auth"}), "unknown")
        self.assertEqual(
            classify_failure({"source": "direct_attribution", "origin": "model", "summary": "verification failed"}),
            "unknown",
        )
        self.assertEqual(
            classify_failure({"source": "direct_attribution", "origin": "verification", "implementation_ready": True}),
            "validation_failure",
        )
        self.assertEqual(
            classify_failure({"source": "direct_attribution", "origin": "verification", "implementation_ready": False}),
            "validation_failure",
        )
        self.assertEqual(
            classify_failure({"source": "direct_attribution", "origin": "cancel"}),
            "user_pause",
        )

    def test_plan_priority_policy_pause_acceptance_and_unknown_effects(self) -> None:
        enabled = RecoveryPolicy(enabled=True)
        self.assertEqual(plan_recovery(RecoveryPolicy(), observation("dependency"))["state"], "suspended")
        self.assertEqual(
            plan_recovery(enabled, observation("dependency", task_state="paused")),
            {
                "category": "dependency", "action": None, "state": "suspended",
                "reason_kind": "user_pause", "owner": "user",
                "requires_authorization": False, "next_wakeup_at": None,
            },
        )
        self.assertEqual(plan_recovery(enabled, observation("unknown", task_state="accepted"))["state"], "resolved")
        blocked = plan_recovery(enabled, observation("auth"))
        self.assertEqual(blocked["state"], "needs_action")
        self.assertTrue(blocked["requires_authorization"])
        self.assertEqual(plan_recovery(enabled, observation("unknown_effects"))["reason_kind"], "unknown_effects")
        self.assertTrue(plan_recovery(enabled, observation("unknown_effects"))["requires_authorization"])
        self.assertEqual(plan_recovery(enabled, observation("unknown", node_state="indeterminate"))["state"], "suspended")
        self.assertEqual(plan_recovery(enabled, observation("unknown", approval_denied=True))["state"], "suspended")

    def test_plan_budget_and_repair_wait_are_bounded(self) -> None:
        policy = RecoveryPolicy(enabled=True, max_action_attempts=2, time_budget_seconds=10)
        self.assertEqual(
            plan_recovery(policy, observation("dependency", action_attempts=2))["reason_kind"],
            "budget_exhausted",
        )
        self.assertEqual(
            plan_recovery(policy, observation("dependency", elapsed_seconds=10))["reason_kind"],
            "budget_exhausted",
        )
        waiting = plan_recovery(policy, observation("tooling_bug", repair_linked=True))
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["reason_kind"], "repair_deployment_wait")
        self.assertEqual(waiting["next_wakeup_at"], "2026-09-11T12:00:30+00:00")

    def test_plan_dependency_uses_only_authorized_observe_or_materialize(self) -> None:
        materialize = RecoveryPolicy(enabled=True, allowed_actions=("observe_readiness", "materialize_dependencies"))
        result = plan_recovery(materialize, observation("dependency", dependencies_ready=False))
        self.assertEqual(result["action"], "materialize_dependencies")
        observe = plan_recovery(RecoveryPolicy(enabled=True), observation("dependency", dependencies_ready=False))
        self.assertEqual(observe["action"], "observe_readiness")
        self.assertEqual(
            plan_recovery(RecoveryPolicy(enabled=True, allowed_actions=()), observation("dependency"))["state"],
            "needs_action",
        )

    def test_plan_validation_requires_ready_implementation_and_profile(self) -> None:
        policy = RecoveryPolicy(enabled=True, allowed_actions=("observe_readiness", "narrow_validation"), validation_profiles=("dsh-b-ipc-v1",))
        not_ready = plan_recovery(policy, observation("validation_failure", implementation_ready=False, validation_profile="dsh-b-ipc-v1"))
        self.assertEqual(not_ready["action"], "observe_readiness")
        missing_profile = plan_recovery(policy, observation("validation_failure", validation_profile="dsh-b-pairing-check-v1"))
        self.assertEqual(missing_profile["reason_kind"], "validation_authorization_required")
        selected = plan_recovery(policy, observation("validation_failure", validation_profile="dsh-b-ipc-v1"))
        self.assertEqual(selected["action"], "narrow_validation")

    def test_plan_validation_success_requires_source_only_authorization(self) -> None:
        policy = RecoveryPolicy(enabled=True, allowed_actions=("source_only_recovery",))
        result = plan_recovery(policy, observation("validation_failure", validation_succeeded=True))
        self.assertEqual(result["action"], "source_only_recovery")
        denied = plan_recovery(RecoveryPolicy(enabled=True), observation("validation_failure", validation_succeeded=True))
        self.assertEqual(denied["reason_kind"], "source_recovery_not_authorized")

    def test_readiness_ready_advances_to_validation_source_only_or_resume(self) -> None:
        policy = RecoveryPolicy(
            enabled=True,
            allowed_actions=("narrow_validation", "source_only_recovery", "resume_node"),
            validation_profiles=("dsh-b-ipc-v1",),
        )
        validation = plan_recovery(policy, observation(
            "validation_failure", readiness_ready=True, validation_profile="dsh-b-ipc-v1",
        ))
        self.assertEqual(validation["action"], "narrow_validation")
        source_only = plan_recovery(policy, observation(
            "validation_failure", readiness_ready=True, validation_succeeded=True,
        ))
        self.assertEqual(source_only["action"], "source_only_recovery")
        resume = plan_recovery(policy, observation(
            "dependency", phase="pre_execution", readiness_ready=True,
            only_missing_dependency=True,
        ))
        self.assertEqual(resume["action"], "resume_node")
        self.assertEqual(resume["reason_kind"], "resume_after_readiness")

    def test_plan_tooling_bug_requests_one_repair_then_waits(self) -> None:
        policy = RecoveryPolicy(
            enabled=True,
            allowed_actions=("request_repair",),
            repair_repository="/repo",
            repair_allowed_scopes=("src/codex_workbench",),
        )
        request = plan_recovery(policy, observation("tooling_bug"))
        self.assertEqual(request["action"], "request_repair")
        pending = plan_recovery(policy, observation("tooling_bug", repair_requested=True))
        self.assertEqual(pending["state"], "waiting")
        self.assertEqual(pending["reason_kind"], "repair_request_pending")
        fresh = plan_recovery(policy, observation("tooling_bug", repair_deployed_verified=True))
        self.assertEqual(fresh["action"], None)
        self.assertEqual(fresh["reason_kind"], "readiness_not_authorized")

    def test_plan_capacity_waits_and_repeated_action_backoffs(self) -> None:
        policy = RecoveryPolicy(enabled=True, allowed_actions=("observe_readiness",), backoff_seconds=30, max_backoff_seconds=60)
        capacity = plan_recovery(policy, observation("capacity"))
        self.assertEqual(capacity["state"], "waiting")
        self.assertEqual(capacity["next_wakeup_at"], "2026-09-11T12:00:30+00:00")
        repeated = plan_recovery(policy, observation(
            "environment", last_action="observe_readiness", action_attempts=2,
            last_action_at="2026-09-11T12:00:00+00:00",
        ))
        self.assertEqual(repeated["reason_kind"], "backoff")
        self.assertEqual(repeated["next_wakeup_at"], "2026-09-11T12:01:00+00:00")
        due = plan_recovery(policy, observation(
            "environment", last_action="observe_readiness", action_attempts=2,
            retry_due_at="2026-09-11T12:00:30+00:00",
            now="2026-09-11T12:01:00+00:00",
        ))
        self.assertEqual(due["action"], "observe_readiness")

    def test_deployed_repair_observes_once_then_advances_from_fresh_readiness(self) -> None:
        policy = RecoveryPolicy(
            enabled=True,
            allowed_actions=("observe_readiness", "narrow_validation"),
            validation_profiles=("dsh-b-ipc-v1",),
        )
        observe = plan_recovery(policy, observation("validation_failure", repair_deployed=True))
        self.assertEqual(observe["action"], "observe_readiness")
        advance = plan_recovery(policy, observation(
            "validation_failure", repair_deployed=True, readiness_ready=True,
            validation_profile="dsh-b-ipc-v1",
        ))
        self.assertEqual(advance["action"], "narrow_validation")


if __name__ == "__main__":
    unittest.main()
