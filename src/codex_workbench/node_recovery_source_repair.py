"""Queue one policy-authorized source repair through the existing recovery lane."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .blocked_source_repair import blocked_source_repair, blocked_source_repair_receipt
from .model import canonical_hash
from .store import StateConflictError, WorkbenchStore


_ACTION = "repair_source"
_REASON = "Repair the current blocked worker source after validation failure; retain final verification."


class SourceRepairNodeActions:
    """Use a fresh preview and reconcile only the original queued receipt."""

    def __init__(self, store: WorkbenchStore):
        self.store = store

    def prepare(self, observation: dict, action: str, request_id: str, **options) -> dict:
        if action != _ACTION or options:
            raise ValueError("unsupported source repair action")
        if (
            observation.get("observation_available") is not True
            or observation.get("node_state") != "blocked"
            or observation.get("node_is_verifier") is not False
            or observation.get("category") != "validation_failure"
            or observation.get("readiness_ready") is not True
            or observation.get("validation_succeeded") is True
        ):
            raise ValueError("source repair requires an observed ready blocked worker validation failure")
        arguments = {
            "task_id": observation["task_id"],
            "node_id": observation["node_id"],
            "expected_revision": observation["task_revision"],
            "expected_attempt": observation["node_attempt"],
            "expected_contract_hash": observation["contract_hash"],
            "request_id": request_id,
            "reason": _REASON,
        }
        preview = blocked_source_repair(self.store, **arguments, dry_run=True)
        binding = {
            "arguments": arguments,
            "fingerprint": preview["fingerprint"],
            "request_fingerprint": preview["request_fingerprint"],
        }
        return {
            "action": _ACTION,
            "request_id": request_id,
            "task_id": observation["task_id"],
            "node_id": observation["node_id"],
            "task_revision": observation["task_revision"],
            "node_attempt": observation["node_attempt"],
            "repair_binding": binding,
            "repair_binding_fingerprint": canonical_hash(binding),
        }

    @staticmethod
    def _binding(plan: dict) -> Mapping[str, Any]:
        binding = plan.get("repair_binding")
        if (
            plan.get("action") != _ACTION
            or not isinstance(binding, Mapping)
            or canonical_hash(binding) != plan.get("repair_binding_fingerprint")
        ):
            raise ValueError("invalid durable source repair binding")
        arguments = binding["arguments"]
        if not isinstance(arguments, Mapping) or arguments.get("request_id") != plan.get("request_id"):
            raise ValueError("source repair request identity changed")
        for outer, inner in (
            ("task_id", "task_id"), ("node_id", "node_id"),
            ("task_revision", "expected_revision"), ("node_attempt", "expected_attempt"),
        ):
            if plan.get(outer) != arguments.get(inner):
                raise ValueError("source repair task binding changed")
        return binding

    def execute(self, plan: dict) -> dict:
        binding = self._binding(plan)
        try:
            receipt = blocked_source_repair(
                self.store, **binding["arguments"], dry_run=False,
                expected_fingerprint=binding["fingerprint"],
            )
        except (StateConflictError, ValueError) as error:
            receipt = blocked_source_repair_receipt(self.store, plan["request_id"])
            if receipt is None:
                return {
                    "ok": False, "known_effects": True, "stage_succeeded": False,
                    "reason_kind": "source_repair_rejected", "error": str(error),
                    "observation_patch": {},
                }
        return self._receipt(plan, receipt)

    def reconcile(self, plan: dict) -> dict | None:
        self._binding(plan)
        receipt = blocked_source_repair_receipt(self.store, plan["request_id"])
        return None if receipt is None else self._receipt(plan, receipt)

    def _receipt(self, plan: dict, receipt: dict) -> dict:
        binding = self._binding(plan)
        if (
            receipt.get("request_id") != plan["request_id"]
            or receipt.get("request_fingerprint") != binding["request_fingerprint"]
            or receipt.get("queued") is not True
        ):
            raise ValueError("source repair receipt does not match its intent")
        return {
            "ok": True,
            "known_effects": True,
            "stage_succeeded": True,
            "receipt": {
                key: receipt[key] for key in (
                    "request_id", "request_fingerprint", "authorization_event_cursor",
                    "revision", "next_attempt", "queued",
                )
            },
            "observation_patch": {
                "recovery_resumed": True,
                "last_action": _ACTION,
            },
        }


__all__ = ["SourceRepairNodeActions"]
