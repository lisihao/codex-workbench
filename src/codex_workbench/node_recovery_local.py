"""Bounded local actions for one already-authorized blocked-node recovery.

The outer recovery reconciler owns leases, durable action intents, and receipt
settlement.  This adapter owns no scheduler and accepts no command text.  It
only delegates readiness to the existing read-only adapter, invokes the
existing cached-template materializer, or fences one clean blocked retry with
the Store's current revision/attempt CAS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from pathlib import Path
import stat
from typing import Any

from .config import WorkbenchConfig
from .dirty_worktree_recovery import (
    DirtyWorktreeRecoveryError,
    PnpmOfflineMaterializer,
    inspect_indeterminate_source_delta,
)
from .execution_readiness import ExecutionReadinessRequest
from .model import canonical_hash, canonical_json
from .node_recovery_observation import collect_node_observation
from .node_recovery_readiness import ReadinessNodeActions
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError, WorkbenchStore
from .worktrees import WorktreeManager


_ACTIONS = frozenset({"observe_readiness", "materialize_dependencies", "resume_node"})
_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "action",
        "stage_key",
        "request_id",
        "task_id",
        "node_id",
        "node_attempt",
        "task_revision",
        "policy_revision",
        "policy_fingerprint",
        "source",
        "runtime",
        "resume_evidence",
        "readiness_plan",
        "action_fingerprint",
    }
)
_MANIFEST_BYTES_LIMIT = 64 * 1024
_REQUEST_ID_MAX_LENGTH = 200
_MATERIALIZATION_KINDS = frozenset({"pnpm-offline-materialization", "not-applicable"})
_MATERIALIZATION_SUCCESS_STATUSES = frozenset({None, "succeeded", "passed"})


class LocalNodeActions:
    """Execute only three fixed local recovery actions.

    @param config: Existing Authority configuration.  Only the bounded install
        manifest identity participates in the plan fingerprint.
    @param store: Existing Authority store; this adapter creates neither a
        second database nor a new Authority service.
    @param readiness_request_factory: The Coordinator's existing fixed
        readiness-request builder for one allocated worktree.
    @param materializer: The existing offline pnpm materializer.
    @param materialize_timeout_seconds: A strict local materialization bound.
    """

    def __init__(
        self,
        config: WorkbenchConfig,
        store: WorkbenchStore,
        *,
        readiness_request_factory: Callable[[Path], ExecutionReadinessRequest],
        materializer: PnpmOfflineMaterializer,
        materialize_timeout_seconds: int = 60,
    ) -> None:
        if not isinstance(config, WorkbenchConfig):
            raise TypeError("config must be a WorkbenchConfig")
        if not isinstance(store, WorkbenchStore):
            raise TypeError("store must be a WorkbenchStore")
        if not callable(readiness_request_factory):
            raise TypeError("readiness_request_factory must be callable")
        if not callable(getattr(materializer, "materialize", None)):
            raise TypeError("materializer must provide materialize")
        if (
            type(materialize_timeout_seconds) is not int
            or not 1 <= materialize_timeout_seconds <= 60
        ):
            raise ValueError("materialize_timeout_seconds must be an integer between 1 and 60")
        self.config = config
        self.store = store
        self.recovery = NodeRecoveryStore(store)
        self.materializer = materializer
        self.materialize_timeout_seconds = materialize_timeout_seconds
        self._readiness = ReadinessNodeActions(
            config, store, readiness_request_factory=readiness_request_factory
        )
        self._request_factory = readiness_request_factory
        # The outer action intent is durable.  This narrow in-process fence
        # still prevents a caught exception from immediately replaying a
        # materializer call before the caller can settle/reconcile the intent.
        self._attempted_request_ids: set[str] = set()

    def prepare(
        self,
        observation: Mapping[str, Any],
        action: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Return a fixed, read-only plan bound to the current blocked node."""

        normalized_action = self._action(action)
        normalized_request_id = self._request_id(request_id)
        identity = self._identity(observation)
        current = self._current(identity, normalized_action)
        readiness_plan: dict[str, Any] | None = None
        if normalized_action == "observe_readiness":
            readiness_plan = self._readiness.prepare(
                dict(observation), normalized_action, normalized_request_id
            )
        plan: dict[str, Any] = {
            "schema_version": 1,
            "kind": "node-recovery-local/v1",
            "action": normalized_action,
            "stage_key": normalized_action,
            "request_id": normalized_request_id,
            **identity,
            "policy_revision": current["policy_revision"],
            "policy_fingerprint": current["policy_fingerprint"],
            "source": self._source_binding(current),
            "runtime": self._runtime_binding(current["worktree"]),
            "resume_evidence": self._resume_evidence(observation),
            "readiness_plan": readiness_plan,
            "action_fingerprint": "",
        }
        plan["action_fingerprint"] = canonical_hash(self._unsigned_plan(plan))
        return plan

    def execute(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Run one plan after re-reading its task, node, policy, and runtime."""

        normalized = self._validated_plan(plan)
        request_id = normalized["request_id"]
        if request_id in self._attempted_request_ids:
            raise StateConflictError(
                "local recovery action may have been dispatched; reconcile its durable intent"
            )
        current = self._assert_plan_current(normalized)
        self._attempted_request_ids.add(request_id)
        if normalized["action"] == "observe_readiness":
            return self._observe_readiness(normalized)
        if normalized["action"] == "materialize_dependencies":
            return self._materialize_dependencies(normalized, current)
        return self._resume_node(normalized)

    def reconcile(self, plan: Mapping[str, Any]) -> dict[str, Any] | None:
        """Read a durable retry authorization only; never infer success."""

        normalized = self._validated_plan(plan)
        if normalized["action"] != "resume_node":
            return None
        try:
            task = self.store.get_task(normalized["task_id"])
        except KeyError:
            return None
        if self._node(task, normalized["node_id"]) is None:
            return None
        cursor = self._retry_authorization_cursor(normalized)
        if cursor is None:
            return None
        return self._receipt(
            normalized,
            ok=True,
            stage_succeeded=True,
            known_effects=True,
            evidence_refs={"retry_authorization": f"event:{cursor}"},
            observation_patch={"recovery_resumed": True},
        )

    def _observe_readiness(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        readiness_plan = plan["readiness_plan"]
        assert isinstance(readiness_plan, Mapping)
        try:
            receipt = self._readiness.execute(dict(readiness_plan))
        except (OSError, StateConflictError, ValueError):
            return self._unknown_receipt(plan, "readiness_outcome_unknown")
        if not isinstance(receipt, Mapping):
            return self._unknown_receipt(plan, "readiness_receipt_invalid")
        required = {"ok", "stage_succeeded", "known_effects", "evidence_refs", "observation_patch"}
        if not required.issubset(receipt):
            return self._unknown_receipt(plan, "readiness_receipt_invalid")
        return {
            "action": plan["action"],
            "request_id": plan["request_id"],
            **dict(receipt),
        }

    def _materialize_dependencies(
        self,
        plan: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.materializer.materialize(
                current["worktree"],
                timeout_seconds=self.materialize_timeout_seconds,
                require_cached_template=True,
            )
            if not isinstance(result, Mapping):
                return self._unknown_receipt(plan, "materialization_receipt_invalid")
            receipt_ref = self.recovery.artifacts.put_text(
                canonical_json(dict(result)), "dependency-materialization.json"
            )
        except Exception:
            # A cached clone may have altered a local linker before its error;
            # the outer durable intent must become unknown rather than retry.
            return self._unknown_receipt(plan, "materialization_outcome_unknown")
        kind = result.get("kind")
        status = result.get("status")
        if kind not in _MATERIALIZATION_KINDS or status not in (
            *_MATERIALIZATION_SUCCESS_STATUSES,
            "blocked",
        ):
            return self._unknown_receipt(plan, "materialization_outcome_unknown")
        blocked = status == "blocked"
        if not blocked:
            try:
                self._assert_plan_current(plan)
            except StateConflictError:
                # The local linker result is durable evidence, but a changed
                # policy, revision, source binding, pause, or cancellation
                # must not be reported as readiness for a later stage.
                return self._receipt(
                    plan,
                    ok=False,
                    stage_succeeded=False,
                    known_effects=True,
                    evidence_refs={"dependency-materialization": receipt_ref},
                    observation_patch={"needs_action": True},
                    reason_kind="materialization_postcondition_changed",
                )
        return self._receipt(
            plan,
            ok=not blocked,
            stage_succeeded=not blocked,
            known_effects=True,
            evidence_refs={"dependency-materialization": receipt_ref},
            observation_patch={
                "dependencies_ready": not blocked,
                # A materialized linker is not a new readiness result.
                "readiness_ready": False,
            },
            reason_kind="materialization_blocked" if blocked else None,
        )

    def _resume_node(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        evidence = plan["resume_evidence"]
        assert isinstance(evidence, Mapping)
        if not (
            evidence["phase"] == "pre_execution"
            and evidence["readiness_ready"] is True
            and evidence["only_missing_dependency"] is True
            and evidence["executor_not_started"] is True
        ):
            return self._needs_action(plan, "resume_evidence_incomplete")
        current_observation = collect_node_observation(
            self.store, plan["task_id"], plan["node_id"]
        )
        if not self._current_failed_readiness(current_observation, plan):
            return self._needs_action(plan, "resume_failed_readiness_unproven")
        try:
            before = self._clean_retry_snapshot(plan)
        except (KeyError, StateConflictError, ValueError):
            return self._needs_action(plan, "resume_durable_binding_unproven")
        try:
            delta = inspect_indeterminate_source_delta(
                before["source_candidate"],
                dependency_input_ref=before["dependency_input_ref"],
                artifacts=self.recovery.artifacts,
                recovery_label="blocked",
            )
        except (DirtyWorktreeRecoveryError, OSError, ValueError):
            return self._needs_action(plan, "resume_source_delta_unproven")
        if delta.changed_paths or delta.untracked_paths:
            return self._needs_action(plan, "resume_source_delta_not_empty")
        try:
            after = self._clean_retry_snapshot(plan)
        except (KeyError, StateConflictError, ValueError):
            return self._needs_action(plan, "resume_durable_binding_changed")
        if canonical_json(before["durable_binding"]) != canonical_json(after["durable_binding"]):
            return self._needs_action(plan, "resume_durable_binding_changed")
        try:
            delta_ref = self.recovery.artifacts.put_text(
                canonical_json(
                    {
                        "schema_version": 1,
                        "kind": "node-recovery-clean-source-delta",
                        "comparison_tree": delta.comparison_tree,
                        "changed_paths": [],
                        "untracked_paths": [],
                        "sha256": delta.sha256,
                    }
                ),
                "node-recovery-clean-source-delta.json",
            )
        except Exception:
            return self._needs_action(plan, "resume_delta_receipt_unavailable")
        try:
            result = self.store.retry_blocked_node(
                plan["task_id"],
                plan["node_id"],
                expected_revision=plan["task_revision"],
                expected_attempt=plan["node_attempt"],
                reason="verified pre-execution Authority readiness block with an empty source delta",
                confirm_no_side_effects=True,
            )
        except (KeyError, StateConflictError, ValueError):
            reconciled = self.reconcile(plan)
            return reconciled or self._unknown_receipt(plan, "resume_retry_outcome_unknown")
        cursor = result.get("authorization_event_cursor")
        if type(cursor) is not int or cursor <= 0:
            return self._unknown_receipt(plan, "resume_authorization_receipt_invalid")
        return self._receipt(
            plan,
            ok=True,
            stage_succeeded=True,
            known_effects=True,
            evidence_refs={
                "source-delta": delta_ref,
                "retry_authorization": f"event:{cursor}",
            },
            observation_patch={"recovery_resumed": True},
        )

    def _current(self, identity: Mapping[str, Any], action: str) -> dict[str, Any]:
        task = self.store.get_task(identity["task_id"])
        if task.get("state") in {"paused", "cancelled", "needs_approval"}:
            raise StateConflictError("local recovery is fenced by pause or cancellation")
        if task.get("state") != "blocked" or task.get("state_revision") != identity["task_revision"]:
            raise StateConflictError("local recovery task revision or state changed")
        node = self._node(task, identity["node_id"])
        if node is None:
            raise KeyError((identity["task_id"], identity["node_id"]))
        if node.get("state") != "blocked" or node.get("attempt") != identity["node_attempt"]:
            raise StateConflictError("local recovery node attempt or state changed")
        raw_worktree = node.get("worktree")
        if not isinstance(raw_worktree, str) or not raw_worktree:
            raise StateConflictError("blocked node has no allocated worktree")
        try:
            worktree = Path(raw_worktree).expanduser().resolve(strict=True)
        except OSError as error:
            raise StateConflictError("blocked node worktree is unavailable") from error
        if not worktree.is_dir():
            raise StateConflictError("blocked node worktree is not a directory")
        policy_row = self.recovery.get_policy(identity["task_id"])
        policy = policy_row.get("policy")
        if not isinstance(policy, Mapping) or policy.get("enabled") is not True:
            raise StateConflictError("local recovery policy is disabled")
        allowed = policy.get("allowed_actions")
        if not isinstance(allowed, list) or action not in allowed:
            raise StateConflictError("local recovery action is not authorized")
        revision = policy_row.get("policy_revision")
        if type(revision) is not int or revision < 1:
            raise StateConflictError("local recovery policy revision is invalid")
        return {
            "task": task,
            "node": node,
            "worktree": worktree,
            "policy_revision": revision,
            "policy_fingerprint": canonical_hash(dict(policy)),
        }

    def _assert_plan_current(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        current = self._current(self._identity(plan), plan["action"])
        if (
            current["policy_revision"] != plan["policy_revision"]
            or current["policy_fingerprint"] != plan["policy_fingerprint"]
        ):
            raise StateConflictError("local recovery policy changed after prepare")
        if canonical_json(self._source_binding(current)) != canonical_json(plan["source"]):
            raise StateConflictError("local recovery source binding changed after prepare")
        if canonical_json(self._runtime_binding(current["worktree"])) != canonical_json(plan["runtime"]):
            raise StateConflictError("local recovery runtime binding changed after prepare")
        return current

    def _source_binding(self, current: Mapping[str, Any]) -> dict[str, Any]:
        task = current["task"]
        node = current["node"]
        assert isinstance(task, Mapping) and isinstance(node, Mapping)
        contract = task.get("contract")
        if not isinstance(contract, Mapping):
            raise StateConflictError("local recovery task contract is invalid")
        repository = contract.get("repository")
        base_sha = contract.get("base_sha")
        contract_hash = task.get("contract_hash")
        if not all(isinstance(value, str) and value for value in (repository, base_sha, contract_hash)):
            raise StateConflictError("local recovery source binding is invalid")
        result = node.get("result")
        artifacts = result.get("artifacts") if isinstance(result, Mapping) else None
        dependency_input_ref = artifacts.get("dependency-input") if isinstance(artifacts, Mapping) else None
        if dependency_input_ref is not None and (
            not isinstance(dependency_input_ref, str) or not dependency_input_ref
        ):
            raise StateConflictError("local recovery dependency input reference is invalid")
        return {
            "repository": repository,
            "base_sha": base_sha,
            "contract_hash": contract_hash,
            "worktree": str(current["worktree"]),
            "dependency_input_ref": dependency_input_ref,
        }

    def _runtime_binding(self, worktree: Path) -> dict[str, Any]:
        request = self._readiness_request(worktree)
        pnpm = request.pnpm
        return {
            "install_manifest": self._install_manifest_identity(),
            "readiness": {
                "python_executable": request.python_executable,
                "probe_timeout_seconds": request.probe_timeout_seconds,
                "total_timeout_seconds": request.total_timeout_seconds,
                "pnpm": None if pnpm is None else {
                    "mode": pnpm.mode,
                    "binary": pnpm.binary,
                    "require_linker": pnpm.require_linker,
                },
                "tools": [
                    (item.name, item.executable, item.version_argument, item.required)
                    for item in request.tools
                ],
                "dependencies": [
                    (item.name, item.relative_path, item.kind, item.required)
                    for item in request.dependencies
                ],
                "source_resolutions": [
                    (item.module, item.source_roots) for item in request.source_resolutions
                ],
            },
            "materializer": {
                "binary": self._text_or_none(getattr(self.materializer, "binary", None)),
                "store_dir": self._path_or_none(getattr(self.materializer, "store_dir", None)),
                "template_dir": self._path_or_none(getattr(self.materializer, "template_dir", None)),
                "timeout_seconds": self.materialize_timeout_seconds,
                "require_cached_template": True,
            },
        }

    def _readiness_request(self, worktree: Path) -> ExecutionReadinessRequest:
        request = self._request_factory(worktree)
        if not isinstance(request, ExecutionReadinessRequest):
            raise TypeError("readiness_request_factory must return ExecutionReadinessRequest")
        try:
            requested = Path(request.worktree).expanduser().resolve(strict=True)
        except OSError as error:
            raise StateConflictError("readiness request worktree is unavailable") from error
        if requested != worktree:
            raise StateConflictError("readiness request is not bound to the blocked node worktree")
        return request

    def _install_manifest_identity(self) -> dict[str, Any]:
        path = self.config.install_manifest
        try:
            metadata = path.stat()
        except OSError:
            return {"status": "unavailable"}
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MANIFEST_BYTES_LIMIT:
            return {"status": "unavailable"}
        try:
            payload = path.read_bytes()
        except OSError:
            return {"status": "unavailable"}
        if len(payload) != metadata.st_size or len(payload) > _MANIFEST_BYTES_LIMIT:
            return {"status": "unavailable"}
        return {"status": "available", "sha256": sha256(payload).hexdigest()}

    def _clean_retry_snapshot(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Build the minimum clean-retry source proof using only reads.

        The Store's existing source-only snapshot intentionally rejects an
        empty historical ``changed_paths`` receipt.  Clean retry requires that
        exact empty receipt, so this reads only the corresponding allocation,
        contract, spec, and dependency reference after the Store's own
        ``_blocked_retry_candidate`` has fenced the task/node CAS inputs.
        """

        with self.store.connection() as connection:
            candidate = self.store._blocked_retry_candidate(
                connection,
                plan["task_id"],
                plan["node_id"],
                expected_revision=plan["task_revision"],
                expected_attempt=plan["node_attempt"],
            )
            task = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?", (plan["task_id"],)
            ).fetchone()
            node = connection.execute(
                "SELECT spec_json, result_json, worktree FROM nodes WHERE task_id = ? AND node_id = ?",
                (plan["task_id"], plan["node_id"]),
            ).fetchone()
            allocation = connection.execute(
                """
                SELECT allocation_id, state, repository, base_sha, branch, current_path, attempt
                FROM worktree_allocations WHERE task_id = ? AND node_id = ? AND attempt = ?
                """,
                (plan["task_id"], plan["node_id"], plan["node_attempt"]),
            ).fetchone()
        if task is None or node is None or allocation is None:
            raise StateConflictError("clean retry durable binding is incomplete")
        try:
            contract = json.loads(str(task["contract_json"]))
            spec = json.loads(str(node["spec_json"]))
            result = json.loads(str(node["result_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise StateConflictError("clean retry durable binding is invalid JSON") from error
        if not isinstance(contract, dict) or not isinstance(spec, dict) or not isinstance(result, dict):
            raise StateConflictError("clean retry durable binding is invalid")
        repository = contract.get("repository")
        base_sha = contract.get("base_sha")
        allowed_scope = contract.get("allowed_scope")
        forbidden_scope = contract.get("forbidden_scope")
        write_scopes = spec.get("write_scopes")
        depends_on = spec.get("depends_on")
        if not (
            isinstance(repository, str) and repository
            and isinstance(base_sha, str) and base_sha
            and isinstance(allowed_scope, list) and all(isinstance(item, str) for item in allowed_scope)
            and isinstance(forbidden_scope, list) and all(isinstance(item, str) for item in forbidden_scope)
            and isinstance(write_scopes, list) and all(isinstance(item, str) for item in write_scopes)
            and isinstance(depends_on, list) and all(isinstance(item, str) and item for item in depends_on)
        ):
            raise StateConflictError("clean retry source metadata is invalid")
        if (
            contract.get("external_write_permission") is not False
            or contract.get("destructive_action_permission") is not False
        ):
            raise StateConflictError("clean retry requires no external or destructive permission")
        expected_branch = WorktreeManager.branch_name(
            plan["task_id"], plan["node_id"], plan["node_attempt"]
        )
        raw_worktree = node["worktree"]
        if not isinstance(raw_worktree, str) or not raw_worktree:
            raise StateConflictError("clean retry worktree is unavailable")
        if (
            allocation["state"] != "active"
            or allocation["current_path"] != raw_worktree
            or allocation["repository"] != repository
            or allocation["base_sha"] != base_sha
            or allocation["branch"] != expected_branch
            or int(allocation["attempt"]) != plan["node_attempt"]
        ):
            raise StateConflictError("clean retry allocation does not match its original attempt")
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise StateConflictError("clean retry result artifacts are invalid")
        dependency_input_ref = artifacts.get("dependency-input")
        if dependency_input_ref is not None and (
            not isinstance(dependency_input_ref, str) or not dependency_input_ref
        ):
            raise StateConflictError("clean retry dependency input reference is invalid")
        source_candidate = {
            "task": {
                **candidate["task"],
                "repository": repository,
                "base_sha": base_sha,
                "allowed_scope": tuple(allowed_scope),
                "forbidden_scope": tuple(forbidden_scope),
            },
            "node": {
                **candidate["node"],
                "worktree": raw_worktree,
                "branch": expected_branch,
                "write_scopes": tuple(write_scopes),
                "depends_on": tuple(depends_on),
            },
        }
        return {
            "source_candidate": source_candidate,
            "dependency_input_ref": dependency_input_ref,
            "durable_binding": {
                "candidate": candidate,
                "contract_json": str(task["contract_json"]),
                "spec_json": str(node["spec_json"]),
                "historical_result_json": str(node["result_json"]),
                "allocation": {
                    "allocation_id": str(allocation["allocation_id"]),
                    "state": str(allocation["state"]),
                    "repository": str(allocation["repository"]),
                    "base_sha": str(allocation["base_sha"]),
                    "branch": str(allocation["branch"]),
                    "current_path": str(allocation["current_path"]),
                    "attempt": int(allocation["attempt"]),
                },
            },
        }

    @staticmethod
    def _current_failed_readiness(observation: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
        return (
            observation.get("task_id") == plan["task_id"]
            and observation.get("node_id") == plan["node_id"]
            and observation.get("node_attempt") == plan["node_attempt"]
            and observation.get("task_revision") == plan["task_revision"]
            and observation.get("phase") == "pre_execution"
            and observation.get("origin") == "environment"
            and observation.get("category") == "dependency"
            and observation.get("only_missing_dependency") is True
            and observation.get("executor_not_started") is True
        )

    def _retry_authorization_cursor(self, plan: Mapping[str, Any]) -> int | None:
        if not self._matching_outer_intent(plan):
            return None
        expected_revision = plan["task_revision"] + 1
        expected_attempt = plan["node_attempt"] + 1
        for event in reversed(self.store.read_events(task_id=plan["task_id"])):
            if event.get("event_type") != "node.blocked_retry_authorized":
                continue
            if event.get("node_id") != plan["node_id"]:
                continue
            payload = event.get("payload")
            cursor = event.get("cursor")
            if (
                isinstance(payload, Mapping)
                and type(cursor) is int
                and cursor > 0
                and payload.get("original_attempt") == plan["node_attempt"]
                and payload.get("next_attempt") == expected_attempt
                and payload.get("task_revision") == expected_revision
            ):
                return cursor
        return None

    def _matching_outer_intent(self, plan: Mapping[str, Any]) -> bool:
        """Require the exact outer intent before reconciling a retry event.

        ``retry_blocked_node`` predates recovery action ids, so its event does
        not carry ``request_id``.  The existing outer action row is therefore
        the required one-to-one binding between this local plan and that CAS;
        a merely similar retry event must never settle an unknown action.
        """

        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, node_id, node_attempt, action_fingerprint, action_input_json
                FROM node_recovery_actions WHERE action_key = ?
                """,
                (plan["request_id"],),
            ).fetchone()
        if row is None:
            return False
        try:
            stored_input = json.loads(str(row["action_input_json"]))
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            row["task_id"] == plan["task_id"]
            and row["node_id"] == plan["node_id"]
            and int(row["node_attempt"]) == plan["node_attempt"]
            and row["action_fingerprint"] == canonical_hash(dict(plan))
            and isinstance(stored_input, Mapping)
            and canonical_json(dict(stored_input)) == canonical_json(dict(plan))
        )

    @staticmethod
    def _resume_evidence(observation: Mapping[str, Any]) -> dict[str, Any]:
        phase = observation.get("phase")
        return {
            "phase": phase if isinstance(phase, str) else None,
            "readiness_ready": observation.get("readiness_ready") is True,
            "only_missing_dependency": observation.get("only_missing_dependency") is True,
            "executor_not_started": observation.get("executor_not_started") is True,
        }

    def _validated_plan(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _PLAN_KEYS:
            raise ValueError("local recovery plan has unsupported or missing fields")
        if raw.get("schema_version") != 1 or raw.get("kind") != "node-recovery-local/v1":
            raise ValueError("local recovery plan schema is unsupported")
        action = self._action(raw.get("action"))
        if raw.get("stage_key") != action:
            raise ValueError("local recovery plan stage key is invalid")
        identity = self._identity(raw)
        request_id = self._request_id(raw.get("request_id"))
        if type(raw.get("policy_revision")) is not int or raw["policy_revision"] < 1:
            raise ValueError("local recovery plan policy revision is invalid")
        policy_fingerprint = self._digest(raw.get("policy_fingerprint"), "policy_fingerprint")
        if not isinstance(raw.get("source"), Mapping) or not isinstance(raw.get("runtime"), Mapping):
            raise ValueError("local recovery plan bindings are invalid")
        evidence = raw.get("resume_evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "phase", "readiness_ready", "only_missing_dependency", "executor_not_started"
        }:
            raise ValueError("local recovery plan resume evidence is invalid")
        if evidence["phase"] is not None and not isinstance(evidence["phase"], str):
            raise ValueError("local recovery plan resume phase is invalid")
        if not all(isinstance(evidence[key], bool) for key in (
            "readiness_ready", "only_missing_dependency", "executor_not_started"
        )):
            raise ValueError("local recovery plan resume booleans are invalid")
        readiness_plan = raw.get("readiness_plan")
        if (action == "observe_readiness") != isinstance(readiness_plan, Mapping):
            raise ValueError("local recovery plan readiness binding is invalid")
        fingerprint = self._digest(raw.get("action_fingerprint"), "action_fingerprint")
        plan = {
            "schema_version": 1,
            "kind": "node-recovery-local/v1",
            "action": action,
            "stage_key": action,
            "request_id": request_id,
            **identity,
            "policy_revision": raw["policy_revision"],
            "policy_fingerprint": policy_fingerprint,
            "source": dict(raw["source"]),
            "runtime": dict(raw["runtime"]),
            "resume_evidence": dict(evidence),
            "readiness_plan": dict(readiness_plan) if isinstance(readiness_plan, Mapping) else None,
            "action_fingerprint": fingerprint,
        }
        if canonical_hash(self._unsigned_plan(plan)) != fingerprint:
            raise ValueError("local recovery plan fingerprint is invalid")
        return plan

    @staticmethod
    def _unsigned_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in plan.items() if key != "action_fingerprint"}

    @staticmethod
    def _receipt(
        plan: Mapping[str, Any],
        *,
        ok: bool,
        stage_succeeded: bool,
        known_effects: bool,
        evidence_refs: Mapping[str, str],
        observation_patch: Mapping[str, Any],
        reason_kind: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": plan["action"],
            "request_id": plan["request_id"],
            "ok": ok,
            "stage_succeeded": stage_succeeded,
            "known_effects": known_effects,
            "evidence_refs": dict(evidence_refs),
            "observation_patch": dict(observation_patch),
        }
        if reason_kind is not None:
            result["reason_kind"] = reason_kind
        return result

    def _needs_action(self, plan: Mapping[str, Any], reason_kind: str) -> dict[str, Any]:
        return self._receipt(
            plan,
            ok=False,
            stage_succeeded=False,
            known_effects=True,
            evidence_refs={},
            observation_patch={"needs_action": True},
            reason_kind=reason_kind,
        )

    def _unknown_receipt(self, plan: Mapping[str, Any], reason_kind: str) -> dict[str, Any]:
        return self._receipt(
            plan,
            ok=False,
            stage_succeeded=False,
            known_effects=False,
            evidence_refs={},
            observation_patch={},
            reason_kind=reason_kind,
        )

    @staticmethod
    def _identity(value: Mapping[str, Any]) -> dict[str, Any]:
        task_id = LocalNodeActions._text(value.get("task_id"), "task_id")
        node_id = LocalNodeActions._text(value.get("node_id"), "node_id")
        raw_attempt = value.get("node_attempt", value.get("attempt"))
        if "node_attempt" in value and "attempt" in value and value["node_attempt"] != value["attempt"]:
            raise ValueError("node_attempt and attempt disagree")
        if type(raw_attempt) is not int or raw_attempt < 0:
            raise ValueError("node_attempt must be a non-negative integer")
        revision = value.get("task_revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("task_revision must be a positive integer")
        return {
            "task_id": task_id,
            "node_id": node_id,
            "node_attempt": raw_attempt,
            "task_revision": revision,
        }

    @staticmethod
    def _node(task: Mapping[str, Any], node_id: str) -> dict[str, Any] | None:
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            return None
        for node in nodes:
            if isinstance(node, dict) and node.get("node_id") == node_id:
                return node
        return None

    @staticmethod
    def _action(value: object) -> str:
        if not isinstance(value, str) or value not in _ACTIONS:
            raise ValueError("unsupported local recovery action")
        return value

    @staticmethod
    def _request_id(value: object) -> str:
        text = LocalNodeActions._text(value, "request_id")
        if len(text) > _REQUEST_ID_MAX_LENGTH:
            raise ValueError("request_id exceeds 200 characters")
        return text

    @staticmethod
    def _digest(value: object, label: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or any(
            item not in "0123456789abcdef" for item in value
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"{label} must be a bounded non-empty string")
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError(f"{label} contains forbidden control characters")
        return value

    @staticmethod
    def _text_or_none(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _path_or_none(value: object) -> str | None:
        return str(value) if isinstance(value, Path) else None


__all__ = ["LocalNodeActions"]
