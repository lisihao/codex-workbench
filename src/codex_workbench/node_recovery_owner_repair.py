"""Resume exhausted accepted-owner preparation after verified harness repair."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .model import canonical_hash
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError, WorkbenchStore


_ACTION = "resume_owner_repairs"
_PLAN_FIELDS = frozenset({
    "schema_version", "kind", "action", "stage_key", "request_id",
    "task_id", "node_id", "node_attempt", "task_revision",
    "policy_revision", "owner_repair_event_cursor", "owner_progress_fingerprint",
    "repair_fingerprint", "readiness_fingerprint", "action_fingerprint",
})


class OwnerRepairNodeActions:
    """Run one CAS-only owner requeue from exact deployment and readiness evidence."""

    def __init__(self, store: WorkbenchStore) -> None:
        self.store = store
        self.recovery = NodeRecoveryStore(store)

    def prepare(
        self,
        observation: Mapping[str, Any],
        action: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Bind an exhausted owner set to one verified repair deployment."""

        if action != _ACTION:
            raise ValueError("owner repair adapter supports only resume_owner_repairs")
        request_id = self._text(request_id, "request_id")
        task_id = self._text(observation.get("task_id"), "task_id")
        node_id = self._text(observation.get("node_id"), "node_id")
        node_attempt = self._integer(observation.get("node_attempt"), "node_attempt")
        task_revision = self._integer(observation.get("task_revision"), "task_revision")
        if observation.get("repair_deployed_verified") is not True:
            raise StateConflictError("owner repair resume requires verified repair deployment")
        if observation.get("readiness_ready") is not True:
            raise StateConflictError("owner repair resume requires fresh readiness")
        repair_fingerprint = self._text(
            observation.get("repair_fingerprint"), "repair_fingerprint"
        )
        readiness_fingerprint = self._text(
            observation.get("validated_runtime_fingerprint"),
            "validated_runtime_fingerprint",
        )
        candidate = self.store.exhausted_blocked_owner_repair_candidate(
            task_id, node_id, node_attempt,
        )
        if candidate["task_revision"] != task_revision:
            raise StateConflictError("owner repair resume task revision changed")
        policy_row = self.recovery.get_policy(task_id)
        policy = policy_row.get("policy")
        if (
            not isinstance(policy, Mapping)
            or policy.get("enabled") is not True
            or _ACTION not in policy.get("allowed_actions", [])
        ):
            raise StateConflictError("owner repair resume is not authorized by policy")
        plan = {
            "schema_version": 1,
            "kind": "node-recovery-owner-repair/v1",
            "action": _ACTION,
            "stage_key": _ACTION,
            "request_id": request_id,
            "task_id": task_id,
            "node_id": node_id,
            "node_attempt": node_attempt,
            "task_revision": task_revision,
            "policy_revision": int(policy_row["policy_revision"]),
            "owner_repair_event_cursor": candidate["event_cursor"],
            "owner_progress_fingerprint": candidate["progress_fingerprint"],
            "repair_fingerprint": repair_fingerprint,
            "readiness_fingerprint": readiness_fingerprint,
            "action_fingerprint": "",
        }
        plan["action_fingerprint"] = canonical_hash(self._unsigned(plan))
        return plan

    def execute(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Apply the exact owner requeue once, reconciling an uncertain return."""

        plan = self._validated(raw)
        existing = self.reconcile(plan)
        if existing is not None:
            return existing
        self._assert_policy_current(plan)
        try:
            receipt = self.store.resume_exhausted_blocked_owner_repairs(
                request_id=plan["request_id"],
                action_fingerprint=plan["action_fingerprint"],
                task_id=plan["task_id"],
                node_id=plan["node_id"],
                expected_attempt=plan["node_attempt"],
                expected_revision=plan["task_revision"],
                expected_event_cursor=plan["owner_repair_event_cursor"],
                expected_progress_fingerprint=plan["owner_progress_fingerprint"],
                repair_fingerprint=plan["repair_fingerprint"],
                readiness_fingerprint=plan["readiness_fingerprint"],
            )
        except (KeyError, StateConflictError, ValueError):
            reconciled = self.reconcile(plan)
            if reconciled is not None:
                return reconciled
            raise
        return self._receipt(plan, receipt)

    def _assert_policy_current(self, plan: Mapping[str, Any]) -> None:
        """Fence execution when the task-scoped action grant was revoked."""

        policy_row = self.recovery.get_policy(str(plan["task_id"]))
        policy = policy_row.get("policy")
        if (
            policy_row.get("policy_revision") != plan["policy_revision"]
            or not isinstance(policy, Mapping)
            or policy.get("enabled") is not True
            or _ACTION not in policy.get("allowed_actions", [])
        ):
            raise StateConflictError("owner repair resume policy changed")

    def reconcile(self, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        """Read the exact request receipt; absence never authorizes a replay."""

        plan = self._validated(raw)
        receipt = self.store.exhausted_owner_repair_resume_receipt(plan["request_id"])
        if receipt is None:
            return None
        if receipt.get("action_fingerprint") != plan["action_fingerprint"]:
            raise StateConflictError("owner repair resume receipt fingerprint changed")
        return self._receipt(plan, receipt)

    @staticmethod
    def _receipt(plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
        event_cursor = receipt.get("event_cursor")
        if type(event_cursor) is not int or event_cursor < 1:
            raise StateConflictError("owner repair resume receipt lacks an event cursor")
        return {
            "action": _ACTION,
            "request_id": plan["request_id"],
            "ok": True,
            "stage_succeeded": True,
            "known_effects": True,
            "evidence_refs": {"owner-repair-resume": f"event:{event_cursor}"},
            "observation_patch": {"owner_repairs_resumed": True},
        }

    @staticmethod
    def _validated(raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _PLAN_FIELDS:
            raise ValueError("owner repair resume plan has unsupported or missing fields")
        plan = dict(raw)
        if (
            plan.get("schema_version") != 1
            or plan.get("kind") != "node-recovery-owner-repair/v1"
            or plan.get("action") != _ACTION
            or plan.get("stage_key") != _ACTION
        ):
            raise ValueError("owner repair resume plan schema is invalid")
        for key in (
            "request_id", "task_id", "node_id", "owner_progress_fingerprint",
            "repair_fingerprint", "readiness_fingerprint", "action_fingerprint",
        ):
            OwnerRepairNodeActions._text(plan.get(key), key)
        for key in (
            "node_attempt", "task_revision", "policy_revision", "owner_repair_event_cursor",
        ):
            OwnerRepairNodeActions._integer(plan.get(key), key)
        if canonical_hash(OwnerRepairNodeActions._unsigned(plan)) != plan["action_fingerprint"]:
            raise ValueError("owner repair resume plan fingerprint is invalid")
        return plan

    @staticmethod
    def _unsigned(plan: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in plan.items() if key != "action_fingerprint"}

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty")
        return value

    @staticmethod
    def _integer(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value


__all__ = ["OwnerRepairNodeActions"]
