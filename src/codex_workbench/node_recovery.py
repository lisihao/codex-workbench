"""Bounded blocked-node reconciliation on the existing Authority control turn.

The Coordinator owns scheduling and the worker pool. This component consumes
durable events, prepares one fixed action, and records its receipt; it owns
neither a background thread nor another task database.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import time
from typing import Any, Callable, Mapping, Protocol

from .model import canonical_hash, now_iso
from .node_recovery_observation import collect_node_observation
from .node_recovery_policy import RecoveryPolicy, plan_recovery
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError, WorkbenchStore


class RecoveryActions(Protocol):
    """A fixed action adapter; uncertain effects are observed, never replayed."""

    def prepare(self, observation: dict, action: str, request_id: str, **kwargs) -> dict: ...
    def execute(self, plan: dict) -> dict: ...
    def reconcile(self, plan: dict) -> dict | None: ...


_PROGRESS_FIELDS = frozenset({
    "readiness_ready", "recovery_readiness_ref", "dependencies_ready", "last_validation_profile",
    "validated_source_delta", "validation_audit_ref", "validated_profiles",
    "validation_succeeded", "validated_install_manifest", "validated_runtime_fingerprint",
    "repair_requested", "repair_linked", "repair_deployed",
    "repair_deployed_verified", "repair_task_id", "repair_request_id", "repair_fingerprint",
    "recovery_resumed", "last_action", "last_action_at",
    "retry_due_at",
})
_EVENT_TYPES = frozenset({
    "node.blocked", "node.failed", "node.accepted", "node.started",
    "node.blocked_worktree_recovery_rolled_back",
    "task.state_changed", "approval.decided", "node_recovery.policy_configured",
})
_REPAIR_DELIVERY_EVENT_TYPES = frozenset({
    "delivery_objective.rollback_verified",
    "delivery_objective.stage_succeeded",
    "delivery_objective.retry_scheduled",
    "delivery_objective.decision_required",
})


def _elapsed(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


class NodeRecoveryReconciler:
    """Drive enabled policies with cursor wakeups and one bounded fallback sweep."""

    def __init__(
        self, store: WorkbenchStore, *, coordinator_epoch: int,
        adapters: Mapping[str, RecoveryActions], sweep_seconds: float = 30,
        observer: Callable[..., dict[str, Any]] = collect_node_observation,
        delivery_observer: Callable[[dict], dict] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not 1 <= sweep_seconds <= 300:
            raise ValueError("recovery sweep_seconds must be between 1 and 300")
        self.store = store
        self.recovery = NodeRecoveryStore(store)
        self.coordinator_epoch = coordinator_epoch
        self.owner_id = f"coordinator-{coordinator_epoch}-node-recovery"
        self.adapters = dict(adapters)
        self.observer = observer
        self.delivery_observer = delivery_observer
        self.sweep_seconds = sweep_seconds
        self.monotonic = monotonic
        self._next_sweep = 0.0
        self._task_cursor: str | None = None

    def _decision(self, observation: dict, episode: dict | None, policy: RecoveryPolicy) -> dict:
        current = dict(observation)
        current["now"] = now_iso()
        current["elapsed_seconds"] = _elapsed(episode["created_at"], current["now"]) if episode else 0
        current["action_attempts"] = 0
        decision = plan_recovery(policy, current)
        if current.get("observation_available") is False:
            if decision["state"] == "suspended" and decision["reason_kind"] in {"policy_disabled", "user_pause"}:
                return {
                    "category": current["category"], "state": "suspended", "action": None,
                    "reason_kind": "observation_unavailable", "control_reason": decision["reason_kind"],
                    "owner": "authority", "requires_authorization": False, "next_wakeup_at": None,
                }
            return {
                "category": current["category"], "state": "waiting", "action": None,
                "reason_kind": "observation_unavailable", "owner": "authority",
                "requires_authorization": False,
                "next_wakeup_at": (
                    datetime.fromisoformat(current["now"]) + timedelta(seconds=policy.backoff_seconds)
                ).isoformat(),
            }
        if current.get("recovery_resumed") is True:
            return {
                "category": current["category"], "state": "resolved", "action": None,
                "reason_kind": "original_node_resumed", "owner": "authority",
                "requires_authorization": False, "next_wakeup_at": None,
            }
        action = decision.get("action")
        stage = (
            f"narrow_validation:{current.get('validation_profile')}"
            if action == "narrow_validation" else action
        )
        if stage is None and episode is not None:
            stage = episode.get("current_stage_key")
        if episode is not None and stage is not None:
            current["action_attempts"] = episode.get("stage_attempts", {}).get(stage, 0)
            decision = plan_recovery(policy, current)
        if stage is not None:
            decision["stage_key"] = stage
        return decision

    def _refresh_node(self, task_id: str, node_id: str, cursor: int) -> dict:
        observed = self.observer(self.store, task_id, node_id, source_event_cursor=cursor)
        observed["attempt"] = observed["node_attempt"]
        policy = RecoveryPolicy.from_dict(self.recovery.get_policy(task_id)["policy"])
        if observed.get("observation_available") is False:
            return self._decision(observed, None, policy)
        prior = None
        for item in self.recovery.list_summary(task_id=task_id, limit=100):
            if (item["node_id"] == node_id and item["node_attempt"] == observed["node_attempt"]
                    and item["failure_fingerprint"] == observed["failure_fingerprint"]):
                prior = self.recovery.get_episode(item["episode_id"])
                break
        if prior is not None:
            if any(action["state"] in {"unknown", "executing"} for action in prior.get("actions", [])):
                # A fresh poll is not a lost write receipt. Only the exact
                # adapter receipt may settle the old intent after restart.
                return prior
            if prior.get("repair") and not prior["repair"].get("deployed_at") and self.delivery_observer is not None:
                delivery = self.delivery_observer(prior)
                if delivery["state"] == "verified":
                    prior = self.recovery.mark_repair_deployed(
                        prior["episode_id"], expected_revision=prior["revision"],
                        expected_node_attempt=prior["node_attempt"],
                        verified_deployment_fingerprint=delivery["verified_deployment_fingerprint"],
                        expected_repair_fingerprint=delivery["expected_repair_fingerprint"],
                        evidence_refs=delivery["evidence_refs"], verified_by="authority-delivery-reconciler",
                        fresh_readiness_verified=delivery.get("fresh_readiness_confirmed") is True,
                    )
                else:
                    observed.update({
                        "repair_wait_kind": delivery["reason_kind"], "repair_wait_state": delivery["state"],
                        "repair_wait_owner": delivery["owner"],
                        "repair_wait_requires_authorization": delivery["requires_authorization"],
                        "repair_fresh_readiness_required": delivery.get("fresh_readiness_required") is True,
                    })
                    if delivery["reason_kind"] == "repair_accepted_not_deployed":
                        observed.update({"repair_wait_state": "needs_action", "repair_wait_owner": "user",
                                         "repair_wait_requires_authorization": True})
            for key in _PROGRESS_FIELDS:
                if key in prior["observation"]:
                    observed[key] = prior["observation"][key]
            observed["source_event_cursor"] = max(cursor, prior["source_event_cursor"])
        profiles = policy.validation_profiles
        validated = observed.get("validated_profiles", [])
        remaining = [profile for profile in profiles if profile not in validated]
        if remaining:
            observed["validation_profile"] = remaining[0]
        observed["validation_succeeded"] = bool(profiles) and not remaining
        if prior is not None and prior.get("repair"):
            observed["repair_linked"] = True
            observed["repair_deployed"] = bool(prior["repair"].get("deployed_at"))
        return self.recovery.record_episode(observed, self._decision(observed, prior, policy))

    def _refresh_task(self, task_id: str, cursor: int) -> None:
        policy = self.recovery.get_policy(task_id)["policy"]
        if not policy["enabled"]:
            return
        # Read node identities without projecting historical worker results.
        with self.store.connection() as connection:
            nodes = connection.execute(
                "SELECT node_id FROM nodes WHERE task_id = ? AND state IN ('blocked', 'failed', 'indeterminate') ORDER BY node_id LIMIT 100",
                (task_id,),
            ).fetchall()
        for node in nodes:
            self._refresh_node(task_id, str(node["node_id"]), cursor)
        self._observe_running_progress(task_id, int(policy["time_budget_seconds"]))

    def _observe_running_progress(self, task_id: str, budget_seconds: int) -> None:
        """Record one bounded durable-evidence investigation, never a deadlock verdict."""
        timestamp = now_iso()
        threshold = max(30, min(300, budget_seconds // 3))
        with self.store.transaction() as connection:
            for node in connection.execute(
                "SELECT node_id, attempt, started_at FROM nodes WHERE task_id = ? AND state = 'running' LIMIT 100",
                (task_id,),
            ).fetchall():
                if not node["started_at"] or _elapsed(str(node["started_at"]), timestamp) < threshold:
                    continue
                previous = connection.execute(
                    "SELECT 1 FROM events WHERE task_id = ? AND node_id = ? AND event_type = 'node.progress_unknown' AND json_extract(payload_json, '$.attempt') = ? LIMIT 1",
                    (task_id, node["node_id"], node["attempt"]),
                ).fetchone()
                if previous is not None:
                    continue
                progress = connection.execute(
                    """SELECT cursor, event_type, created_at FROM events WHERE task_id = ? AND node_id = ?
                       AND created_at >= ? AND event_type IN ('node.started', 'worktree.allocated',
                       'node.check_completed', 'node.patch_recorded', 'node.phase_changed')
                       ORDER BY cursor DESC LIMIT 1""",
                    (task_id, node["node_id"], node["started_at"]),
                ).fetchone()
                if progress is not None and _elapsed(str(progress["created_at"]), timestamp) < threshold:
                    continue
                self.store._event(connection, "node.progress_unknown", task_id, str(node["node_id"]), {
                    "attempt": int(node["attempt"]), "owner": "executor",
                    "started_at": str(node["started_at"]), "observed_at": timestamp,
                    "last_phase_cursor": int(progress["cursor"]) if progress is not None else None,
                    "last_phase": str(progress["event_type"]) if progress is not None else None,
                    "investigation": "bounded-current-attempt-durable-events",
                    "execution_progress": "unknown", "deadlock_confirmed": False,
                    "model_woken": False, "process_interrupted": False,
                    "next_action": "retain the existing executor deadline and await substantive evidence",
                }, created_at=timestamp)

    def _linked_repair_parents(self, repair_task_id: str) -> tuple[str, ...]:
        """Return the bounded set of enabled parents waiting on one repair task."""
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT repairs.task_id
                FROM node_recovery_repairs AS repairs
                JOIN node_recovery_policies AS policies ON policies.task_id = repairs.task_id
                WHERE repairs.repair_task_id = ?
                  AND json_extract(policies.policy_json, '$.enabled') = 1
                ORDER BY repairs.task_id
                LIMIT 100
                """,
                (repair_task_id,),
            ).fetchall()
        return tuple(str(row["task_id"]) for row in rows)

    def _observe(self) -> None:
        cursor = self.recovery.read_cursor()
        with self.store.connection() as connection:
            events = connection.execute(
                "SELECT cursor, event_type, task_id FROM events WHERE cursor > ? ORDER BY cursor LIMIT 200",
                (cursor,),
            ).fetchall()
        dirty: dict[str, int] = {}
        repair_events: dict[str, int] = {}
        for event in events:
            if event["task_id"] and event["event_type"] in _EVENT_TYPES:
                task_id = str(event["task_id"])
                dirty[task_id] = max(dirty.get(task_id, 0), int(event["cursor"]))
            elif event["task_id"] and event["event_type"] in _REPAIR_DELIVERY_EVENT_TYPES:
                task_id = str(event["task_id"])
                repair_events[task_id] = max(repair_events.get(task_id, 0), int(event["cursor"]))
        for repair_task_id, event_cursor in repair_events.items():
            for parent_task_id in self._linked_repair_parents(repair_task_id):
                dirty[parent_task_id] = max(dirty.get(parent_task_id, 0), event_cursor)
        for task_id, event_cursor in dirty.items():
            self._refresh_task(task_id, event_cursor)
        if events:
            self.recovery.advance_cursor(cursor, int(events[-1]["cursor"]))
        if self.monotonic() >= self._next_sweep:
            page = self.recovery.list_enabled_tasks(limit=100, cursor=self._task_cursor)
            tasks = page["tasks"]
            for task in tasks:
                self._reconcile_unknown(task["task_id"])
                self._refresh_task(task["task_id"], 0)
            self._task_cursor = page["next_cursor"]
            self._next_sweep = self.monotonic() + self.sweep_seconds

    @staticmethod
    def _receipt_payload(value: dict | None) -> dict | None:
        if value is None:
            return None
        if "journal_status" not in value:
            return value
        receipt = value.get("receipt")
        if isinstance(receipt, dict):
            return {**receipt, "journal_status": value["journal_status"]}
        return {"ok": False, "known_effects": False, "stage_succeeded": False,
                "journal_status": value["journal_status"], "observation_patch": {}}

    def _reconcile_unknown(self, task_id: str) -> None:
        for summary in self.recovery.list_summary(task_id=task_id, limit=100):
            episode = self.recovery.get_episode(summary["episode_id"])
            for action in episode.get("actions", []):
                if action["state"] != "unknown":
                    continue
                plan = action["action_input"]
                adapter = self.adapters.get(plan.get("action"))
                if adapter is None:
                    continue
                receipt = self._receipt_payload(adapter.reconcile(plan))
                if receipt is None or receipt.get("known_effects") is not True:
                    continue
                observed, receipt = self._receipt_progress(episode, plan, receipt)
                next_decision = self._decision(observed, episode, RecoveryPolicy.from_dict(episode["policy"]))
                settled = self.recovery.reconcile_action_receipt(
                    episode["episode_id"], action["action_key"], action["action_fingerprint"],
                    coordinator_epoch=self.coordinator_epoch,
                    expected_intent_coordinator_epoch=action["intent_coordinator_epoch"],
                    expected_intent_lease_epoch=action["intent_lease_epoch"],
                    expected_revision=episode["revision"], expected_node_attempt=episode["node_attempt"],
                    receipt=receipt, next_decision=next_decision, effect_dispatched=True,
                    material_progress=receipt.get("stage_succeeded") is True,
                )
                episode = settled["episode"]

    @staticmethod
    def _receipt_progress(episode: dict, plan: dict, receipt: dict) -> tuple[dict, dict]:
        """Apply identical check-reuse facts for direct and reconciled receipts."""
        observed = {**episode["observation"], **receipt.get("observation_patch", {})}
        if plan["action"] == "narrow_validation" and receipt.get("ok") is True:
            prior = episode["observation"]
            preview = plan.get("fresh_preview", {})
            same_inputs = all(
                prior.get(prior_key) is not None and prior.get(prior_key) == preview.get(preview_key)
                for prior_key, preview_key in (
                    ("validated_source_delta", "source_delta_sha256"),
                    ("validated_install_manifest", "install_manifest_sha256"),
                    ("validated_runtime_fingerprint", "runtime_fingerprint"),
                )
            )
            validated = list(prior.get("validated_profiles", [])) if same_inputs else []
            profile = plan.get("arguments", {}).get("check_id") or observed.get("validation_profile")
            if profile and profile not in validated:
                validated.append(profile)
            observed["validated_profiles"] = validated
            policy = RecoveryPolicy.from_dict(episode["policy"])
            remaining = [item for item in policy.validation_profiles if item not in validated]
            observed["validation_succeeded"] = bool(policy.validation_profiles) and not remaining
            if remaining:
                observed["validation_profile"] = remaining[0]
        observed["last_action"] = plan["action"]
        observed["last_action_at"] = now_iso()
        return observed, {**receipt, "observation_patch": {
            **receipt.get("observation_patch", {}),
            **{key: value for key, value in observed.items() if key in _PROGRESS_FIELDS},
        }}

    def reconcile_once(self) -> list[dict[str, Any]]:
        """Consume bounded observations, then execute at most one due action."""
        if self.store.delivery_admission_gate() is not None:
            return []
        self._observe()
        for due in self.recovery.list_due(limit=20):
            episode = self._refresh_node(due["task_id"], due["node_id"], 0)
            if episode["state"] != "ready" or episode.get("action") is None:
                continue
            claimed = self.recovery.claim_due(
                episode["episode_id"], self.owner_id, self.coordinator_epoch,
                expected_revision=episode["revision"], expected_node_attempt=episode["node_attempt"],
                lease_seconds=300,
            )
            if claimed is not None:
                return [self._execute(claimed)]
        return []

    def _execute(self, episode: dict) -> dict:
        action = str(episode["action"])
        stage = episode["current_stage_key"]
        attempt = episode["stage_attempts"].get(stage, 0) + 1
        request_id = "node-recovery-" + canonical_hash({
            "episode_id": episode["episode_id"], "stage": stage, "attempt": attempt,
        })[:40]
        adapter = self.adapters.get(action)
        preparation_error: Exception | None = None
        plan: dict
        try:
            if adapter is None:
                raise ValueError("the authorized action has no installed adapter")
            options = {"validation_profile": episode["observation"].get("validation_profile")} if action == "narrow_validation" else {}
            plan = adapter.prepare(episode["observation"], action, request_id, **options)
            plan = {**plan, "stage_key": stage}
        except (OSError, ValueError, StateConflictError) as error:
            preparation_error = error
            plan = {
                "action": action, "request_id": request_id, "stage_key": stage,
                "task_revision": episode["task_revision"], "node_attempt": episode["node_attempt"],
                "preparation_error": type(error).__name__,
            }
        fingerprint = canonical_hash(plan)
        intended = self.recovery.begin_action(
            episode["episode_id"], request_id, fingerprint, plan,
            owner_id=self.owner_id, coordinator_epoch=self.coordinator_epoch,
            lease_epoch=episode["lease_epoch"], expected_revision=episode["revision"],
            expected_node_attempt=episode["node_attempt"],
        )
        episode = intended["episode"]
        if action == "request_repair" and preparation_error is None and not episode.get("repair"):
            # Persist the reserved relationship before enqueue. This is an
            # intent, not evidence that the repair exists or is deployed.
            episode = self.recovery.link_repair(
                episode["episode_id"], owner_id=self.owner_id,
                coordinator_epoch=self.coordinator_epoch, lease_epoch=episode["lease_epoch"],
                expected_revision=episode["revision"], repair_task_id=plan["repair_task_id"],
                repair_request_id=plan["repair_request_id"], repair_fingerprint=plan["repair_fingerprint"],
            )
        receipt: dict | None = None
        if intended["existing"]:
            if intended["action"]["state"] == "completed":
                receipt = intended["action"].get("receipt")
            elif adapter is not None:
                receipt = adapter.reconcile(plan)
        elif preparation_error is not None:
            receipt = {
                "ok": False, "known_effects": True, "stage_succeeded": False,
                "reason_kind": "action_preflight_failed", "error_type": type(preparation_error).__name__,
                "evidence_refs": [], "observation_patch": {},
            }
        elif adapter is not None:
            try:
                receipt = adapter.execute(plan)
            except Exception:
                # An exception after admission is not proof that no effect
                # occurred. The adapter may only query the original receipt.
                receipt = adapter.reconcile(plan)
        if receipt is None:
            receipt = {
                "ok": False, "known_effects": False, "stage_succeeded": False,
                "reason_kind": "unknown_effects", "evidence_refs": [], "observation_patch": {},
            }
        receipt = self._receipt_payload(receipt)
        assert receipt is not None
        known = receipt.get("known_effects") is True
        observed, receipt = self._receipt_progress(episode, plan, receipt)
        counting = {**episode, "stage_attempts": {**episode["stage_attempts"], stage: attempt}}
        next_decision = self._decision(observed, counting, RecoveryPolicy.from_dict(episode["policy"]))
        return self.recovery.settle_action(
            episode["episode_id"], request_id, owner_id=self.owner_id,
            coordinator_epoch=self.coordinator_epoch, lease_epoch=episode["lease_epoch"],
            expected_revision=episode["revision"], expected_node_attempt=episode["node_attempt"],
            receipt=receipt, next_decision=next_decision,
            receipt_state="completed" if known else "unknown", effect_dispatched=not intended["existing"],
            material_progress=receipt.get("stage_succeeded") is True,
        )
