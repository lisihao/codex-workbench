"""Durable, task-scoped projection for deterministic blocked-node recovery.

The component deliberately owns no scheduler or adapter.  It records the
Authority's already-classified observations, fenced action intents, and
receipts beside the existing task/node ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .model import canonical_hash, canonical_json, now_iso
from .node_recovery_policy import RecoveryPolicy
from .store import StateConflictError, WorkbenchStore


NODE_RECOVERY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_recovery_policies (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    policy_json TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    configured_task_revision INTEGER NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_recovery_episodes (
    episode_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_attempt INTEGER NOT NULL,
    failure_fingerprint TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    policy_revision INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    category TEXT NOT NULL,
    phase TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_event_cursor INTEGER NOT NULL,
    state TEXT NOT NULL,
    action TEXT,
    owner TEXT NOT NULL,
    permission_required INTEGER NOT NULL,
    action_attempts INTEGER NOT NULL DEFAULT 0,
    current_stage_key TEXT,
    stage_attempts_json TEXT NOT NULL,
    time_budget_deadline_at TEXT NOT NULL,
    next_wakeup_at TEXT,
    last_material_progress_at TEXT NOT NULL,
    last_progress_json TEXT NOT NULL,
    state_revision INTEGER NOT NULL,
    owner_id TEXT,
    coordinator_epoch INTEGER NOT NULL DEFAULT 0,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    repair_request_id TEXT,
    repair_task_id TEXT,
    repair_deployment_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, node_id, node_attempt, failure_fingerprint),
    FOREIGN KEY(task_id, node_id) REFERENCES nodes(task_id, node_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS node_recovery_actions (
    action_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES node_recovery_episodes(episode_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_attempt INTEGER NOT NULL,
    action_key TEXT NOT NULL UNIQUE,
    stage_key TEXT NOT NULL,
    intent_coordinator_epoch INTEGER NOT NULL,
    intent_lease_epoch INTEGER NOT NULL,
    action_fingerprint TEXT NOT NULL,
    action_input_json TEXT NOT NULL,
    state TEXT NOT NULL,
    receipt_json TEXT,
    effect_dispatched INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE(episode_id, action_key),
    UNIQUE(episode_id, action_fingerprint)
);
CREATE TABLE IF NOT EXISTS node_recovery_repairs (
    repair_link_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    failure_fingerprint TEXT NOT NULL,
    episode_id TEXT NOT NULL REFERENCES node_recovery_episodes(episode_id) ON DELETE CASCADE,
    repair_request_id TEXT NOT NULL UNIQUE,
    repair_task_id TEXT NOT NULL,
    repair_fingerprint TEXT NOT NULL,
    verified_deployment_fingerprint TEXT,
    deployment_evidence_refs_json TEXT,
    deployed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, failure_fingerprint)
);
CREATE TABLE IF NOT EXISTS node_recovery_notifications (
    episode_id TEXT NOT NULL REFERENCES node_recovery_episodes(episode_id) ON DELETE CASCADE,
    reason_kind TEXT NOT NULL,
    event_cursor INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(episode_id, reason_kind)
);
CREATE TABLE IF NOT EXISTS node_recovery_cursors (
    projection TEXT PRIMARY KEY,
    event_cursor INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS node_recovery_episodes_due_idx
    ON node_recovery_episodes(state, next_wakeup_at, time_budget_deadline_at);
CREATE INDEX IF NOT EXISTS node_recovery_episodes_task_idx
    ON node_recovery_episodes(task_id, updated_at, episode_id);
CREATE INDEX IF NOT EXISTS node_recovery_actions_episode_idx
    ON node_recovery_actions(episode_id, created_at, action_id);
CREATE INDEX IF NOT EXISTS node_recovery_repairs_episode_idx
    ON node_recovery_repairs(episode_id, deployed_at);
"""


_PROJECTION_CURSOR = "node-recovery-v1"
_MAX_DOCUMENT_BYTES = 16 * 1024
_MAX_EVIDENCE_REFS = 64
_TEXT_LIMIT = 512
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_DOCUMENT_KEYS = frozenset(
    {
        "changed_paths",
        "ignored_paths",
        "raw_output",
        "stdout",
        "stderr",
        "secret",
        "token",
        "api_key",
        "password",
        "credential",
    }
)
_DECISION_STATES = frozenset({"waiting", "ready", "needs_action", "resolved", "suspended"})
_ACTION_STATES = frozenset({"executing", "completed", "unknown"})


class NodeRecoveryStore:
    """Persist recovery episodes through the existing Authority Store only."""

    def __init__(self, base_store: WorkbenchStore):
        if not isinstance(base_store, WorkbenchStore):
            raise TypeError("base_store must be a WorkbenchStore")
        self.base_store = base_store

    @property
    def artifacts(self):
        """Reuse the Authority content-addressed artifact store."""

        return self.base_store.artifacts

    def get_policy(self, task_id: str) -> dict[str, Any]:
        """Return the explicit policy, or the disabled default for a known task."""

        _text(task_id, "task_id")
        with self.base_store.connection() as connection:
            self._task_row(connection, task_id)
            row = connection.execute(
                "SELECT * FROM node_recovery_policies WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return {
                    "task_id": task_id,
                    "policy": RecoveryPolicy().to_dict(),
                    "policy_revision": 0,
                    "configured_task_revision": None,
                    "actor": None,
                    "created_at": None,
                    "updated_at": None,
                }
            return self._policy_row(row)

    def configure_policy(
        self,
        task_id: str,
        policy: RecoveryPolicy | Mapping[str, object],
        *,
        expected_task_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        """CAS one task-scoped recovery policy without changing task state."""

        _text(task_id, "task_id")
        _positive_int(expected_task_revision, "expected_task_revision")
        actor = _text(actor, "actor")
        normalized = _policy(policy)
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            task = self._task_row(connection, task_id)
            if int(task["state_revision"]) != expected_task_revision:
                raise StateConflictError(
                    f"expected task revision {expected_task_revision}, found {task['state_revision']}"
                )
            existing = connection.execute(
                "SELECT * FROM node_recovery_policies WHERE task_id = ?", (task_id,)
            ).fetchone()
            document = normalized.to_dict()
            if existing is not None and (
                json.loads(str(existing["policy_json"])) == document
                and str(existing["actor"]) == actor
                and int(existing["configured_task_revision"]) == expected_task_revision
            ):
                return self._policy_row(existing)
            revision = 1 if existing is None else int(existing["policy_revision"]) + 1
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO node_recovery_policies(
                        task_id, policy_json, policy_revision, configured_task_revision,
                        actor, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, canonical_json(document), revision, expected_task_revision, actor, timestamp, timestamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE node_recovery_policies
                    SET policy_json = ?, policy_revision = ?, configured_task_revision = ?,
                        actor = ?, updated_at = ?
                    WHERE task_id = ? AND policy_revision = ?
                    """,
                    (
                        canonical_json(document), revision, expected_task_revision, actor,
                        timestamp, task_id, int(existing["policy_revision"]),
                    ),
                )
            self.base_store._event(
                connection,
                "node_recovery.policy_configured",
                task_id,
                None,
                {
                    "policy_revision": revision,
                    "enabled": normalized.enabled,
                    "actor": actor,
                    "configured_task_revision": expected_task_revision,
                },
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM node_recovery_policies WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            return self._policy_row(row)

    def record_episode(
        self,
        observation: Mapping[str, object],
        decision: Mapping[str, object],
    ) -> dict[str, Any]:
        """Insert or materially update one failure-fingerprint episode.

        The caller supplies an already attributed Authority observation and a
        pure-policy decision.  Repeating either document byte-for-byte is a
        read-equivalent operation: it does not wake the episode or emit a
        second user-action notification.
        """

        observed = _observation(observation)
        planned = _decision(decision, observation=observed["document"])
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            task = self._task_row(connection, observed["task_id"])
            node = self._node_row(connection, observed["task_id"], observed["node_id"])
            if int(task["state_revision"]) != observed["task_revision"]:
                raise StateConflictError("recovery observation task revision is stale")
            if int(node["attempt"]) != observed["attempt"]:
                raise StateConflictError("recovery observation node attempt is stale")
            policy, policy_revision = self._policy_for_task(connection, observed["task_id"])
            episode_id = _episode_id(
                observed["task_id"], observed["node_id"], observed["attempt"], observed["failure_fingerprint"]
            )
            existing = connection.execute(
                """
                SELECT * FROM node_recovery_episodes
                WHERE task_id = ? AND node_id = ? AND node_attempt = ? AND failure_fingerprint = ?
                """,
                (
                    observed["task_id"], observed["node_id"], observed["attempt"],
                    observed["failure_fingerprint"],
                ),
            ).fetchone()
            if existing is not None:
                return self._update_episode_if_material(
                    connection,
                    existing,
                    observed=observed,
                    planned=planned,
                    policy=policy,
                    policy_revision=policy_revision,
                    timestamp=timestamp,
                )

            deadline = _after(timestamp, policy.time_budget_seconds)
            progress = {
                "kind": "node_recovery.episode_recorded",
                "source_event_cursor": observed["source_event_cursor"],
                "phase": observed["phase"],
            }
            cursor = self.base_store._event(
                connection,
                "node_recovery.episode_recorded",
                observed["task_id"],
                observed["node_id"],
                {
                    "episode_id": episode_id,
                    "node_attempt": observed["attempt"],
                    "failure_fingerprint": observed["failure_fingerprint"],
                    "category": planned["category"],
                    "state": planned["state"],
                    "action": planned["action"],
                    "source_event_cursor": observed["source_event_cursor"],
                },
                created_at=timestamp,
            )
            progress["event_cursor"] = cursor
            connection.execute(
                """
                INSERT INTO node_recovery_episodes(
                    episode_id, task_id, node_id, node_attempt, failure_fingerprint,
                    task_revision, policy_revision, policy_json, origin, category, phase,
                    observation_json, decision_json, evidence_refs_json, source_event_cursor,
                    state, action, owner, permission_required, action_attempts,
                    current_stage_key, stage_attempts_json, time_budget_deadline_at,
                    next_wakeup_at, last_material_progress_at,
                    last_progress_json, state_revision, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    episode_id,
                    observed["task_id"],
                    observed["node_id"],
                    observed["attempt"],
                    observed["failure_fingerprint"],
                    observed["task_revision"],
                    policy_revision,
                    canonical_json(policy.to_dict()),
                    observed["origin"],
                    planned["category"],
                    observed["phase"],
                    canonical_json(observed["document"]),
                    canonical_json(planned["document"]),
                    canonical_json(observed["evidence_refs"]),
                    observed["source_event_cursor"],
                    planned["state"],
                    planned["action"],
                    planned["owner"],
                    int(planned["requires_authorization"]),
                    planned["stage_key"],
                    canonical_json({}),
                    deadline,
                    planned["next_wakeup_at"],
                    timestamp,
                    canonical_json(progress),
                    timestamp,
                    timestamp,
                ),
            )
            self._record_needs_action_notification(
                connection,
                episode_id=episode_id,
                task_id=observed["task_id"],
                node_id=observed["node_id"],
                planned=planned,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM node_recovery_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            assert row is not None
            return self._episode_row(connection, row)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        """Read one episode, its action intents, and its optional repair link."""

        episode_id = _text(episode_id, "episode_id")
        with self.base_store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM node_recovery_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if row is None:
                raise KeyError(episode_id)
            return self._episode_row(connection, row)

    def list_due(
        self,
        *,
        now: str | None = None,
        limit: int = 100,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List due active recovery work without acquiring a lease."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("recovery due limit must be between 1 and 500")
        timestamp = _timestamp(now, "now") if now is not None else now_iso()
        assert timestamp is not None
        if task_id is not None:
            _text(task_id, "task_id")
        with self.base_store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM node_recovery_episodes
                WHERE state IN ('ready', 'waiting')
                  AND time_budget_deadline_at > ?
                  AND (next_wakeup_at IS NULL OR next_wakeup_at <= ?)
                  AND (? IS NULL OR task_id = ?)
                ORDER BY COALESCE(next_wakeup_at, created_at), episode_id
                LIMIT ?
                """,
                (timestamp, timestamp, task_id, task_id, limit),
            ).fetchall()
            return [self._episode_row(connection, row, include_actions=False) for row in rows]

    def list_enabled_tasks(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Page only explicitly enabled policies for the Coordinator sweep."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("recovery enabled-task limit must be between 1 and 100")
        if cursor is not None:
            cursor = _text(cursor, "cursor")
        with self.base_store.connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, policy_revision, configured_task_revision, actor, updated_at
                FROM node_recovery_policies
                WHERE json_extract(policy_json, '$.enabled') = 1
                  AND (? IS NULL OR task_id > ?)
                ORDER BY task_id LIMIT ?
                """,
                (cursor, cursor, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            return {
                "tasks": [
                    {
                        "task_id": str(row["task_id"]),
                        "policy_revision": int(row["policy_revision"]),
                        "configured_task_revision": int(row["configured_task_revision"]),
                        "actor": str(row["actor"]),
                        "updated_at": str(row["updated_at"]),
                    }
                    for row in visible
                ],
                "next_cursor": str(visible[-1]["task_id"]) if len(rows) > limit else None,
            }

    def claim_due(
        self,
        episode_id: str,
        owner_id: str,
        coordinator_epoch: int,
        *,
        expected_revision: int,
        expected_node_attempt: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        """Claim one due episode after all task, policy, and attempt fences hold."""

        episode_id = _text(episode_id, "episode_id")
        owner_id = _text(owner_id, "owner_id")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        _lease_seconds(lease_seconds)
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            self.base_store._assert_active_coordinator(connection, coordinator_epoch)
            row = self._episode_or_key_error(connection, episode_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            self._assert_episode_attempt(connection, row, expected_node_attempt)
            task = self._task_row(connection, str(row["task_id"]))
            node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
            if self._user_fence(task, node):
                self._suspend_for_user_fence(connection, row, timestamp)
                return None
            if int(task["state_revision"]) != int(row["task_revision"]):
                raise StateConflictError("recovery task revision changed")
            policy, policy_revision = self._policy_for_task(connection, str(row["task_id"]))
            if not policy.enabled or policy_revision != int(row["policy_revision"]):
                return None
            if row["state"] not in {"ready", "waiting"}:
                return None
            if _due(str(row["time_budget_deadline_at"]), timestamp):
                return self._exhaust_budget(connection, row, timestamp)
            if not _due(row["next_wakeup_at"], timestamp):
                return None
            if self._lease_is_live(row, timestamp):
                if (
                    row["owner_id"] == owner_id
                    and int(row["coordinator_epoch"]) == coordinator_epoch
                ):
                    return self._episode_row(connection, row)
                raise StateConflictError("recovery episode is leased by another owner")
            lease_epoch = self.base_store._next_lease_epoch(connection)
            revision = expected_revision + 1
            expires_at = _after(timestamp, lease_seconds)
            changed = connection.execute(
                """
                UPDATE node_recovery_episodes
                SET phase = 'claimed', state_revision = ?, owner_id = ?,
                    coordinator_epoch = ?, lease_epoch = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE episode_id = ? AND state_revision = ?
                """,
                (
                    revision, owner_id, coordinator_epoch, lease_epoch, expires_at,
                    timestamp, episode_id, expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("recovery episode claim compare-and-set failed")
            self.base_store._event(
                connection,
                "node_recovery.episode_claimed",
                str(row["task_id"]),
                str(row["node_id"]),
                {
                    "episode_id": episode_id,
                    "state_revision": revision,
                    "owner_id": owner_id,
                    "coordinator_epoch": coordinator_epoch,
                    "lease_epoch": lease_epoch,
                    "lease_expires_at": expires_at,
                },
                created_at=timestamp,
            )
            claimed = self._episode_or_key_error(connection, episode_id)
            return self._episode_row(connection, claimed)

    def renew(
        self,
        episode_id: str,
        owner_id: str,
        coordinator_epoch: int,
        *,
        lease_epoch: int,
        expected_revision: int,
        expected_node_attempt: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        """Renew a leased episode only while its current task/attempt remain legal."""

        episode_id = _text(episode_id, "episode_id")
        owner_id = _text(owner_id, "owner_id")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(lease_epoch, "lease_epoch")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        _lease_seconds(lease_seconds)
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            row = self._episode_or_key_error(connection, episode_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            self._assert_episode_attempt(connection, row, expected_node_attempt)
            task = self._task_row(connection, str(row["task_id"]))
            node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
            if self._user_fence(task, node):
                return self._suspend_for_user_fence(connection, row, timestamp)
            if int(task["state_revision"]) != int(row["task_revision"]):
                raise StateConflictError("recovery task revision changed")
            self._assert_episode_lease(
                connection, row, owner_id=owner_id, coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch, timestamp=timestamp,
            )
            policy, policy_revision = self._policy_for_task(connection, str(row["task_id"]))
            if not policy.enabled or policy_revision != int(row["policy_revision"]):
                raise StateConflictError("recovery policy changed while the episode was leased")
            expires_at = _after(timestamp, lease_seconds)
            revision = expected_revision + 1
            changed = connection.execute(
                """
                UPDATE node_recovery_episodes
                SET state_revision = ?, lease_expires_at = ?, updated_at = ?
                WHERE episode_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (revision, expires_at, timestamp, episode_id, expected_revision, lease_epoch),
            ).rowcount
            if changed != 1:
                raise StateConflictError("recovery episode renewal compare-and-set failed")
            self.base_store._event(
                connection,
                "node_recovery.episode_lease_renewed",
                str(row["task_id"]),
                str(row["node_id"]),
                {
                    "episode_id": episode_id,
                    "state_revision": revision,
                    "owner_id": owner_id,
                    "coordinator_epoch": coordinator_epoch,
                    "lease_epoch": lease_epoch,
                    "lease_expires_at": expires_at,
                },
                created_at=timestamp,
            )
            renewed = self._episode_or_key_error(connection, episode_id)
            return self._episode_row(connection, renewed)

    def begin_action(
        self,
        episode_id: str,
        action_key: str,
        action_fingerprint: str,
        action_input: Mapping[str, object],
        *,
        owner_id: str,
        coordinator_epoch: int,
        lease_epoch: int,
        expected_revision: int,
        expected_node_attempt: int,
    ) -> dict[str, Any]:
        """Persist an immutable action intent before an adapter may act.

        ``action_key`` is the Authority request ID.  An existing intent is
        returned with ``existing=True`` and never authorizes a second effect.
        """

        episode_id = _text(episode_id, "episode_id")
        action_key = _text(action_key, "action_key")
        action_fingerprint = _fingerprint(action_fingerprint, "action_fingerprint")
        input_document = _bounded_document(action_input, "action_input")
        stage_key = _text(input_document.get("stage_key"), "action_input.stage_key")
        owner_id = _text(owner_id, "owner_id")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(lease_epoch, "lease_epoch")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            row = self._episode_or_key_error(connection, episode_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            self._assert_episode_attempt(connection, row, expected_node_attempt)
            task = self._task_row(connection, str(row["task_id"]))
            node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
            if self._user_fence(task, node):
                self._suspend_for_user_fence(connection, row, timestamp)
                raise StateConflictError("user pause or cancellation prevents recovery action")
            if int(task["state_revision"]) != int(row["task_revision"]):
                raise StateConflictError("recovery task revision changed")
            self._assert_episode_lease(
                connection, row, owner_id=owner_id, coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch, timestamp=timestamp,
            )
            policy, policy_revision = self._policy_for_task(connection, str(row["task_id"]))
            if not policy.enabled or policy_revision != int(row["policy_revision"]):
                raise StateConflictError("recovery policy changed while the episode was leased")
            action = row["action"]
            if action is None or action not in policy.allowed_actions:
                raise StateConflictError("recovery episode has no currently authorized action")
            if stage_key != row["current_stage_key"]:
                raise StateConflictError("recovery action stage does not match the planned stage")
            stage_attempts = _stage_attempts(row["stage_attempts_json"])
            if stage_attempts.get(stage_key, 0) >= policy.max_action_attempts or _due(
                str(row["time_budget_deadline_at"]), timestamp
            ):
                self._exhaust_budget(connection, row, timestamp)
                raise StateConflictError("recovery action budget is exhausted")
            existing = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_key = ?", (action_key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["episode_id"]) != episode_id
                    or str(existing["action_fingerprint"]) != action_fingerprint
                    or json.loads(str(existing["action_input_json"])) != input_document
                ):
                    raise StateConflictError("recovery action key conflicts with existing intent")
                return {
                    "action": self._action_row(existing),
                    "episode": self._episode_row(connection, row),
                    "existing": True,
                }
            matching_fingerprint = connection.execute(
                """
                SELECT * FROM node_recovery_actions
                WHERE episode_id = ? AND action_fingerprint = ?
                """,
                (episode_id, action_fingerprint),
            ).fetchone()
            if matching_fingerprint is not None:
                if (
                    str(matching_fingerprint["stage_key"]) != stage_key
                    or json.loads(str(matching_fingerprint["action_input_json"])) != input_document
                ):
                    raise StateConflictError("recovery action fingerprint conflicts with existing intent")
                return {
                    "action": self._action_row(matching_fingerprint),
                    "episode": self._episode_row(connection, row),
                    "existing": True,
                }
            action_id = "node-recovery-action-" + canonical_hash(
                {
                    "episode_id": episode_id,
                    "action_key": action_key,
                    "action_fingerprint": action_fingerprint,
                }
            )[:24]
            connection.execute(
                """
                INSERT INTO node_recovery_actions(
                    action_id, episode_id, task_id, node_id, node_attempt, action_key,
                    stage_key, intent_coordinator_epoch, intent_lease_epoch,
                    action_fingerprint, action_input_json, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'executing', ?, ?)
                """,
                (
                    action_id, episode_id, row["task_id"], row["node_id"], row["node_attempt"],
                    action_key, stage_key, coordinator_epoch, lease_epoch,
                    action_fingerprint, canonical_json(input_document), timestamp, timestamp,
                ),
            )
            revision = expected_revision + 1
            cursor = self.base_store._event(
                connection,
                "node_recovery.action_intended",
                str(row["task_id"]),
                str(row["node_id"]),
                {
                    "episode_id": episode_id,
                    "action_id": action_id,
                    "action_key": action_key,
                    "action": action,
                    "stage_key": stage_key,
                    "action_fingerprint": action_fingerprint,
                    "state_revision": revision,
                },
                created_at=timestamp,
            )
            changed = connection.execute(
                """
                UPDATE node_recovery_episodes
                SET phase = 'action_intent', state_revision = ?,
                    last_material_progress_at = ?, last_progress_json = ?, updated_at = ?
                WHERE episode_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (
                    revision,
                    timestamp,
                    canonical_json(
                        {
                            "kind": "node_recovery.action_intended",
                            "event_cursor": cursor,
                            "action_id": action_id,
                            "action_key": action_key,
                            "action": action,
                            "stage_key": stage_key,
                        }
                    ),
                    timestamp,
                    episode_id,
                    expected_revision,
                    lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("recovery action intent compare-and-set failed")
            stored = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            episode = self._episode_or_key_error(connection, episode_id)
            assert stored is not None
            return {
                "action": self._action_row(stored),
                "episode": self._episode_row(connection, episode),
                "existing": False,
            }

    def settle_action(
        self,
        episode_id: str,
        action_key: str,
        *,
        owner_id: str,
        coordinator_epoch: int,
        lease_epoch: int,
        expected_revision: int,
        expected_node_attempt: int,
        receipt: Mapping[str, object],
        next_decision: Mapping[str, object],
        receipt_state: str = "completed",
        effect_dispatched: bool = False,
        material_progress: bool = True,
    ) -> dict[str, Any]:
        """Record a completed or unknown action receipt and release its lease.

        Settlement intentionally does not compare the *current* node attempt:
        a legal recovery action may have advanced that attempt before it could
        persist its receipt.  The immutable episode attempt and lease fences
        still have to match.
        """

        episode_id = _text(episode_id, "episode_id")
        action_key = _text(action_key, "action_key")
        owner_id = _text(owner_id, "owner_id")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(lease_epoch, "lease_epoch")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        if receipt_state not in {"completed", "unknown"}:
            raise ValueError("recovery receipt_state must be completed or unknown")
        if not isinstance(effect_dispatched, bool) or not isinstance(material_progress, bool):
            raise ValueError("recovery effect_dispatched and material_progress must be booleans")
        receipt_document = _bounded_document(receipt, "action receipt")
        planned = _decision(next_decision)
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            row = self._episode_or_key_error(connection, episode_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            if int(row["node_attempt"]) != expected_node_attempt:
                raise StateConflictError("recovery episode attempt is stale")
            self._assert_episode_lease(
                connection, row, owner_id=owner_id, coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch, timestamp=timestamp,
            )
            action = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_key = ?", (action_key,)
            ).fetchone()
            if action is None or str(action["episode_id"]) != episode_id:
                raise StateConflictError("recovery receipt does not match this episode intent")
            if action["state"] in {"completed", "unknown"}:
                if (
                    str(action["state"]) == receipt_state
                    and json.loads(str(action["receipt_json"])) == receipt_document
                    and bool(action["effect_dispatched"]) == effect_dispatched
                ):
                    return {
                        "action": self._action_row(action),
                        "episode": self._episode_row(connection, row),
                        "idempotent": True,
                    }
                raise StateConflictError("recovery action receipt conflicts with prior settlement")
            if action["state"] != "executing":
                raise StateConflictError("recovery action is not executing")
            try:
                stored_observation = json.loads(str(row["observation_json"]))
            except json.JSONDecodeError as error:
                raise StateConflictError("recovery episode observation is invalid") from error
            raw_patch = receipt_document.get("observation_patch")
            if raw_patch is None:
                merged_observation = _bounded_document(stored_observation, "stored observation")
                observation_patch_applied = False
            else:
                if not isinstance(raw_patch, Mapping):
                    raise ValueError("action receipt observation_patch must be an object")
                merged_observation = _merge_observation(
                    stored_observation, raw_patch, allow_evidence_reset=True
                )
                observation_patch_applied = merged_observation != stored_observation
            merged_evidence_refs = _evidence_refs(merged_observation.get("evidence_refs"))
            merged_source_cursor = _nonnegative_int(
                merged_observation.get("source_event_cursor", row["source_event_cursor"]),
                "merged source_event_cursor",
            )
            planned = _decision(
                next_decision,
                fallback_stage_key=(
                    str(row["current_stage_key"])
                    if row["current_stage_key"] is not None
                    else None
                ),
            )
            task = self._task_row(connection, str(row["task_id"]))
            node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
            if self._user_fence(task, node):
                planned = _suspended_decision("user_pause")
            elif receipt_state == "unknown":
                planned = _unknown_effects_decision()
            policy = RecoveryPolicy.from_dict(json.loads(str(row["policy_json"])))
            stage_attempts = _stage_attempts(row["stage_attempts_json"])
            action_stage_key = str(action["stage_key"])
            if effect_dispatched:
                stage_attempts[action_stage_key] = stage_attempts.get(action_stage_key, 0) + 1
            next_stage_key = planned["stage_key"]
            if (
                next_stage_key is not None
                and next_stage_key != action_stage_key
                and not (
                    effect_dispatched
                    and receipt_state == "completed"
                    and receipt_document.get("stage_succeeded") is True
                )
            ):
                raise StateConflictError("recovery stage may advance only after a successful dispatched action")
            if next_stage_key == action_stage_key and stage_attempts.get(action_stage_key, 0) >= policy.max_action_attempts:
                planned = _budget_exhausted_decision()
                next_stage_key = action_stage_key
            if next_stage_key is None:
                next_stage_key = action_stage_key
            current_attempts = stage_attempts.get(next_stage_key, 0)
            revision = expected_revision + 1
            phase = "action_unknown" if receipt_state == "unknown" else "action_settled"
            cursor = self.base_store._event(
                connection,
                "node_recovery.action_settled",
                str(row["task_id"]),
                str(row["node_id"]),
                {
                    "episode_id": episode_id,
                    "action_id": action["action_id"],
                    "action_key": action_key,
                    "stage_key": action_stage_key,
                    "receipt_state": receipt_state,
                    "effect_dispatched": effect_dispatched,
                    "observation_patch_keys": sorted(raw_patch) if raw_patch is not None else [],
                    "state_revision": revision,
                    "next_state": planned["state"],
                    "next_action": planned["action"],
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE node_recovery_actions
                SET state = ?, receipt_json = ?, effect_dispatched = ?,
                    updated_at = ?, settled_at = ?
                WHERE action_id = ? AND state = 'executing'
                """,
                (
                    receipt_state, canonical_json(receipt_document), int(effect_dispatched),
                    timestamp, timestamp, action["action_id"],
                ),
            )
            progress = {
                "kind": "node_recovery.action_settled",
                "event_cursor": cursor,
                "action_id": action["action_id"],
                "action_key": action_key,
                "receipt_state": receipt_state,
                "observation_patch_applied": observation_patch_applied,
            }
            changed = connection.execute(
                """
                UPDATE node_recovery_episodes
                SET state = ?, action = ?, owner = ?, permission_required = ?, phase = ?,
                    action_attempts = ?, state_revision = ?, owner_id = NULL,
                    coordinator_epoch = 0, lease_epoch = 0, lease_expires_at = NULL,
                    current_stage_key = ?, stage_attempts_json = ?, next_wakeup_at = ?,
                    observation_json = ?, evidence_refs_json = ?, source_event_cursor = ?,
                    last_material_progress_at = ?,
                    last_progress_json = ?, decision_json = ?, updated_at = ?
                WHERE episode_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (
                    planned["state"], planned["action"], planned["owner"],
                    int(planned["requires_authorization"]), phase, current_attempts, revision,
                    next_stage_key, canonical_json(stage_attempts),
                    planned["next_wakeup_at"],
                    canonical_json(merged_observation), canonical_json(merged_evidence_refs), merged_source_cursor,
                    timestamp if material_progress or observation_patch_applied else row["last_material_progress_at"],
                    canonical_json(progress), canonical_json(planned["document"]), timestamp,
                    episode_id, expected_revision, lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("recovery action settlement compare-and-set failed")
            self._record_needs_action_notification(
                connection,
                episode_id=episode_id,
                task_id=str(row["task_id"]),
                node_id=str(row["node_id"]),
                planned=planned,
                timestamp=timestamp,
            )
            stored_action = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_id = ?", (action["action_id"],)
            ).fetchone()
            episode = self._episode_or_key_error(connection, episode_id)
            assert stored_action is not None
            return {
                "action": self._action_row(stored_action),
                "episode": self._episode_row(connection, episode),
                "idempotent": False,
            }

    def recover_interrupted(self) -> int:
        """Mark pre-restart executing intents unknown without replaying effects."""

        timestamp = now_iso()
        recovered = 0
        with self.base_store.transaction() as connection:
            actions = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE state = 'executing' ORDER BY created_at, action_id"
            ).fetchall()
            for action in actions:
                episode = self._episode_or_key_error(connection, str(action["episode_id"]))
                receipt = {
                    "kind": "recovery_interrupted",
                    "detail": "intent survived a restart without a durable receipt; effect is unknown",
                }
                connection.execute(
                    """
                    UPDATE node_recovery_actions
                    SET state = 'unknown', receipt_json = ?, updated_at = ?, settled_at = ?
                    WHERE action_id = ? AND state = 'executing'
                    """,
                    (canonical_json(receipt), timestamp, timestamp, action["action_id"]),
                )
                planned = _unknown_effects_decision()
                revision = int(episode["state_revision"]) + 1
                cursor = self.base_store._event(
                    connection,
                    "node_recovery.action_interrupted",
                    str(episode["task_id"]),
                    str(episode["node_id"]),
                    {
                        "episode_id": episode["episode_id"],
                        "action_id": action["action_id"],
                        "action_key": action["action_key"],
                        "state_revision": revision,
                    },
                    created_at=timestamp,
                )
                connection.execute(
                    """
                    UPDATE node_recovery_episodes
                    SET state = ?, action = NULL, owner = ?, permission_required = ?,
                        phase = 'interrupted_unknown', state_revision = ?, owner_id = NULL,
                        coordinator_epoch = 0, lease_epoch = 0, lease_expires_at = NULL,
                        next_wakeup_at = NULL, decision_json = ?,
                        last_material_progress_at = ?, last_progress_json = ?, updated_at = ?
                    WHERE episode_id = ? AND state_revision = ?
                    """,
                    (
                        planned["state"], planned["owner"], int(planned["requires_authorization"]),
                        revision, canonical_json(planned["document"]), timestamp,
                        canonical_json(
                            {
                                "kind": "node_recovery.action_interrupted",
                                "event_cursor": cursor,
                                "action_id": action["action_id"],
                            }
                        ),
                        timestamp, episode["episode_id"], episode["state_revision"],
                    ),
                )
                current = self._episode_or_key_error(connection, str(episode["episode_id"]))
                self._record_needs_action_notification(
                    connection,
                    episode_id=str(episode["episode_id"]),
                    task_id=str(episode["task_id"]),
                    node_id=str(episode["node_id"]),
                    planned=planned,
                    timestamp=timestamp,
                )
                del current
                recovered += 1
        return recovered

    def reconcile_action_receipt(
        self,
        episode_id: str,
        action_key: str,
        action_fingerprint: str,
        *,
        coordinator_epoch: int,
        expected_intent_coordinator_epoch: int,
        expected_intent_lease_epoch: int,
        expected_revision: int,
        expected_node_attempt: int,
        receipt: Mapping[str, object],
        next_decision: Mapping[str, object],
        effect_dispatched: bool,
        material_progress: bool = True,
    ) -> dict[str, Any]:
        """Settle a journal-verified receipt for an interrupted unknown intent.

        This method never invokes an adapter.  It only turns the same durable
        intent into a completed receipt after the current Authority has found
        a matching receipt in its existing idempotency journal.
        """

        episode_id = _text(episode_id, "episode_id")
        action_key = _text(action_key, "action_key")
        action_fingerprint = _fingerprint(action_fingerprint, "action_fingerprint")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(expected_intent_coordinator_epoch, "expected_intent_coordinator_epoch")
        _positive_int(expected_intent_lease_epoch, "expected_intent_lease_epoch")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        if not isinstance(effect_dispatched, bool) or not isinstance(material_progress, bool):
            raise ValueError("recovery effect_dispatched and material_progress must be booleans")
        receipt_document = _bounded_document(receipt, "reconciled action receipt")
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            self.base_store._assert_active_coordinator(connection, coordinator_epoch)
            row = self._episode_or_key_error(connection, episode_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            if int(row["node_attempt"]) != expected_node_attempt:
                raise StateConflictError("recovery episode attempt is stale")
            action = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_key = ?", (action_key,)
            ).fetchone()
            if action is None or str(action["episode_id"]) != episode_id:
                raise StateConflictError("reconciled receipt does not match this episode intent")
            if (
                str(action["action_fingerprint"]) != action_fingerprint
                or int(action["intent_coordinator_epoch"]) != expected_intent_coordinator_epoch
                or int(action["intent_lease_epoch"]) != expected_intent_lease_epoch
            ):
                raise StateConflictError("reconciled receipt does not match the original fenced intent")
            if action["state"] == "completed":
                if (
                    json.loads(str(action["receipt_json"])) == receipt_document
                    and bool(action["effect_dispatched"]) == effect_dispatched
                ):
                    return {
                        "action": self._action_row(action),
                        "episode": self._episode_row(connection, row),
                        "idempotent": True,
                    }
                raise StateConflictError("reconciled receipt conflicts with prior completion")
            if action["state"] != "unknown":
                raise StateConflictError("only an interrupted unknown action may be reconciled")
            try:
                stored_observation = json.loads(str(row["observation_json"]))
            except json.JSONDecodeError as error:
                raise StateConflictError("recovery episode observation is invalid") from error
            raw_patch = receipt_document.get("observation_patch")
            if raw_patch is None:
                merged_observation = _bounded_document(stored_observation, "stored observation")
                observation_patch_applied = False
            else:
                if not isinstance(raw_patch, Mapping):
                    raise ValueError("reconciled receipt observation_patch must be an object")
                merged_observation = _merge_observation(
                    stored_observation, raw_patch, allow_evidence_reset=True
                )
                observation_patch_applied = merged_observation != stored_observation
            merged_refs = _evidence_refs(merged_observation.get("evidence_refs"))
            merged_cursor = _nonnegative_int(
                merged_observation.get("source_event_cursor", row["source_event_cursor"]),
                "merged source_event_cursor",
            )
            planned = _decision(
                next_decision,
                fallback_stage_key=(
                    str(row["current_stage_key"])
                    if row["current_stage_key"] is not None
                    else None
                ),
            )
            task = self._task_row(connection, str(row["task_id"]))
            node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
            if self._user_fence(task, node):
                planned = _suspended_decision("user_pause")
            policy = RecoveryPolicy.from_dict(json.loads(str(row["policy_json"])))
            stage_attempts = _stage_attempts(row["stage_attempts_json"])
            action_stage_key = str(action["stage_key"])
            if effect_dispatched:
                stage_attempts[action_stage_key] = stage_attempts.get(action_stage_key, 0) + 1
            next_stage_key = planned["stage_key"]
            if (
                next_stage_key is not None
                and next_stage_key != action_stage_key
                and not (effect_dispatched and receipt_document.get("stage_succeeded") is True)
            ):
                raise StateConflictError("recovery stage may advance only after a successful reconciled action")
            if next_stage_key == action_stage_key and stage_attempts.get(action_stage_key, 0) >= policy.max_action_attempts:
                planned = _budget_exhausted_decision()
                next_stage_key = action_stage_key
            if next_stage_key is None:
                next_stage_key = action_stage_key
            current_attempts = stage_attempts.get(next_stage_key, 0)
            revision = expected_revision + 1
            cursor = self.base_store._event(
                connection,
                "node_recovery.action_reconciled",
                str(row["task_id"]),
                str(row["node_id"]),
                {
                    "episode_id": episode_id,
                    "action_id": action["action_id"],
                    "action_key": action_key,
                    "previous_state": "unknown",
                    "intent_coordinator_epoch": expected_intent_coordinator_epoch,
                    "intent_lease_epoch": expected_intent_lease_epoch,
                    "observation_patch_keys": sorted(raw_patch) if raw_patch is not None else [],
                    "state_revision": revision,
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE node_recovery_actions
                SET state = 'completed', receipt_json = ?, effect_dispatched = ?,
                    updated_at = ?, settled_at = ?
                WHERE action_id = ? AND state = 'unknown'
                """,
                (canonical_json(receipt_document), int(effect_dispatched), timestamp, timestamp, action["action_id"]),
            )
            connection.execute(
                """
                UPDATE node_recovery_episodes
                SET state = ?, action = ?, owner = ?, permission_required = ?,
                    phase = 'action_reconciled', action_attempts = ?, current_stage_key = ?,
                    stage_attempts_json = ?, state_revision = ?, owner_id = NULL,
                    coordinator_epoch = 0, lease_epoch = 0, lease_expires_at = NULL,
                    next_wakeup_at = ?, observation_json = ?, evidence_refs_json = ?,
                    source_event_cursor = ?, last_material_progress_at = ?,
                    last_progress_json = ?, decision_json = ?, updated_at = ?
                WHERE episode_id = ? AND state_revision = ?
                """,
                (
                    planned["state"], planned["action"], planned["owner"],
                    int(planned["requires_authorization"]), current_attempts, next_stage_key,
                    canonical_json(stage_attempts), revision, planned["next_wakeup_at"],
                    canonical_json(merged_observation), canonical_json(merged_refs), merged_cursor,
                    timestamp if material_progress or observation_patch_applied else row["last_material_progress_at"],
                    canonical_json(
                        {
                            "kind": "node_recovery.action_reconciled",
                            "event_cursor": cursor,
                            "action_id": action["action_id"],
                            "previous_state": "unknown",
                        }
                    ),
                    canonical_json(planned["document"]), timestamp, episode_id, expected_revision,
                ),
            )
            self._record_needs_action_notification(
                connection,
                episode_id=episode_id,
                task_id=str(row["task_id"]),
                node_id=str(row["node_id"]),
                planned=planned,
                timestamp=timestamp,
            )
            stored_action = connection.execute(
                "SELECT * FROM node_recovery_actions WHERE action_id = ?", (action["action_id"],)
            ).fetchone()
            episode = self._episode_or_key_error(connection, episode_id)
            assert stored_action is not None
            return {
                "action": self._action_row(stored_action),
                "episode": self._episode_row(connection, episode),
                "idempotent": False,
            }

    def link_repair(
        self,
        episode_id: str,
        *,
        owner_id: str,
        coordinator_epoch: int,
        lease_epoch: int,
        expected_revision: int,
        repair_request_id: str,
        repair_task_id: str,
        repair_fingerprint: str,
    ) -> dict[str, Any]:
        """Attach at most one repair request/task to one failure fingerprint."""

        episode_id = _text(episode_id, "episode_id")
        owner_id = _text(owner_id, "owner_id")
        repair_request_id = _text(repair_request_id, "repair_request_id")
        repair_task_id = _text(repair_task_id, "repair_task_id")
        repair_fingerprint = _fingerprint(repair_fingerprint, "repair_fingerprint")
        _positive_int(coordinator_epoch, "coordinator_epoch")
        _positive_int(lease_epoch, "lease_epoch")
        _positive_int(expected_revision, "expected_revision")
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            episode = self._episode_or_key_error(connection, episode_id)
            if int(episode["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            self._assert_episode_lease(
                connection, episode, owner_id=owner_id, coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch, timestamp=timestamp,
            )
            existing = connection.execute(
                """
                SELECT * FROM node_recovery_repairs
                WHERE task_id = ? AND failure_fingerprint = ?
                """,
                (episode["task_id"], episode["failure_fingerprint"]),
            ).fetchone()
            if existing is not None:
                if (
                    existing["episode_id"] != episode_id
                    or existing["repair_request_id"] != repair_request_id
                    or existing["repair_task_id"] != repair_task_id
                    or existing["repair_fingerprint"] != repair_fingerprint
                ):
                    raise StateConflictError("failure fingerprint already has a different repair linkage")
                return self._episode_row(connection, episode)
            link_id = "node-recovery-repair-" + canonical_hash(
                {"task_id": episode["task_id"], "failure_fingerprint": episode["failure_fingerprint"]}
            )[:24]
            connection.execute(
                """
                INSERT INTO node_recovery_repairs(
                    repair_link_id, task_id, failure_fingerprint, episode_id,
                    repair_request_id, repair_task_id, repair_fingerprint,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id, episode["task_id"], episode["failure_fingerprint"], episode_id,
                    repair_request_id, repair_task_id, repair_fingerprint, timestamp, timestamp,
                ),
            )
            revision = expected_revision + 1
            cursor = self.base_store._event(
                connection,
                "node_recovery.repair_linked",
                str(episode["task_id"]),
                str(episode["node_id"]),
                {
                    "episode_id": episode_id,
                    "repair_request_id": repair_request_id,
                    "repair_task_id": repair_task_id,
                    "repair_fingerprint": repair_fingerprint,
                    "state_revision": revision,
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE node_recovery_episodes
                SET phase = 'repair_linked', repair_request_id = ?, repair_task_id = ?,
                    repair_deployment_fingerprint = ?, state_revision = ?,
                    last_material_progress_at = ?, last_progress_json = ?, updated_at = ?
                WHERE episode_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (
                    repair_request_id, repair_task_id, None, revision,
                    timestamp,
                    canonical_json(
                        {
                            "kind": "node_recovery.repair_linked",
                            "event_cursor": cursor,
                            "repair_request_id": repair_request_id,
                        }
                    ),
                    timestamp, episode_id, expected_revision, lease_epoch,
                ),
            )
            current = self._episode_or_key_error(connection, episode_id)
            return self._episode_row(connection, current)

    def mark_repair_deployed(
        self,
        episode_id: str,
        *,
        expected_revision: int,
        expected_node_attempt: int,
        verified_deployment_fingerprint: str,
        expected_repair_fingerprint: str,
        evidence_refs: Mapping[str, object] | list[object] | tuple[object, ...],
        verified_by: str,
        fresh_readiness_verified: bool = False,
    ) -> dict[str, Any]:
        """Persist a root-verified deployment identity; this does not resume a node."""

        episode_id = _text(episode_id, "episode_id")
        _positive_int(expected_revision, "expected_revision")
        _nonnegative_int(expected_node_attempt, "expected_node_attempt")
        verified = _fingerprint(verified_deployment_fingerprint, "verified_deployment_fingerprint")
        expected_repair = _fingerprint(expected_repair_fingerprint, "expected_repair_fingerprint")
        refs = _evidence_refs(evidence_refs)
        verified_by = _text(verified_by, "verified_by")
        if not isinstance(fresh_readiness_verified, bool):
            raise ValueError("fresh_readiness_verified must be a boolean")
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            episode = self._episode_or_key_error(connection, episode_id)
            if int(episode["state_revision"]) != expected_revision:
                raise StateConflictError("recovery episode revision is stale")
            if int(episode["node_attempt"]) != expected_node_attempt:
                raise StateConflictError("recovery episode attempt is stale")
            repair = connection.execute(
                "SELECT * FROM node_recovery_repairs WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if repair is None:
                raise StateConflictError("recovery episode has no repair linkage")
            if str(repair["repair_fingerprint"]) != expected_repair:
                raise StateConflictError("repair fingerprint does not match the parent linkage")
            if repair["deployed_at"] is not None:
                if (json.loads(str(repair["deployment_evidence_refs_json"])) == refs
                        and repair["verified_deployment_fingerprint"] == verified):
                    return self._episode_row(connection, episode)
                raise StateConflictError("repair deployment evidence conflicts with prior verification")
            task = self._task_row(connection, str(episode["task_id"]))
            node = self._node_row(connection, str(episode["task_id"]), str(episode["node_id"]))
            paused = self._user_fence(task, node)
            revision = expected_revision + 1
            cursor = self.base_store._event(
                connection,
                "node_recovery.repair_deployed",
                str(episode["task_id"]),
                str(episode["node_id"]),
                {
                    "episode_id": episode_id,
                    "repair_request_id": repair["repair_request_id"],
                    "repair_task_id": repair["repair_task_id"],
                    "deployment_fingerprint": verified,
                    "verified_by": verified_by,
                    "state_revision": revision,
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE node_recovery_repairs
                SET deployment_evidence_refs_json = ?, verified_deployment_fingerprint = ?, deployed_at = ?, updated_at = ?
                WHERE repair_link_id = ? AND deployed_at IS NULL
                """,
                (canonical_json(refs), verified, timestamp, timestamp, repair["repair_link_id"]),
            )
            if paused:
                planned = _suspended_decision("user_pause")
            else:
                policy = RecoveryPolicy.from_dict(json.loads(str(episode["policy_json"])))
                planned = _repair_deployed_wait_decision(policy.backoff_seconds, timestamp)
            observed = json.loads(str(episode["observation_json"]))
            for key in ("validated_source_delta", "validated_install_manifest", "validated_runtime_fingerprint", "validation_audit_ref",
                        "last_validation_profile", "repair_wait_kind", "repair_wait_state"):
                observed.pop(key, None)
            observed.update({
                "repair_deployed": True, "repair_fresh_readiness_required": False,
                "readiness_ready": fresh_readiness_verified,
                "validated_profiles": [], "validation_succeeded": False,
            })
            connection.execute(
                """
                UPDATE node_recovery_episodes
                SET phase = 'repair_deployed', state = ?, action = ?, owner = ?,
                    permission_required = ?, state_revision = ?, next_wakeup_at = ?,
                    last_material_progress_at = ?, last_progress_json = ?, decision_json = ?, updated_at = ?,
                    repair_deployment_fingerprint = ?, observation_json = ?
                WHERE episode_id = ? AND state_revision = ?
                """,
                (
                    planned["state"], planned["action"], planned["owner"],
                    int(planned["requires_authorization"]), revision, planned["next_wakeup_at"],
                    timestamp,
                    canonical_json(
                        {
                            "kind": "node_recovery.repair_deployed",
                            "event_cursor": cursor,
                            "deployment_fingerprint": verified,
                        }
                    ),
                    canonical_json(planned["document"]), timestamp, verified, canonical_json(observed),
                    episode_id, expected_revision,
                ),
            )
            current = self._episode_or_key_error(connection, episode_id)
            return self._episode_row(connection, current)

    def read_cursor(self) -> int:
        """Read the append-only event cursor consumed by this projection."""

        with self.base_store.connection() as connection:
            row = connection.execute(
                "SELECT event_cursor FROM node_recovery_cursors WHERE projection = ?",
                (_PROJECTION_CURSOR,),
            ).fetchone()
            return 0 if row is None else int(row["event_cursor"])

    def advance_cursor(self, expected_cursor: int, event_cursor: int) -> int:
        """CAS advance the projection cursor without emitting a noisy event."""

        _nonnegative_int(expected_cursor, "expected_cursor")
        _nonnegative_int(event_cursor, "event_cursor")
        if event_cursor < expected_cursor:
            raise ValueError("event_cursor must not move backwards")
        timestamp = now_iso()
        with self.base_store.transaction() as connection:
            row = connection.execute(
                "SELECT event_cursor FROM node_recovery_cursors WHERE projection = ?",
                (_PROJECTION_CURSOR,),
            ).fetchone()
            current = 0 if row is None else int(row["event_cursor"])
            if current != expected_cursor:
                raise StateConflictError(
                    f"expected recovery cursor {expected_cursor}, found {current}"
                )
            if event_cursor == current:
                return current
            if row is None:
                connection.execute(
                    """
                    INSERT INTO node_recovery_cursors(projection, event_cursor, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    (_PROJECTION_CURSOR, event_cursor, timestamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE node_recovery_cursors SET event_cursor = ?, updated_at = ?
                    WHERE projection = ? AND event_cursor = ?
                    """,
                    (event_cursor, timestamp, _PROJECTION_CURSOR, expected_cursor),
                )
            return event_cursor

    def list_summary(
        self,
        *,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return compact episode state for status views without raw evidence bodies."""

        if task_id is not None:
            _text(task_id, "task_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("recovery summary limit must be between 1 and 500")
        with self.base_store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM node_recovery_episodes
                WHERE (? IS NULL OR task_id = ?)
                ORDER BY updated_at DESC, episode_id DESC LIMIT ?
                """,
                (task_id, task_id, limit),
            ).fetchall()
            return [self._episode_summary(row) for row in rows]

    def metrics(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Aggregate durable counts without interpreting model or process activity as progress."""

        if task_id is not None:
            _text(task_id, "task_id")
        with self.base_store.connection() as connection:
            states = connection.execute(
                """
                SELECT state, COUNT(*) AS count FROM node_recovery_episodes
                WHERE (? IS NULL OR task_id = ?) GROUP BY state
                """,
                (task_id, task_id),
            ).fetchall()
            actions = connection.execute(
                """
                SELECT
                  COUNT(*) AS total_action_count,
                  COALESCE(SUM(effect_dispatched), 0) AS dispatched_action_count,
                  COALESCE(SUM(CASE WHEN state = 'executing' THEN 1 ELSE 0 END), 0) AS executing_action_count,
                  COALESCE(SUM(CASE WHEN state = 'unknown' THEN 1 ELSE 0 END), 0) AS unknown_action_count
                FROM node_recovery_actions
                WHERE (? IS NULL OR task_id = ?)
                """,
                (task_id, task_id),
            ).fetchone()
            assert actions is not None
            notices = connection.execute(
                """SELECT COUNT(*) AS count FROM node_recovery_notifications n
                   JOIN node_recovery_episodes e USING(episode_id)
                   WHERE (? IS NULL OR e.task_id = ?)""", (task_id, task_id),
            ).fetchone()
            return {
                "task_id": task_id,
                "episodes_by_state": {str(row["state"]): int(row["count"]) for row in states},
                "total_action_count": int(actions["total_action_count"]),
                "dispatched_action_count": int(actions["dispatched_action_count"]),
                "executing_action_count": int(actions["executing_action_count"]),
                "unknown_action_count": int(actions["unknown_action_count"]),
                "human_actions_required": int(notices["count"]),
                "model_wakes": None,
                "model_wakes_status": "not measured by the recovery projection; dispatch is not a provider call",
                "event_cursor": self._read_cursor_connection(connection),
            }

    @staticmethod
    def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT task_id, state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _node_row(connection: sqlite3.Connection, task_id: str, node_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT task_id, node_id, state, attempt FROM nodes WHERE task_id = ? AND node_id = ?",
            (task_id, node_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"{task_id}/{node_id}")
        return row

    @staticmethod
    def _episode_or_key_error(connection: sqlite3.Connection, episode_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM node_recovery_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return row

    def _policy_for_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
    ) -> tuple[RecoveryPolicy, int]:
        row = connection.execute(
            "SELECT * FROM node_recovery_policies WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return RecoveryPolicy(), 0
        try:
            return RecoveryPolicy.from_dict(json.loads(str(row["policy_json"]))), int(row["policy_revision"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateConflictError("recovery policy row is invalid") from error

    @staticmethod
    def _policy_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            policy = RecoveryPolicy.from_dict(json.loads(str(row["policy_json"]))).to_dict()
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateConflictError("recovery policy row is invalid") from error
        return {
            "task_id": str(row["task_id"]),
            "policy": policy,
            "policy_revision": int(row["policy_revision"]),
            "configured_task_revision": int(row["configured_task_revision"]),
            "actor": str(row["actor"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _episode_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_actions: bool = True,
    ) -> dict[str, Any]:
        try:
            observation = json.loads(str(row["observation_json"]))
            decision = json.loads(str(row["decision_json"]))
            evidence_refs = json.loads(str(row["evidence_refs_json"]))
            policy = RecoveryPolicy.from_dict(json.loads(str(row["policy_json"]))).to_dict()
            progress = json.loads(str(row["last_progress_json"]))
            stage_attempts = _stage_attempts(row["stage_attempts_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateConflictError("recovery episode row is invalid") from error
        result: dict[str, Any] = {
            "episode_id": str(row["episode_id"]),
            "task_id": str(row["task_id"]),
            "node_id": str(row["node_id"]),
            "node_attempt": int(row["node_attempt"]),
            "failure_fingerprint": str(row["failure_fingerprint"]),
            "task_revision": int(row["task_revision"]),
            "policy_revision": int(row["policy_revision"]),
            "policy": policy,
            "origin": str(row["origin"]),
            "category": str(row["category"]),
            "phase": str(row["phase"]),
            "observation": observation,
            "decision": decision,
            "evidence_refs": evidence_refs,
            "source_event_cursor": int(row["source_event_cursor"]),
            "revision": int(row["state_revision"]),
            "state_revision": int(row["state_revision"]),
            "state": str(row["state"]),
            "action": row["action"],
            "owner": str(row["owner"]),
            "permission_required": bool(row["permission_required"]),
            "action_attempts": int(row["action_attempts"]),
            "current_stage_key": row["current_stage_key"],
            "stage_attempts": stage_attempts,
            "time_budget_deadline_at": str(row["time_budget_deadline_at"]),
            "next_wakeup_at": row["next_wakeup_at"],
            "last_material_progress_at": str(row["last_material_progress_at"]),
            "last_progress": progress,
            "owner_id": row["owner_id"],
            "coordinator_epoch": int(row["coordinator_epoch"]),
            "lease_epoch": int(row["lease_epoch"]),
            "lease_expires_at": row["lease_expires_at"],
            "lease": {
                "owner_id": row["owner_id"],
                "coordinator_epoch": int(row["coordinator_epoch"]),
                "lease_epoch": int(row["lease_epoch"]),
                "expires_at": row["lease_expires_at"],
            },
            "repair_request_id": row["repair_request_id"],
            "repair_task_id": row["repair_task_id"],
            "repair_deployment_fingerprint": row["repair_deployment_fingerprint"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        repair = connection.execute(
            "SELECT * FROM node_recovery_repairs WHERE episode_id = ?", (row["episode_id"],)
        ).fetchone()
        result["repair"] = self._repair_row(repair) if repair is not None else None
        if include_actions:
            actions = connection.execute(
                """
                SELECT * FROM node_recovery_actions WHERE episode_id = ?
                ORDER BY created_at, action_id
                """,
                (row["episode_id"],),
            ).fetchall()
            result["actions"] = [self._action_row(action) for action in actions]
        return result

    @staticmethod
    def _episode_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "episode_id": str(row["episode_id"]),
            "task_id": str(row["task_id"]),
            "node_id": str(row["node_id"]),
            "node_attempt": int(row["node_attempt"]),
            "failure_fingerprint": str(row["failure_fingerprint"]),
            "revision": int(row["state_revision"]),
            "state": str(row["state"]),
            "action": row["action"],
            "phase": str(row["phase"]),
            "owner": str(row["owner"]),
            "permission_required": bool(row["permission_required"]),
            "action_attempts": int(row["action_attempts"]),
            "current_stage_key": row["current_stage_key"],
            "next_wakeup_at": row["next_wakeup_at"],
            "last_material_progress_at": str(row["last_material_progress_at"]),
            "repair_request_id": row["repair_request_id"],
            "repair_task_id": row["repair_task_id"],
            "repair_deployment_fingerprint": row["repair_deployment_fingerprint"],
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _action_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            action_input = json.loads(str(row["action_input_json"]))
            receipt = json.loads(str(row["receipt_json"])) if row["receipt_json"] is not None else None
        except json.JSONDecodeError as error:
            raise StateConflictError("recovery action row is invalid") from error
        return {
            "action_id": str(row["action_id"]),
            "episode_id": str(row["episode_id"]),
            "task_id": str(row["task_id"]),
            "node_id": str(row["node_id"]),
            "node_attempt": int(row["node_attempt"]),
            "action_key": str(row["action_key"]),
            "stage_key": str(row["stage_key"]),
            "intent_coordinator_epoch": int(row["intent_coordinator_epoch"]),
            "intent_lease_epoch": int(row["intent_lease_epoch"]),
            "action_fingerprint": str(row["action_fingerprint"]),
            "action_input": action_input,
            "state": str(row["state"]),
            "receipt": receipt,
            "effect_dispatched": bool(row["effect_dispatched"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "settled_at": row["settled_at"],
        }

    @staticmethod
    def _repair_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            refs = (
                json.loads(str(row["deployment_evidence_refs_json"]))
                if row["deployment_evidence_refs_json"] is not None
                else None
            )
        except json.JSONDecodeError as error:
            raise StateConflictError("recovery repair row is invalid") from error
        return {
            "repair_link_id": str(row["repair_link_id"]),
            "repair_request_id": str(row["repair_request_id"]),
            "repair_task_id": str(row["repair_task_id"]),
            "repair_fingerprint": str(row["repair_fingerprint"]),
            "verified_deployment_fingerprint": row["verified_deployment_fingerprint"],
            "deployment_evidence_refs": refs,
            "deployed_at": row["deployed_at"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _update_episode_if_material(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        observed: dict[str, Any],
        planned: dict[str, Any],
        policy: RecoveryPolicy,
        policy_revision: int,
        timestamp: str,
    ) -> dict[str, Any]:
        try:
            previous_observation = json.loads(str(row["observation_json"]))
            previous_decision = json.loads(str(row["decision_json"]))
            previous_refs = json.loads(str(row["evidence_refs_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("recovery episode row is invalid") from error
        merged_observation = _merge_observation(
            previous_observation, observed["document"], allow_task_revision=True
        )
        merged_refs = _evidence_refs(merged_observation.get("evidence_refs"))
        merged_cursor = max(int(row["source_event_cursor"]), observed["source_event_cursor"])
        observation_changed = previous_observation != merged_observation or previous_refs != merged_refs
        policy_changed = int(row["policy_revision"]) != policy_revision
        source_changed = int(row["source_event_cursor"]) != merged_cursor
        task_changed = int(row["task_revision"]) != observed["task_revision"]
        timer_decision = False
        if _due(str(row["time_budget_deadline_at"]), timestamp):
            planned = _budget_exhausted_decision()
            timer_decision = True
        elif (
            not observation_changed
            and not source_changed
            and not policy_changed
            and not task_changed
            and _timer_decision_due(row, timestamp)
        ):
            timer_decision = previous_decision != planned["document"]
        elif not observation_changed and not source_changed and not policy_changed and not task_changed:
            planned = _decision(
                previous_decision,
                observation=merged_observation,
                fallback_stage_key=(
                    str(row["current_stage_key"])
                    if row["current_stage_key"] is not None
                    else None
                ),
            )
        decision_changed = previous_decision != planned["document"]
        material = observation_changed or source_changed or task_changed
        if not material and not policy_changed and not decision_changed:
            return self._episode_row(connection, row)
        if self._lease_is_live(row, timestamp):
            raise StateConflictError("cannot replace a materially changed leased recovery episode")
        stage_attempts = _stage_attempts(row["stage_attempts_json"])
        stage_key = planned["stage_key"]
        if stage_key is None:
            stage_key = row["current_stage_key"]
        action_attempts = stage_attempts.get(stage_key, 0) if stage_key is not None else 0
        revision = int(row["state_revision"]) + 1
        cursor = self.base_store._event(
            connection,
            "node_recovery.timer_decision_updated" if timer_decision and not material else "node_recovery.episode_updated",
            str(row["task_id"]),
            str(row["node_id"]),
            {
                "episode_id": row["episode_id"],
                "state_revision": revision,
                "category": planned["category"],
                "state": planned["state"],
                "action": planned["action"],
                "source_event_cursor": merged_cursor,
                "timer_decision": timer_decision,
            },
            created_at=timestamp,
        )
        connection.execute(
            """
            UPDATE node_recovery_episodes
            SET task_revision = ?, policy_revision = ?, policy_json = ?, origin = ?,
                category = ?, phase = ?, observation_json = ?, decision_json = ?,
                evidence_refs_json = ?, source_event_cursor = ?, state = ?, action = ?,
                owner = ?, permission_required = ?, action_attempts = ?, current_stage_key = ?,
                state_revision = ?, next_wakeup_at = ?, last_material_progress_at = ?,
                last_progress_json = ?, updated_at = ?
            WHERE episode_id = ? AND state_revision = ?
            """,
            (
                observed["task_revision"], policy_revision, canonical_json(policy.to_dict()),
                observed["origin"], planned["category"], observed["phase"],
                canonical_json(merged_observation), canonical_json(planned["document"]),
                canonical_json(merged_refs), merged_cursor,
                planned["state"], planned["action"], planned["owner"],
                int(planned["requires_authorization"]), action_attempts, stage_key, revision,
                planned["next_wakeup_at"], timestamp if material else row["last_material_progress_at"],
                canonical_json(
                    {
                        "kind": "node_recovery.timer_decision_updated" if timer_decision and not material else "node_recovery.episode_updated",
                        "event_cursor": cursor,
                        "source_event_cursor": merged_cursor,
                    }
                ),
                timestamp, row["episode_id"], row["state_revision"],
            ),
        )
        current = self._episode_or_key_error(connection, str(row["episode_id"]))
        self._record_needs_action_notification(
            connection,
            episode_id=str(row["episode_id"]),
            task_id=str(row["task_id"]),
            node_id=str(row["node_id"]),
            planned=planned,
            timestamp=timestamp,
        )
        return self._episode_row(connection, current)

    def _record_needs_action_notification(
        self,
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        task_id: str,
        node_id: str,
        planned: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        if planned["state"] != "needs_action":
            return
        reason = _text(planned["reason_kind"], "decision.reason_kind")
        existing = connection.execute(
            """
            SELECT 1 FROM node_recovery_notifications
            WHERE episode_id = ? AND reason_kind = ?
            """,
            (episode_id, reason),
        ).fetchone()
        if existing is not None:
            return
        cursor = self.base_store._event(
            connection,
            "node_recovery.needs_action",
            task_id,
            node_id,
            {"episode_id": episode_id, "reason_kind": reason},
            created_at=timestamp,
        )
        connection.execute(
            """
            INSERT INTO node_recovery_notifications(episode_id, reason_kind, event_cursor, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (episode_id, reason, cursor, timestamp),
        )

    def _assert_episode_attempt(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        expected_node_attempt: int,
    ) -> None:
        if int(row["node_attempt"]) != expected_node_attempt:
            raise StateConflictError("recovery episode attempt is stale")
        node = self._node_row(connection, str(row["task_id"]), str(row["node_id"]))
        if int(node["attempt"]) != expected_node_attempt:
            raise StateConflictError("recovery node attempt changed")

    def _assert_episode_lease(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        owner_id: str,
        coordinator_epoch: int,
        lease_epoch: int,
        timestamp: str,
    ) -> None:
        self.base_store._assert_active_coordinator(connection, coordinator_epoch)
        if (
            row["owner_id"] != owner_id
            or int(row["coordinator_epoch"]) != coordinator_epoch
            or int(row["lease_epoch"]) != lease_epoch
            or not self._lease_is_live(row, timestamp)
        ):
            raise StateConflictError("recovery episode lease is stale")

    @staticmethod
    def _lease_is_live(row: sqlite3.Row, timestamp: str) -> bool:
        return (
            row["owner_id"] is not None
            and row["lease_expires_at"] is not None
            and not _due(str(row["lease_expires_at"]), timestamp)
        )

    @staticmethod
    def _user_fence(task: sqlite3.Row, node: sqlite3.Row) -> bool:
        return str(task["state"]).lower() in {"paused", "cancelled"} or str(node["state"]).lower() in {
            "paused", "cancelled"
        }

    def _suspend_for_user_fence(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        timestamp: str,
    ) -> dict[str, Any]:
        if row["state"] == "suspended" and row["owner_id"] is None:
            return self._episode_row(connection, row)
        planned = _suspended_decision("user_pause")
        revision = int(row["state_revision"]) + 1
        cursor = self.base_store._event(
            connection,
            "node_recovery.suspended",
            str(row["task_id"]),
            str(row["node_id"]),
            {"episode_id": row["episode_id"], "state_revision": revision, "reason_kind": "user_pause"},
            created_at=timestamp,
        )
        connection.execute(
            """
            UPDATE node_recovery_episodes
            SET state = ?, action = NULL, owner = ?, permission_required = ?,
                phase = 'suspended', state_revision = ?, owner_id = NULL,
                coordinator_epoch = 0, lease_epoch = 0, lease_expires_at = NULL,
                next_wakeup_at = NULL, decision_json = ?, last_material_progress_at = ?,
                last_progress_json = ?, updated_at = ?
            WHERE episode_id = ? AND state_revision = ?
            """,
            (
                planned["state"], planned["owner"], int(planned["requires_authorization"]),
                revision, canonical_json(planned["document"]), timestamp,
                canonical_json(
                    {"kind": "node_recovery.suspended", "event_cursor": cursor, "reason_kind": "user_pause"}
                ),
                timestamp, row["episode_id"], row["state_revision"],
            ),
        )
        current = self._episode_or_key_error(connection, str(row["episode_id"]))
        return self._episode_row(connection, current)

    def _exhaust_budget(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        timestamp: str,
    ) -> dict[str, Any]:
        if row["state"] == "needs_action" and row["action"] is None:
            return self._episode_row(connection, row)
        planned = _budget_exhausted_decision()
        revision = int(row["state_revision"]) + 1
        cursor = self.base_store._event(
            connection,
            "node_recovery.budget_exhausted",
            str(row["task_id"]),
            str(row["node_id"]),
            {"episode_id": row["episode_id"], "state_revision": revision},
            created_at=timestamp,
        )
        connection.execute(
            """
            UPDATE node_recovery_episodes
            SET state = ?, action = NULL, owner = ?, permission_required = ?,
                phase = 'budget_exhausted', state_revision = ?, owner_id = NULL,
                coordinator_epoch = 0, lease_epoch = 0, lease_expires_at = NULL,
                next_wakeup_at = NULL, decision_json = ?, last_material_progress_at = ?,
                last_progress_json = ?, updated_at = ?
            WHERE episode_id = ? AND state_revision = ?
            """,
            (
                planned["state"], planned["owner"], int(planned["requires_authorization"]),
                revision, canonical_json(planned["document"]), timestamp,
                canonical_json({"kind": "node_recovery.budget_exhausted", "event_cursor": cursor}),
                timestamp, row["episode_id"], row["state_revision"],
            ),
        )
        current = self._episode_or_key_error(connection, str(row["episode_id"]))
        self._record_needs_action_notification(
            connection,
            episode_id=str(row["episode_id"]),
            task_id=str(row["task_id"]),
            node_id=str(row["node_id"]),
            planned=planned,
            timestamp=timestamp,
        )
        return self._episode_row(connection, current)

    @staticmethod
    def _read_cursor_connection(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT event_cursor FROM node_recovery_cursors WHERE projection = ?",
            (_PROJECTION_CURSOR,),
        ).fetchone()
        return 0 if row is None else int(row["event_cursor"])


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > _TEXT_LIMIT or "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError(f"{label} is invalid")
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _lease_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3_600:
        raise ValueError("recovery lease_seconds must be between 1 and 3600")
    return value


def _fingerprint(value: object, label: str) -> str:
    text = _text(value, label)
    if _FINGERPRINT.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return text


def _policy(value: RecoveryPolicy | Mapping[str, object]) -> RecoveryPolicy:
    if isinstance(value, RecoveryPolicy):
        return value
    return RecoveryPolicy.from_dict(value)


def _bounded_document(value: Mapping[str, object], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")

    def visit(item: object, path: str, depth: int) -> Any:
        if depth > 8:
            raise ValueError(f"{label} is too deeply nested")
        if item is None or isinstance(item, (bool, int, float, str)):
            return item
        if isinstance(item, Mapping):
            if len(item) > 128:
                raise ValueError(f"{label} has too many fields")
            result: dict[str, Any] = {}
            for key, child in item.items():
                name = _text(key, f"{label} key")
                if name.lower() in _FORBIDDEN_DOCUMENT_KEYS:
                    raise ValueError(f"{label} may not persist {name}")
                result[name] = visit(child, path + "." + name, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > 128:
                raise ValueError(f"{label} has too many items")
            return [visit(child, path, depth + 1) for child in item]
        raise ValueError(f"{label} is not JSON-safe")

    normalized = visit(value, label, 0)
    assert isinstance(normalized, dict)
    try:
        rendered = json.dumps(normalized, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON-safe") from error
    if len(rendered.encode()) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} exceeds the bounded persistence limit")
    return normalized


def _evidence_refs(value: object) -> dict[str, str] | list[str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        if len(value) > _MAX_EVIDENCE_REFS:
            raise ValueError("evidence_refs has too many entries")
        result: dict[str, str] = {}
        for key, ref in value.items():
            result[_text(key, "evidence_refs key")] = _text(ref, "evidence_refs value")
        return result
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        if len(value) > _MAX_EVIDENCE_REFS:
            raise ValueError("evidence_refs has too many entries")
        return [_text(ref, "evidence_refs value") for ref in value]
    raise ValueError("evidence_refs must be an object or array")


def _merge_observation(
    previous: object,
    incoming: Mapping[str, object],
    *,
    allow_task_revision: bool = False,
    allow_evidence_reset: bool = False,
) -> dict[str, Any]:
    """Merge fresh Authority facts without letting old blocked receipts regress them."""

    prior = _bounded_document(previous, "stored observation")
    fresh = _bounded_document(incoming, "observation")
    immutable = {
        "task_id",
        "node_id",
        "attempt",
        "failure_fingerprint",
        "origin",
    }
    if not allow_task_revision:
        immutable.add("task_revision")
    for key in immutable:
        if key in fresh and fresh[key] != prior.get(key):
            raise StateConflictError(f"recovery observation changed immutable {key}")
    result: dict[str, Any] = dict(prior)
    for key, value in fresh.items():
        if key == "evidence_refs":
            result[key] = _merge_evidence_refs(prior.get(key), value)
        elif key in {
            "readiness_ready",
            "validation_succeeded",
        }:
            if value is None and not allow_evidence_reset:
                # A fresh base snapshot with no readiness receipt is absence,
                # not evidence that a prior verified result became false.
                result[key] = prior.get(key)
                continue
            if not isinstance(value, bool):
                raise ValueError(f"observation.{key} must be a boolean")
            result[key] = value if allow_evidence_reset else bool(prior.get(key)) or value
        elif key in {
            "repair_requested",
            "repair_deployed_verified",
            "repair_deployed",
        }:
            if not isinstance(value, bool):
                raise ValueError(f"observation.{key} must be a boolean")
            result[key] = bool(prior.get(key)) or value
        elif key == "source_event_cursor":
            result[key] = max(
                _nonnegative_int(prior.get(key, 0), "stored source_event_cursor"),
                _nonnegative_int(value, "observation.source_event_cursor"),
            )
        else:
            result[key] = value
    return _bounded_document(result, "merged observation")


def _merge_evidence_refs(previous: object, incoming: object) -> dict[str, str] | list[str]:
    old = _evidence_refs(previous)
    new = _evidence_refs(incoming)
    if isinstance(old, dict) and isinstance(new, dict):
        return {**old, **new}
    values: list[str] = []
    for source in (old, new):
        iterable = source.values() if isinstance(source, dict) else source
        for ref in iterable:
            if ref not in values:
                values.append(ref)
    if len(values) > _MAX_EVIDENCE_REFS:
        raise ValueError("evidence_refs has too many entries")
    return values


def _observation(value: Mapping[str, object]) -> dict[str, Any]:
    raw_document = _bounded_document(value, "observation")
    # These fields are inputs to the pure policy clock, not durable evidence.
    # Persisting them would manufacture progress every sweep.
    document = {
        key: item
        for key, item in raw_document.items()
        if key not in {"now", "elapsed_seconds", "action_attempts", "last_action", "last_action_at", "retry_due_at"}
    }
    for name in ("validation_succeeded", "repair_requested"):
        if name in document and not isinstance(document[name], bool):
            raise ValueError(f"observation.{name} must be a boolean")
    return {
        "task_id": _text(document.get("task_id"), "observation.task_id"),
        "node_id": _text(document.get("node_id"), "observation.node_id"),
        "attempt": _nonnegative_int(document.get("attempt"), "observation.attempt"),
        "task_revision": _positive_int(document.get("task_revision"), "observation.task_revision"),
        "failure_fingerprint": _fingerprint(
            document.get("failure_fingerprint"), "observation.failure_fingerprint"
        ),
        "origin": _text(document.get("origin"), "observation.origin"),
        "phase": _text(document.get("phase", "observed"), "observation.phase"),
        "source_event_cursor": _nonnegative_int(
            document.get("source_event_cursor"), "observation.source_event_cursor"
        ),
        "evidence_refs": _evidence_refs(document.get("evidence_refs")),
        "document": document,
    }


def _decision(
    value: Mapping[str, object],
    *,
    observation: Mapping[str, object] | None = None,
    fallback_stage_key: str | None = None,
) -> dict[str, Any]:
    document = _bounded_document(value, "decision")
    state = _text(document.get("state"), "decision.state")
    if state not in _DECISION_STATES:
        raise ValueError("decision.state is unsupported")
    raw_action = document.get("action")
    action = None if raw_action is None else _text(raw_action, "decision.action")
    owner = _text(document.get("owner"), "decision.owner")
    reason_kind = _text(document.get("reason_kind"), "decision.reason_kind")
    category = _text(document.get("category"), "decision.category")
    requires_authorization = document.get("requires_authorization")
    if not isinstance(requires_authorization, bool):
        raise ValueError("decision.requires_authorization must be a boolean")
    wakeup = _timestamp(document.get("next_wakeup_at"), "decision.next_wakeup_at")
    if action is None:
        stage_key = None
    elif document.get("stage_key") is not None:
        stage_key = _text(document["stage_key"], "decision.stage_key")
    elif fallback_stage_key is not None and _stage_action(fallback_stage_key) == action:
        stage_key = fallback_stage_key
    else:
        profile = document.get("validation_profile")
        if profile is None and observation is not None:
            profile = observation.get("validation_profile")
        stage_key = action if action != "narrow_validation" else (
            "narrow_validation:" + _text(profile, "validation_profile")
            if profile is not None
            else "narrow_validation"
        )
    return {
        "state": state,
        "action": action,
        "owner": owner,
        "reason_kind": reason_kind,
        "category": category,
        "requires_authorization": requires_authorization,
        "next_wakeup_at": wakeup,
        "stage_key": stage_key,
        "document": document,
    }


def _stage_action(stage_key: str) -> str:
    return stage_key.split(":", 1)[0]


def _stage_attempts(value: object) -> dict[str, int]:
    try:
        raw = json.loads(str(value)) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise StateConflictError("recovery stage attempts are invalid") from error
    if not isinstance(raw, Mapping):
        raise StateConflictError("recovery stage attempts are invalid")
    result: dict[str, int] = {}
    for key, count in raw.items():
        stage = _text(key, "stage attempt key")
        result[stage] = _nonnegative_int(count, "stage attempt count")
    return result


def _timestamp(value: object, label: str) -> str | None:
    if value is None:
        return None
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _after(timestamp: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(timestamp)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _due(value: object, timestamp: str) -> bool:
    if value is None:
        return True
    return datetime.fromisoformat(str(value)) <= datetime.fromisoformat(timestamp)


def _timer_decision_due(row: sqlite3.Row, timestamp: str) -> bool:
    return row["state"] == "waiting" and row["next_wakeup_at"] is not None and _due(
        row["next_wakeup_at"], timestamp
    )


def _episode_id(task_id: str, node_id: str, attempt: int, failure_fingerprint: str) -> str:
    return "node-recovery-" + canonical_hash(
        {
            "task_id": task_id,
            "node_id": node_id,
            "attempt": attempt,
            "failure_fingerprint": failure_fingerprint,
        }
    )[:24]


def _manual_decision(
    *,
    state: str,
    reason_kind: str,
    owner: str,
    requires_authorization: bool,
    next_wakeup_at: str | None,
) -> dict[str, Any]:
    document = {
        "category": "unknown_effects" if reason_kind == "unknown_effects" else "unknown",
        "state": state,
        "action": None,
        "owner": owner,
        "reason_kind": reason_kind,
        "requires_authorization": requires_authorization,
        "next_wakeup_at": next_wakeup_at,
    }
    return _decision(document)


def _suspended_decision(reason_kind: str) -> dict[str, Any]:
    return _manual_decision(
        state="suspended", reason_kind=reason_kind, owner="user",
        requires_authorization=False, next_wakeup_at=None,
    )


def _unknown_effects_decision() -> dict[str, Any]:
    return _manual_decision(
        state="needs_action", reason_kind="unknown_effects", owner="user",
        requires_authorization=True, next_wakeup_at=None,
    )


def _budget_exhausted_decision() -> dict[str, Any]:
    return _manual_decision(
        state="needs_action", reason_kind="budget_exhausted", owner="user",
        requires_authorization=False, next_wakeup_at=None,
    )


def _repair_deployed_wait_decision(backoff_seconds: int, timestamp: str) -> dict[str, Any]:
    return _manual_decision(
        state="waiting", reason_kind="fresh_readiness_required", owner="authority",
        requires_authorization=False, next_wakeup_at=_after(timestamp, backoff_seconds),
    )
