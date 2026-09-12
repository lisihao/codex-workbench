"""Read-only readiness observation; no dependency install or task retry action."""
from __future__ import annotations

from pathlib import Path
from hashlib import sha256

from .execution_readiness import assess_execution_readiness
from .model import canonical_hash, canonical_json
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError


class ReadinessNodeActions:
    """Expose only the existing bounded, non-model readiness assessor."""

    def __init__(self, config, store, *, readiness_request_factory):
        self.config = config
        self.store = store
        self.request_factory = readiness_request_factory

    def _current(self, observation):
        task = self.store.get_task(observation["task_id"])
        node = next(node for node in task["nodes"] if node["node_id"] == observation["node_id"])
        policy = NodeRecoveryStore(self.store).get_policy(task["task_id"])
        if not policy["policy"]["enabled"] or "observe_readiness" not in policy["policy"]["allowed_actions"]:
            raise StateConflictError("readiness observation is not enabled for this task")
        if (task["state"] in {"paused", "cancelled", "needs_approval"}
                or task["state_revision"] != observation["task_revision"]
                or node["state"] != "blocked" or node["attempt"] != observation["node_attempt"]):
            raise StateConflictError("readiness observation task/attempt changed")
        if not node.get("worktree") or not Path(node["worktree"]).is_absolute():
            raise StateConflictError("readiness observation has no allocated worktree")
        return {"worktree": node["worktree"], "policy_revision": policy["policy_revision"]}

    def prepare(self, observation, action, request_id):
        if action != "observe_readiness":
            raise ValueError("this adapter only observes readiness")
        return {
            "action": action, "request_id": request_id, "stage_key": action,
            **{key: observation[key] for key in ("task_id", "node_id", "task_revision", "node_attempt")},
            **self._current(observation),
        }

    def execute(self, plan):
        if self._current(plan) != {key: plan[key] for key in ("worktree", "policy_revision")}:
            raise StateConflictError("readiness allocation or policy changed")
        request = self.request_factory(Path(plan["worktree"]))
        report = assess_execution_readiness(request)
        payload = report.to_dict()
        authority = self.store.authority_status()
        manifest_digest = (
            sha256(self.config.install_manifest.read_bytes()).hexdigest()
            if self.config.install_manifest.is_file() else None
        )
        receipt = {
            "kind": "node-recovery-readiness/v1",
            **{key: plan[key] for key in ("task_id", "node_id", "node_attempt", "task_revision")},
            "authority_epoch": authority.get("authority_epoch") if authority is not None else None,
            "authority_instance_id": authority.get("instance_id") if authority is not None else None,
            "install_manifest_sha256": manifest_digest, "report": payload,
        }
        ref = self.store.artifacts.put_text(canonical_json(receipt), "recovery-readiness.json")
        current = self._current(plan)
        if current != {key: plan[key] for key in ("worktree", "policy_revision")}:
            raise StateConflictError("readiness allocation or policy changed during observation")
        return {
            "ok": report.ready, "known_effects": True, "stage_succeeded": report.ready,
            "readiness_fingerprint": canonical_hash({key: value for key, value in payload.items() if key != "elapsed_ms"}),
            "evidence_refs": [ref], "observation_patch": {
                "readiness_ready": report.ready, "recovery_readiness_ref": ref,
            },
        }

    def reconcile(self, plan):
        # A missing observation receipt is not a new grant to repeat work.
        # The controller preserves the unknown intent for explicit resolution.
        return None
