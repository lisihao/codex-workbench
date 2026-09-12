"""Prepare and journal the two bounded blocked-node recovery actions.

``JournaledNodeActions`` is an adapter, not a second scheduler or persistence
layer.  ``prepare`` obtains an existing read-only MCP preview through the
Authority service, and returns a JSON-safe :class:`RecoveryActionPlan`.
``execute`` uses that plan's stable request id with the same Authority
journal; ``reconcile`` only reads that request id and never treats absence as
permission to run it again.

The public receipt mapping contains ``journal_status`` (``completed``,
``executing``, or ``unknown``) and a parsed MCP business receipt only once the
journal is completed.  The adapter intentionally supports no arbitrary tool
or recovery action.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Any, TypedDict

from .authority_service import AuthorityService
from .config import WorkbenchConfig
from .controlled_validation import resolve_runtime
from .controlled_validation_service import PAIRING_WRITE, TOOL_NAME as VALIDATION_TOOL_NAME
from .model import canonical_hash, canonical_json
from .node_recovery_policy import VALIDATION_PROFILES
from .node_recovery_store import NodeRecoveryStore
from .store import StateConflictError


_ACTIONS = frozenset({"narrow_validation", "source_only_recovery"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECOVERY_REASON = {
    "narrow_validation": "bounded blocked-node narrow validation",
    "source_only_recovery": "bounded blocked-node source-only recovery",
}


class RecoveryActionPlan(TypedDict):
    """JSON-safe immutable input returned by :meth:`JournaledNodeActions.prepare`.

    ``fresh_preview`` is a bounded projection of the existing dry-run result,
    not the raw MCP response.  ``arguments`` are the exact non-dry-run
    Authority arguments, and ``action_fingerprint`` binds them to the
    task/node revision, policy snapshot, request id, and preview before an
    outer recovery-action intent persists the plan.
    """

    action: str
    request_id: str
    task_id: str
    node_id: str
    node_attempt: int
    task_revision: int
    stage_key: str
    action_fingerprint: str
    tool: str
    arguments: dict[str, Any]
    fresh_preview: dict[str, Any]
    policy_revision: int
    policy: dict[str, Any]
    validation_profile: str | None


class RecoveryActionReceipt(TypedDict):
    """Parsed Authority journal state returned by execute or reconcile."""

    action: str
    request_id: str
    journal_status: str
    known_effects: bool
    receipt: dict[str, Any] | None


class JournaledNodeActions:
    """Adapt two policy-authorized recovery actions to the Authority journal.

    @param config: The existing Authority configuration; retained so callers
        construct this adapter alongside the other Authority components.
    @param store: The existing node-recovery policy projection and base store.
    @param authority_service: The already-running Authority request service.
    """

    def __init__(
        self,
        config: WorkbenchConfig,
        store: NodeRecoveryStore,
        authority_service: AuthorityService,
    ) -> None:
        self.config = config
        self.store = store
        self.authority_service = authority_service

    def prepare(
        self,
        observation: dict[str, Any],
        action: str,
        request_id: str,
        *,
        validation_profile: str | None = None,
    ) -> RecoveryActionPlan:
        """Read policy/current state and obtain one fresh, non-mutating preview.

        ``validation_profile`` may select a normal narrow validation explicitly
        or rely on the observed profile.  The pairing-write profile must be
        explicitly selected because it is the only profile permitted to write
        its fixed pairing sidecars during execution.
        """

        action = self._action(action)
        request_id = self._request_id(request_id)
        identity = self._observation_identity(observation)
        policy_row = self.store.get_policy(identity["task_id"])
        policy = self._authorize(policy_row, action, validation_profile, observation)
        current = self._current_binding(identity)

        if action == "narrow_validation":
            profile = self._validation_profile(validation_profile, observation, policy)
            preview_arguments = {
                "task_id": identity["task_id"],
                "node_id": identity["node_id"],
                "expected_revision": identity["task_revision"],
                "expected_attempt": identity["node_attempt"],
                "worktree": self._worktree(current),
                "check_id": profile,
                "reason": _RECOVERY_REASON[action],
                "dry_run": True,
            }
            install_manifest_sha256 = self._install_manifest_sha256()
            preview = self._narrow_preview(
                self._preview(VALIDATION_TOOL_NAME, preview_arguments),
                preview_arguments["worktree"],
                install_manifest_sha256,
            )
            if self._install_manifest_sha256() != install_manifest_sha256:
                raise StateConflictError("Authority installation changed during validation preview")
            fingerprint = self._sha256(preview.get("fingerprint"), "validation preview fingerprint")
            arguments: dict[str, Any] = {
                **preview_arguments,
                "dry_run": False,
                "validation_id": request_id,
                "expected_fingerprint": fingerprint,
            }
            if profile == PAIRING_WRITE:
                # _validation_profile only permits this branch after an
                # explicit caller selection, never from an observation alone.
                arguments["confirm_pairing_write"] = True
            stage_key = f"narrow_validation:{profile}"
        else:
            profile = None
            preview_arguments = {
                "task_id": identity["task_id"],
                "node_id": identity["node_id"],
                "action": "resume",
                "expected_revision": identity["task_revision"],
                "expected_attempt": identity["node_attempt"],
                "reason": _RECOVERY_REASON[action],
                "source_only": True,
                "preserve_untracked": True,
                "confirm_source_only_extraction": True,
                "confirm_preserve_unknown_ignored": True,
                "dry_run": True,
            }
            preview = self._source_only_preview(
                self._preview("workbench_control_task", preview_arguments),
                observation,
            )
            source_delta = self._sha256(
                preview.get("source_delta_sha256"), "source-only preview source_delta_sha256"
            )
            arguments = {
                **preview_arguments,
                "dry_run": False,
                "expected_source_delta_sha256": source_delta,
            }
            stage_key = "source_only_recovery"

        plan: RecoveryActionPlan = {
            "action": action,
            "request_id": request_id,
            **identity,
            "stage_key": stage_key,
            "action_fingerprint": "",
            "tool": VALIDATION_TOOL_NAME if action == "narrow_validation" else "workbench_control_task",
            "arguments": arguments,
            "fresh_preview": preview,
            "policy_revision": self._policy_revision(policy_row),
            "policy": self._policy_binding(policy, action, profile),
            "validation_profile": profile,
        }
        plan["action_fingerprint"] = self._plan_fingerprint(plan)
        return plan

    def execute(self, plan: Mapping[str, Any]) -> RecoveryActionReceipt:
        """Dispatch one prepared action, or return its existing journal receipt.

        Existing receipts are read first: a source-only recovery can change the
        task revision, so a lost response must still return that same receipt
        rather than be rejected as a stale request or be invoked again.
        """

        normalized = self._validated_plan(plan)
        existing = self._journal_receipt_or_none(normalized["request_id"])
        if existing is not None:
            return self._receipt(normalized, existing)

        self._assert_plan_current(normalized)
        authority_receipt = self.authority_service.dispatch({
            "request_id": normalized["request_id"],
            "tool": normalized["tool"],
            "task_id": normalized["task_id"],
            "arguments": normalized["arguments"],
        })
        return self._receipt(normalized, authority_receipt)

    def reconcile(self, plan: Mapping[str, Any]) -> RecoveryActionReceipt | None:
        """Read exactly the prepared request id; absence is not a retry signal."""

        normalized = self._validated_plan(plan)
        authority_receipt = self._journal_receipt_or_none(normalized["request_id"])
        if authority_receipt is None:
            return None
        return self._receipt(normalized, authority_receipt)

    def _preview(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.authority_service.dispatch({"tool": tool, "arguments": arguments})
        if response.get("state") != "completed" or "result" not in response:
            raise StateConflictError("recovery preview did not complete")
        return self._business_receipt(response["result"])

    def _assert_plan_current(self, plan: RecoveryActionPlan) -> None:
        policy_row = self.store.get_policy(plan["task_id"])
        policy = self._authorize(
            policy_row,
            plan["action"],
            plan["validation_profile"],
            {},
        )
        if (
            self._policy_revision(policy_row) != plan["policy_revision"]
            or canonical_json(self._policy_binding(
                policy, plan["action"], plan["validation_profile"],
            )) != canonical_json(plan["policy"])
        ):
            raise StateConflictError("recovery policy changed after preview")
        if "install_manifest_sha256" in plan["fresh_preview"] and self._sha256(
            plan["fresh_preview"]["install_manifest_sha256"],
            "recovery preview install_manifest_sha256",
        ) != self._install_manifest_sha256():
            raise StateConflictError("Authority installation changed after recovery preview")
        runtime_fingerprint = plan["fresh_preview"].get("runtime_fingerprint")
        if (runtime_fingerprint is not None
                and runtime_fingerprint != canonical_hash(resolve_runtime(self.config).to_dict())):
            raise StateConflictError("validation runtime changed after recovery preview")
        self._current_binding({
            "task_id": plan["task_id"],
            "node_id": plan["node_id"],
            "node_attempt": plan["node_attempt"],
            "task_revision": plan["task_revision"],
        })

    def _validated_plan(self, raw: Mapping[str, Any]) -> RecoveryActionPlan:
        if not isinstance(raw, Mapping):
            raise ValueError("recovery action plan must be an object")
        required = {
            "action", "request_id", "task_id", "node_id", "node_attempt", "task_revision",
            "stage_key", "action_fingerprint", "tool", "arguments", "fresh_preview",
            "policy_revision", "policy", "validation_profile",
        }
        if set(raw) != required:
            raise ValueError("recovery action plan has unsupported or missing fields")
        action = self._action(raw["action"])
        request_id = self._request_id(raw["request_id"])
        identity = self._observation_identity({
            "task_id": raw["task_id"],
            "node_id": raw["node_id"],
            "node_attempt": raw["node_attempt"],
            "task_revision": raw["task_revision"],
        })
        stage_key = self._text(raw["stage_key"], "stage_key")
        fingerprint = self._sha256(raw["action_fingerprint"], "action_fingerprint")
        tool = self._text(raw["tool"], "tool")
        if not isinstance(raw["arguments"], dict) or not isinstance(raw["fresh_preview"], dict):
            raise ValueError("recovery action plan arguments and fresh_preview must be objects")
        if type(raw["policy_revision"]) is not int or raw["policy_revision"] < 0:
            raise ValueError("policy_revision must be a non-negative integer")
        if not isinstance(raw["policy"], dict):
            raise ValueError("policy must be an object")
        profile = raw["validation_profile"]
        if profile is not None:
            profile = self._text(profile, "validation_profile")

        expected_tool = VALIDATION_TOOL_NAME if action == "narrow_validation" else "workbench_control_task"
        if tool != expected_tool:
            raise ValueError("recovery action plan tool does not match action")
        expected_stage = f"narrow_validation:{profile}" if action == "narrow_validation" else "source_only_recovery"
        if profile is None and action == "narrow_validation":
            raise ValueError("narrow validation plan requires validation_profile")
        if action == "source_only_recovery" and profile is not None:
            raise ValueError("source-only recovery plan cannot select validation_profile")
        if profile is not None and profile not in VALIDATION_PROFILES:
            raise ValueError("recovery action plan has an unsupported validation_profile")
        if stage_key != expected_stage:
            raise ValueError("recovery action plan stage_key does not match action")
        if raw["policy"] != self._policy_binding(
            {"enabled": True}, action, profile,
        ):
            raise ValueError("recovery action plan policy binding is invalid")
        preview_keys = set(raw["fresh_preview"])
        if action == "narrow_validation":
            if preview_keys != {
                "worktree", "fingerprint", "source_delta_sha256", "install_manifest_sha256", "runtime_fingerprint",
            }:
                raise ValueError("narrow validation plan preview is not bounded")
        elif ("source_delta_sha256" not in preview_keys or preview_keys - {
            "source_delta_sha256", "install_manifest_sha256", "runtime_fingerprint",
        }):
            raise ValueError("source-only recovery plan preview is not bounded")

        plan: RecoveryActionPlan = {
            "action": action,
            "request_id": request_id,
            **identity,
            "stage_key": stage_key,
            "action_fingerprint": fingerprint,
            "tool": tool,
            "arguments": dict(raw["arguments"]),
            "fresh_preview": dict(raw["fresh_preview"]),
            "policy_revision": raw["policy_revision"],
            "policy": dict(raw["policy"]),
            "validation_profile": profile,
        }
        expected_arguments = self._execution_arguments(plan)
        if canonical_json(plan["arguments"]) != canonical_json(expected_arguments):
            raise ValueError("recovery action plan arguments are not bound to its fresh preview")
        if self._plan_fingerprint({**plan, "action_fingerprint": ""}) != fingerprint:
            raise ValueError("recovery action plan fingerprint is invalid")
        return plan

    def _execution_arguments(self, plan: RecoveryActionPlan) -> dict[str, Any]:
        if plan["action"] == "narrow_validation":
            profile = plan["validation_profile"]
            assert profile is not None
            fingerprint = self._sha256(
                plan["fresh_preview"].get("fingerprint"), "validation preview fingerprint"
            )
            arguments: dict[str, Any] = {
                "task_id": plan["task_id"],
                "node_id": plan["node_id"],
                "expected_revision": plan["task_revision"],
                "expected_attempt": plan["node_attempt"],
                "worktree": self._text(plan["fresh_preview"].get("worktree"), "preview worktree"),
                "check_id": profile,
                "reason": _RECOVERY_REASON["narrow_validation"],
                "dry_run": False,
                "validation_id": plan["request_id"],
                "expected_fingerprint": fingerprint,
            }
            if profile == PAIRING_WRITE:
                arguments["confirm_pairing_write"] = True
            return arguments
        source_delta = self._sha256(
            plan["fresh_preview"].get("source_delta_sha256"),
            "source-only preview source_delta_sha256",
        )
        return {
            "task_id": plan["task_id"],
            "node_id": plan["node_id"],
            "action": "resume",
            "expected_revision": plan["task_revision"],
            "expected_attempt": plan["node_attempt"],
            "reason": _RECOVERY_REASON["source_only_recovery"],
            "source_only": True,
            "preserve_untracked": True,
            "confirm_source_only_extraction": True,
            "confirm_preserve_unknown_ignored": True,
            "dry_run": False,
            "expected_source_delta_sha256": source_delta,
        }

    def _current_binding(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        task = self.store.base_store.get_task(identity["task_id"])
        if task.get("state") in {"paused", "cancelled"}:
            raise StateConflictError("recovery task is paused or cancelled")
        if task.get("state") != "blocked":
            raise StateConflictError("recovery requires a blocked task")
        if task.get("state_revision") != identity["task_revision"]:
            raise StateConflictError("recovery task revision changed after observation")
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            raise StateConflictError("recovery task nodes are invalid")
        node = next((item for item in nodes if isinstance(item, dict) and item.get("node_id") == identity["node_id"]), None)
        if not isinstance(node, dict):
            raise StateConflictError("recovery node no longer exists")
        if node.get("state") != "blocked":
            raise StateConflictError("recovery requires a blocked node")
        if node.get("attempt") != identity["node_attempt"]:
            raise StateConflictError("recovery node attempt changed after observation")
        return node

    def _authorize(
        self,
        policy_row: Mapping[str, Any],
        action: str,
        validation_profile: str | None,
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        policy = policy_row.get("policy")
        if not isinstance(policy, dict):
            raise StateConflictError("recovery policy is invalid")
        if policy.get("enabled") is not True:
            raise StateConflictError("recovery policy is disabled")
        actions = policy.get("allowed_actions")
        if not isinstance(actions, list) or action not in actions:
            raise StateConflictError("recovery action is not authorized by policy")
        if action == "narrow_validation":
            profile = validation_profile
            if profile is None:
                candidate = observation.get("validation_profile")
                profile = candidate if isinstance(candidate, str) else None
            if profile not in VALIDATION_PROFILES:
                raise StateConflictError("validation profile is not an existing bounded profile")
            profiles = policy.get("validation_profiles")
            if not isinstance(profiles, list) or profile not in profiles:
                raise StateConflictError("validation profile is not authorized by policy")
        return dict(policy)

    def _validation_profile(
        self,
        explicit: str | None,
        observation: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> str:
        profile = explicit
        if profile is not None:
            profile = self._text(profile, "validation_profile")
        else:
            candidate = observation.get("validation_profile")
            profile = candidate if isinstance(candidate, str) else None
        if profile not in VALIDATION_PROFILES:
            raise StateConflictError("validation profile is not an existing bounded profile")
        profiles = policy.get("validation_profiles")
        if not isinstance(profiles, list) or profile not in profiles:
            raise StateConflictError("validation profile is not authorized by policy")
        if profile == PAIRING_WRITE and explicit != PAIRING_WRITE:
            raise StateConflictError("pairing-write requires an explicit validation_profile")
        return profile

    def _receipt(
        self,
        plan: RecoveryActionPlan,
        authority_receipt: Mapping[str, Any],
    ) -> RecoveryActionReceipt:
        state = authority_receipt.get("state")
        if state not in {"completed", "executing", "unknown"}:
            raise RuntimeError("Authority journal returned an invalid state")
        result: dict[str, Any] | None = None
        if state == "completed":
            if "result" not in authority_receipt:
                raise RuntimeError("completed Authority receipt has no result")
            result = self._completed_receipt(
                plan,
                self._business_receipt(authority_receipt["result"]),
            )
        return {
            "action": plan["action"],
            "request_id": plan["request_id"],
            "journal_status": state,
            "known_effects": state == "completed",
            "receipt": result,
        }

    def _journal_receipt_or_none(self, request_id: str) -> dict[str, Any] | None:
        try:
            return self.authority_service.get_request(request_id)
        except KeyError:
            return None

    @staticmethod
    def _business_receipt(raw: object) -> dict[str, Any]:
        if isinstance(raw, dict) and isinstance(raw.get("content"), list):
            content = raw["content"]
            texts = [
                item.get("text")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            if len(texts) == 1:
                try:
                    parsed = json.loads(texts[0])
                except json.JSONDecodeError:
                    if raw.get("isError") is True:
                        return {"ok": False, "error": texts[0]}
                    raise StateConflictError("Authority MCP result is not a JSON business receipt")
                if isinstance(parsed, dict):
                    return parsed
        if isinstance(raw, dict):
            return dict(raw)
        raise StateConflictError("Authority result is not an object")

    def _narrow_preview(
        self,
        raw: Mapping[str, Any],
        worktree: str,
        install_manifest_sha256: str,
    ) -> dict[str, Any]:
        """Keep only the fixed validation fields safe for an action intent."""
        plan = raw.get("plan")
        runtime = plan.get("runtime") if isinstance(plan, Mapping) else None
        return {
            "worktree": worktree,
            "fingerprint": self._sha256(raw.get("fingerprint"), "validation preview fingerprint"),
            "source_delta_sha256": self._sha256(
                raw.get("source_delta_sha256"), "validation preview source_delta_sha256"
            ),
            "install_manifest_sha256": install_manifest_sha256,
            "runtime_fingerprint": canonical_hash(runtime) if isinstance(runtime, Mapping) else None,
        }

    def _source_only_preview(
        self,
        raw: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind source-only apply to a current dry-run and prior validation facts."""

        source_delta = self._sha256(
            raw.get("source_delta_sha256"), "source-only preview source_delta_sha256"
        )
        preview = {"source_delta_sha256": source_delta}
        if "validated_source_delta" in observation:
            validated = self._sha256(
                observation["validated_source_delta"], "observation.validated_source_delta"
            )
            if validated != source_delta:
                raise StateConflictError("source-only preview source delta differs from validated source")
        if "validated_install_manifest" in observation:
            validated_install = self._sha256(
                observation["validated_install_manifest"], "observation.validated_install_manifest"
            )
            current_install = self._install_manifest_sha256()
            if validated_install != current_install:
                raise StateConflictError("Authority installation changed after validation")
            preview["install_manifest_sha256"] = current_install
        prior_runtime = observation.get("validated_runtime_fingerprint")
        if prior_runtime is not None:
            current_runtime = canonical_hash(resolve_runtime(self.config).to_dict())
            if self._sha256(prior_runtime, "validated_runtime_fingerprint") != current_runtime:
                raise StateConflictError("validation runtime changed after the prior checks")
            preview["runtime_fingerprint"] = current_runtime
        return preview

    def _completed_receipt(
        self,
        plan: RecoveryActionPlan,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return the small action receipt safe to persist beside an intent."""

        succeeded = raw.get("ok") is True
        result: dict[str, Any] = {
            "ok": succeeded,
            "known_effects": True,
            "stage_succeeded": succeeded,
        }
        if not succeeded:
            error = raw.get("error")
            if isinstance(error, str):
                result["error"] = self._receipt_text(error)
            return result
        if plan["action"] == "narrow_validation":
            source_delta = self._sha256(
                raw.get("source_delta_after"), "validation result source_delta_after"
            )
            audit_ref = self._text(raw.get("audit_ref"), "validation result audit_ref")
            profile = plan["validation_profile"]
            assert profile is not None
            fingerprint = self._sha256(raw.get("fingerprint"), "validation result fingerprint")
            if fingerprint != plan["fresh_preview"]["fingerprint"]:
                raise StateConflictError("validation result does not match its fresh preview")
            result.update({
                "validation_id": plan["request_id"],
                "fingerprint": fingerprint,
                "validation_audit_ref": audit_ref,
                "validated_source_delta": source_delta,
                "observation_patch": {
                    "last_validation_profile": profile,
                    "validated_source_delta": source_delta,
                    "validation_audit_ref": audit_ref,
                    "validated_install_manifest": plan["fresh_preview"]["install_manifest_sha256"],
                    "validated_runtime_fingerprint": plan["fresh_preview"]["runtime_fingerprint"],
                },
            })
            return result
        source_delta = self._sha256(
            raw.get("source_delta_sha256"), "source-only result source_delta_sha256"
        )
        if source_delta != plan["fresh_preview"]["source_delta_sha256"]:
            raise StateConflictError("source-only result does not match its fresh preview")
        result.update({
            "source_delta_sha256": source_delta,
            "recovery_authorization": "source_only_extraction",
            "historical_effects": "unknown",
            "observation_patch": {"recovery_resumed": True},
        })
        return result

    def _install_manifest_sha256(self) -> str:
        try:
            return sha256(self.config.install_manifest.read_bytes()).hexdigest()
        except OSError as error:
            raise StateConflictError("Authority installation manifest is unavailable") from error

    @staticmethod
    def _policy_binding(
        policy: Mapping[str, Any],
        action: str,
        validation_profile: str | None,
    ) -> dict[str, Any]:
        """Persist only the authorization facts needed to re-fence this action."""

        return {
            "enabled": policy.get("enabled") is True,
            "action": action,
            "validation_profile": validation_profile,
        }

    @staticmethod
    def _receipt_text(value: str) -> str:
        text = value.strip()
        if not text:
            return "Authority action failed"
        return text[:512]

    @staticmethod
    def _worktree(node: Mapping[str, Any]) -> str:
        worktree = node.get("worktree")
        if not isinstance(worktree, str) or not worktree or not worktree.startswith("/"):
            raise StateConflictError("blocked recovery node has no absolute worktree")
        return worktree

    @staticmethod
    def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
        return canonical_hash({
            "version": "journaled-node-actions/v1",
            "action": plan["action"],
            "request_id": plan["request_id"],
            "task_id": plan["task_id"],
            "node_id": plan["node_id"],
            "node_attempt": plan["node_attempt"],
            "task_revision": plan["task_revision"],
            "stage_key": plan["stage_key"],
            "tool": plan["tool"],
            "arguments": plan["arguments"],
            "fresh_preview": plan["fresh_preview"],
            "policy_revision": plan["policy_revision"],
            "policy": plan["policy"],
            "validation_profile": plan["validation_profile"],
        })

    @staticmethod
    def _observation_identity(observation: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(observation, Mapping):
            raise ValueError("recovery observation must be an object")
        task_id = JournaledNodeActions._text(observation.get("task_id"), "observation.task_id")
        node_id = JournaledNodeActions._text(observation.get("node_id"), "observation.node_id")
        attempt = observation.get("node_attempt", observation.get("attempt"))
        if "node_attempt" in observation and "attempt" in observation and observation["node_attempt"] != observation["attempt"]:
            raise ValueError("observation node_attempt does not match attempt")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("observation node_attempt must be a positive integer")
        revision = observation.get("task_revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("observation task_revision must be a non-negative integer")
        return {
            "task_id": task_id,
            "node_id": node_id,
            "node_attempt": attempt,
            "task_revision": revision,
        }

    @staticmethod
    def _policy_revision(policy_row: Mapping[str, Any]) -> int:
        value = policy_row.get("policy_revision")
        if type(value) is not int or value < 0:
            raise StateConflictError("recovery policy revision is invalid")
        return value

    @staticmethod
    def _action(value: object) -> str:
        action = JournaledNodeActions._text(value, "action")
        if action not in _ACTIONS:
            raise ValueError("unsupported journaled recovery action")
        return action

    @staticmethod
    def _request_id(value: object) -> str:
        request_id = JournaledNodeActions._text(value, "request_id")
        if len(request_id) > 200 or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in request_id):
            raise ValueError("request_id is invalid")
        return request_id

    @staticmethod
    def _sha256(value: object, label: str) -> str:
        text = JournaledNodeActions._text(value, label)
        if _SHA256.fullmatch(text) is None:
            raise StateConflictError(f"{label} must be a lowercase SHA-256 digest")
        return text

    @staticmethod
    def _text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError(f"{label} contains forbidden control characters")
        return value
