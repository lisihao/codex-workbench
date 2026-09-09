"""Authority-owned adapters for durable delivery objectives.

This module composes the existing lifecycle reconciler with the existing
GitHub delivery implementation.  It deliberately has no objective-creation
or authorization API: the SQLite store remains the only authority for both.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Protocol

from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .delivery import GitHubDelivery, GitHubDeliveryStageAdapter
from .deployment_adapter import build_authority_deployment_stage_adapter
from .delivery_lifecycle import (
    CompositeDeliveryStageAdapter,
    DeliveryLifecycleReconciler,
    DeliveryStageContext,
    DeliveryStageOutcome,
    required_completion_identities,
)
from .model import canonical_hash
from .store import StateConflictError, WorkbenchStore


_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_OBSERVATION_TIMEOUT_SECONDS = 8
_OBSERVATION_WAKE_SECONDS = 1


@dataclass(frozen=True)
class VerificationObservation:
    """Read-only source and toolchain observations for an accepted verifier."""

    identities: Mapping[str, Any]
    receipt: Mapping[str, Any]


class VerificationIdentityObserver(Protocol):
    """Collect actual verifier-adjacent identities without running project work."""

    def observe(
        self,
        task: Mapping[str, Any],
        verifier: Mapping[str, Any],
        required_runtimes: tuple[str, ...],
    ) -> VerificationObservation: ...


class BoundedVerificationIdentityObserver:
    """Probe only explicitly requested authority toolchains.

    The installer pins the Node executable in ``worktree_recovery`` and the
    pnpm launcher in ``CODEX_WORKBENCH_PNPM``.  This observer never falls back
    to ``PATH`` and every subprocess has a small timeout.  Missing tools are
    recorded as unavailable observations, not invented as identities.
    """

    def __init__(
        self,
        config: WorkbenchConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = _OBSERVATION_TIMEOUT_SECONDS,
    ):
        if timeout_seconds <= 0:
            raise ValueError("verification observation timeout_seconds must be positive")
        self.config = config
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def observe(
        self,
        _task: Mapping[str, Any],
        _verifier: Mapping[str, Any],
        required_runtimes: tuple[str, ...],
    ) -> VerificationObservation:
        identities: dict[str, Any] = {}
        receipt: dict[str, Any] = {"toolchain": {}}
        recovery = self._recovery_config(receipt)

        if "node" in required_runtimes:
            node = self._configured_executable(
                recovery.get("pnpm_node_binary"),
                label="configured Node executable",
            )
            node_identity, node_receipt = self._version_identity("node", node)
            receipt["toolchain"]["node"] = node_receipt
            if node_identity is not None:
                identities["node"] = node_identity
        else:
            receipt["toolchain"]["node"] = {"status": "not-requested"}

        if "pnpm" in required_runtimes:
            configured_pnpm = os.environ.get("CODEX_WORKBENCH_PNPM") or recovery.get("pnpm_binary")
            pnpm = self._configured_executable(configured_pnpm, label="configured pnpm launcher")
            pnpm_identity, pnpm_receipt = self._version_identity("pnpm", pnpm)
            receipt["toolchain"]["pnpm"] = pnpm_receipt
            if pnpm_identity is not None:
                identities["pnpm"] = pnpm_identity
        else:
            receipt["toolchain"]["pnpm"] = {"status": "not-requested"}

        return VerificationObservation(identities=identities, receipt=receipt)

    def _recovery_config(self, receipt: dict[str, Any]) -> dict[str, object]:
        try:
            raw = json.loads(self.config.config_file.read_text())
        except FileNotFoundError:
            receipt["config"] = {"status": "unavailable", "reason": "authority config is absent"}
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            receipt["config"] = {
                "status": "unavailable",
                "reason": f"cannot read authority config: {type(error).__name__}: {error}",
            }
            return {}
        if not isinstance(raw, dict):
            receipt["config"] = {"status": "unavailable", "reason": "authority config is not an object"}
            return {}
        recovery = raw.get("worktree_recovery")
        if recovery is None:
            receipt["config"] = {
                "status": "unavailable",
                "reason": "worktree recovery launcher configuration is absent",
            }
            return {}
        if not isinstance(recovery, dict):
            receipt["config"] = {
                "status": "unavailable",
                "reason": "worktree recovery launcher configuration is invalid",
            }
            return {}
        receipt["config"] = {"status": "observed"}
        return dict(recovery)

    @staticmethod
    def _configured_executable(value: object, *, label: str) -> tuple[Path | None, str | None]:
        if not isinstance(value, str) or not value.strip():
            return None, f"{label} is not configured"
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None, f"{label} must be an absolute path"
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            return None, f"{label} is unavailable: {type(error).__name__}: {error}"
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return None, f"{label} is not executable"
        return resolved, None

    def _version_identity(
        self,
        name: str,
        configured: tuple[Path | None, str | None],
    ) -> tuple[dict[str, str] | None, dict[str, str]]:
        executable, problem = configured
        if executable is None:
            return None, {"status": "unavailable", "reason": str(problem)}
        try:
            result = self.runner(
                [str(executable), "--version"],
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, {
                "status": "unavailable",
                "path": str(executable),
                "reason": f"{type(error).__name__}: {error}",
            }
        version = result.stdout.strip()
        if result.returncode != 0 or not version:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            return None, {
                "status": "unavailable",
                "path": str(executable),
                "reason": detail[:512],
            }
        identity = {"path": str(executable), "version": version}
        return identity, {"status": "observed", **identity, "name": name}


class TaskDeliveryStageAdapter:
    """Observe task state and resume only bounded safe implementation failures."""

    def __init__(self, store: WorkbenchStore, observer: VerificationIdentityObserver):
        self.store = store
        self.observer = observer

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        if context.stage not in {"plan", "implement", "verify"}:
            raise ValueError("task lifecycle adapter only observes plan, implement, and verify")
        task = context.task
        if not isinstance(task, Mapping):
            planning = self.store.get_planning_request_for_task(str(context.objective["task_id"]))
            if planning is not None and planning["state"] == "failed":
                error = str(planning.get("error") or "")
                limit = context.objective["budget"]["attempt_limit"]
                if error.startswith(("PlannerError:", "ValueError:")) and planning["attempt"] < limit:
                    try:
                        self.store.retry_planning_request(planning["command_id"], expected_attempt=planning["attempt"],
                                                          max_attempts=limit, reason="repair previous planner validation failure",
                                                          coordinator_epoch=context.objective["lease"]["coordinator_epoch"],
                                                          objective_id=context.objective["objective_id"],
                                                          objective_lease_epoch=context.objective["lease"]["lease_epoch"])
                    except StateConflictError as conflict:
                        current = self.store.get_planning_request_for_task(str(context.objective["task_id"]))
                        if current is not None and (current["state"] != "failed" or current["attempt"] != planning["attempt"]):
                            return self._deferred(context, "another coordinator already advanced the planning reservation")
                        return DeliveryStageOutcome(receipt_id=f"{context.dispatch_id}:planning-retry-blocked", status="blocked",
                            failure={"kind": "invalid-planning-scopes", "detail": str(conflict)}, retry_eligible=False)
                    return self._deferred(context, "planning validation failure queued for bounded repair")
                return DeliveryStageOutcome(receipt_id=f"{context.dispatch_id}:planning-failed", status="blocked",
                    failure={"kind": "invalid-planning-scopes", "detail": "planning retry budget exhausted or failure requires a specific external recovery"},
                    retry_eligible=False, receipt={"planning_command_id": planning["command_id"], "error": error})
            return self._deferred(context, "the planning reservation has not materialized a task")
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            return self._deferred(context, "materialized task has no durable node list")
        normalized_nodes = [node for node in nodes if isinstance(node, Mapping)]
        if len(normalized_nodes) != len(nodes):
            return self._deferred(context, "materialized task has an invalid durable node record")
        if context.stage == "plan":
            return self._planned(context, task, normalized_nodes)
        if context.stage == "implement":
            return self._implemented(context, task, normalized_nodes)
        return self._verified(context, task, normalized_nodes)

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        """Repeat only SQLite and bounded read-only observations after restart."""

        return self.execute_stage(context)

    def _planned(
        self,
        context: DeliveryStageContext,
        task: Mapping[str, Any],
        nodes: list[Mapping[str, Any]],
    ) -> DeliveryStageOutcome:
        contract = task.get("contract")
        if not isinstance(contract, Mapping):
            return self._deferred(context, "materialized task has no durable contract")
        receipt = {
            "task_id": task.get("task_id"),
            "task_state": task.get("state"),
            "task_revision": task.get("state_revision"),
            "contract_hash": task.get("contract_hash"),
            "frozen_base_sha": contract.get("base_sha"),
            "nodes": [self._node_summary(node) for node in nodes],
        }
        return self._succeeded(context, receipt)

    def _implemented(
        self,
        context: DeliveryStageContext,
        task: Mapping[str, Any],
        nodes: list[Mapping[str, Any]],
    ) -> DeliveryStageOutcome:
        repair = self._resume_retryable_implementation(context, task, nodes)
        if repair["status"] in {"queued", "raced"}:
            return self._deferred(
                context,
                "eligible implementation repair is queued or was claimed by another coordinator",
                {"internal_repair": repair},
            )
        if repair["status"] == "ineligible":
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:task-implement-recovery-blocked",
                status="blocked",
                receipt={"internal_repair": repair},
                failure={
                    "kind": repair["failure_kind"],
                    "detail": repair["reason"],
                },
                retry_eligible=False,
            )

        workers = [node for node in nodes if node.get("verifier") is not True]
        unresolved = [
            self._node_summary(node)
            for node in workers
            if node.get("state") not in {"accepted", "succeeded"}
        ]
        if unresolved:
            return self._deferred(
                context,
                "implementation workers have not reached a durable accepted result",
                {"unresolved_nodes": unresolved},
            )
        receipt = {
            "task_id": task.get("task_id"),
            "task_state": task.get("state"),
            "task_revision": task.get("state_revision"),
            "workers": [self._node_summary(node) for node in workers],
        }
        return self._succeeded(context, receipt)

    def _resume_retryable_implementation(
        self,
        context: DeliveryStageContext,
        task: Mapping[str, Any],
        nodes: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if task.get("state") != "needs_fix":
            return {"status": "not-needed"}
        contract = task.get("contract")
        budget = context.objective.get("budget")
        if not isinstance(contract, Mapping) or not isinstance(budget, Mapping):
            return self._ineligible_repair("task or delivery retry budget is unavailable")
        task_limit = contract.get("retry_limit")
        objective_limit = budget.get("attempt_limit")
        if (
            isinstance(task_limit, bool)
            or not isinstance(task_limit, int)
            or isinstance(objective_limit, bool)
            or not isinstance(objective_limit, int)
        ):
            return self._ineligible_repair("task or delivery retry limit is invalid")
        retry_limit = min(task_limit, objective_limit)
        if retry_limit <= 0:
            return self._ineligible_repair("the strict task/delivery retry limit is exhausted")
        if contract.get("external_write_permission") is True or contract.get("destructive_action_permission") is True:
            return self._ineligible_repair("automatic repair cannot requeue a task with external or destructive authority")
        eligible: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for node in nodes:
            state = node.get("state")
            if state in {"blocked", "indeterminate"}:
                return self._ineligible_repair(
                    "a blocked or indeterminate node has unknown side effects",
                    failure_kind="unknown-effects",
                )
            if state != "failed":
                continue
            result = node.get("result")
            if node.get("verifier") is True:
                return self._ineligible_repair("a verifier failure requires an explicit recovery decision")
            if not isinstance(result, Mapping) or result.get("status") != "failed" or result.get("retryable") is not True:
                return self._ineligible_repair("a failed worker is not a retryable known failure")
            attempt = node.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt >= retry_limit:
                return self._ineligible_repair("a failed worker reached the strict task/delivery retry cap")
            eligible.append((node, result))
        if not eligible:
            return self._ineligible_repair("needs_fix has no retryable failed worker")
        diagnostics: list[str] = []
        for node, result in eligible:
            summary = result.get("summary")
            compact = str(summary)[:240] if isinstance(summary, str) else "no recorded summary"
            node_id = node.get("node_id")
            attempt = node.get("attempt")
            diagnostics.append(f"{node_id} attempt {attempt}: {compact}")
        instruction = (
            "Automatic delivery-objective recovery for retryable implementation failures. "
            "Repair only the recorded failure within the existing task scope; do not deliver externally. "
            "Recorded diagnostics: " + "; ".join(diagnostics)
        )
        try:
            queued = self.store.queue_task_with_instruction(
                str(task["task_id"]),
                instruction,
                expected_revision=int(task["state_revision"]),
            )
        except StateConflictError as error:
            detail = str(error)
            if "expected task revision" in detail or "compare-and-set" in detail:
                return {"status": "raced", "reason": detail[:512]}
            return self._ineligible_repair(detail)
        except (KeyError, ValueError) as error:
            return self._ineligible_repair(f"cannot queue bounded recovery: {type(error).__name__}: {error}")
        return {
            "status": "queued",
            "task_id": task["task_id"],
            "task_revision": queued.get("revision"),
            "retry_limit": retry_limit,
            "failed_nodes": [self._node_summary(node) for node, _result in eligible],
        }

    @staticmethod
    def _ineligible_repair(
        reason: str,
        *,
        failure_kind: str = "verification-failure",
    ) -> dict[str, str]:
        return {"status": "ineligible", "reason": reason, "failure_kind": failure_kind}

    def _verified(
        self,
        context: DeliveryStageContext,
        task: Mapping[str, Any],
        nodes: list[Mapping[str, Any]],
    ) -> DeliveryStageOutcome:
        verifier = next((node for node in nodes if node.get("verifier") is True), None)
        if task.get("state") != "accepted" or verifier is None or verifier.get("state") != "accepted":
            return self._deferred(
                context,
                "the independent verifier has not accepted the task",
                {
                    "task_state": task.get("state"),
                    "verifier": self._node_summary(verifier) if verifier is not None else None,
                },
            )
        try:
            observation = self.observer.observe(
                task,
                verifier,
                self._required_runtimes(context),
            )
        except Exception as error:
            return self._deferred(
                context,
                f"bounded verification identity observation failed: {type(error).__name__}: {error}",
            )
        identities = dict(observation.identities)
        requested = context.objective.get("identities")
        frozen = dict(requested) if isinstance(requested, Mapping) else {}
        mismatches = {
            name: {"requested": frozen[name], "observed": value}
            for name, value in identities.items()
            if name in frozen and frozen[name] != value
        }
        receipt = {
            "task_id": task.get("task_id"),
            "task_state": task.get("state"),
            "task_revision": task.get("state_revision"),
            "verifier": self._node_summary(verifier),
            "observation": dict(observation.receipt),
            "frozen_base_sha": (
                task.get("contract", {}).get("base_sha")
                if isinstance(task.get("contract"), Mapping)
                else None
            ),
            "accepted_verifier_artifacts": (
                dict(verifier["result"].get("artifacts", {}))
                if isinstance(verifier.get("result"), Mapping)
                and isinstance(verifier["result"].get("artifacts"), Mapping)
                else {}
            ),
            "accepted_verifier_evidence": (
                list(verifier["result"].get("evidence", ()))
                if isinstance(verifier.get("result"), Mapping)
                and isinstance(verifier["result"].get("evidence"), list)
                else []
            ),
        }
        if mismatches:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:task-verify-mismatch",
                status="failed",
                receipt={**receipt, "identity_mismatches": mismatches},
                failure={
                    "kind": "verification-failure",
                    "detail": "observed verifier identities differ from the frozen delivery objective",
                },
                retry_eligible=False,
            )
        return self._succeeded(context, receipt, identities=identities)

    @staticmethod
    def _required_runtimes(context: DeliveryStageContext) -> tuple[str, ...]:
        return tuple(
            name
            for name in required_completion_identities(context.objective)
            if name in {"node", "pnpm"}
        )

    @staticmethod
    def _node_summary(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if node is None:
            return None
        return {
            "node_id": node.get("node_id"),
            "state": node.get("state"),
            "attempt": node.get("attempt"),
            "settled_at": node.get("settled_at"),
        }

    def _succeeded(
        self,
        context: DeliveryStageContext,
        receipt: Mapping[str, Any],
        *,
        identities: Mapping[str, Any] | None = None,
    ) -> DeliveryStageOutcome:
        document = dict(receipt)
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:task-{context.stage}",
            receipt=document,
            evidence_fingerprint=canonical_hash(
                {
                    "objective_id": context.objective["objective_id"],
                    "stage": context.stage,
                    "task_observation": document,
                }
            ),
            identities=dict(identities or {}),
        )

    @staticmethod
    def _deferred(
        context: DeliveryStageContext,
        detail: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> DeliveryStageOutcome:
        wakeup = (datetime.now(UTC) + timedelta(seconds=_OBSERVATION_WAKE_SECONDS)).isoformat(
            timespec="seconds"
        )
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:task-{context.stage}-deferred",
            status="deferred",
            receipt={"observation": "pending", **dict(receipt or {})},
            failure={"kind": "execution-environment", "detail": detail},
            retry_eligible=True,
            next_wakeup_at=wakeup,
        )


class RestrictedGitHubDeliveryStageAdapter:
    """Defence-in-depth bridge that requires the store's scoped authorization."""

    def __init__(self, adapter: GitHubDeliveryStageAdapter):
        self.adapter = adapter

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        if not isinstance(context.authorization, Mapping):
            return self._denied(context)
        return self._with_build_identity(context, self.adapter.execute_stage(context))

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        if not isinstance(context.authorization, Mapping):
            return self._denied(context)
        return self._with_build_identity(context, self.adapter.reconcile_stage(context))

    @staticmethod
    def _denied(context: DeliveryStageContext) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:github-authorization",
            status="denied",
            receipt={"authorization": "missing"},
            failure={
                "kind": "permission-denied",
                "detail": "GitHub lifecycle execution requires an existing scoped store authorization",
            },
            retry_eligible=False,
        )

    @staticmethod
    def _with_build_identity(
        context: DeliveryStageContext,
        outcome: DeliveryStageOutcome,
    ) -> DeliveryStageOutcome:
        if outcome.status != "succeeded":
            return outcome
        receipt = dict(outcome.receipt)
        github_delivery = receipt.get("github_delivery")
        details = github_delivery.get("details") if isinstance(github_delivery, Mapping) else None
        commit = details.get("commit") if isinstance(details, Mapping) else None
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit.lower()) is None:
            return outcome
        build: dict[str, Any] = {"integration_commit": commit}
        if isinstance(details, Mapping):
            for name in ("push_log",):
                value = details.get(name)
                if isinstance(value, str) and value:
                    build[name] = value
        observed = {"source": commit, "build": build}
        requested = context.objective.get("identities")
        frozen = dict(requested) if isinstance(requested, Mapping) else {}
        mismatches = {
            name: {"requested": frozen[name], "observed": value}
            for name, value in observed.items()
            if name in frozen and frozen[name] != value
        }
        if mismatches:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:github-identity-mismatch",
                status="failed",
                receipt={**receipt, "observed_integration_commit": commit, "identity_mismatches": mismatches},
                failure={
                    "kind": "verification-failure",
                    "detail": "GitHub integration receipt differs from frozen delivery identities",
                },
                retry_eligible=False,
            )
        identities = {**dict(outcome.identities), **observed}
        return replace(
            outcome,
            receipt={**receipt, "observed_integration_commit": commit, "build_evidence": build},
            identities=identities,
        )

class UnavailableExternalStageAdapter:
    """State explicitly that deployment verification has no configured operator."""

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:{context.stage}-adapter-unavailable",
            status="blocked",
            receipt={"stage": context.stage, "external_action": "not_started"},
            failure={
                "kind": "execution-environment",
                "detail": f"no authority-owned {context.stage} adapter is configured",
            },
            retry_eligible=False,
        )

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        return self.execute_stage(context)


def build_authority_delivery_lifecycle(
    store: WorkbenchStore,
    config: WorkbenchConfig,
    *,
    coordinator_epoch: int,
    owner_id: str | None = None,
    github_delivery: GitHubDelivery | None = None,
    verification_observer: VerificationIdentityObserver | None = None,
) -> DeliveryLifecycleReconciler:
    """Build the one authority adapter composition for the existing scheduler.

    Constructing the composition has no GitHub, deployment, or model effect.
    The reconciler asks ``WorkbenchStore.delivery_stage_authorization`` before
    it reaches the restricted GitHub adapter, so a configured authority still
    cannot act without an objective-specific durable authorization receipt.
    """

    observer = verification_observer or BoundedVerificationIdentityObserver(config)
    task_adapter = TaskDeliveryStageAdapter(store, observer)
    delivery = github_delivery or GitHubDelivery(
        store,
        ArtifactStore(config.state_root / "artifacts"),
    )
    github_adapter = RestrictedGitHubDeliveryStageAdapter(GitHubDeliveryStageAdapter(delivery))
    unavailable = UnavailableExternalStageAdapter()
    deployment_adapter = build_authority_deployment_stage_adapter(store, config) or unavailable
    return DeliveryLifecycleReconciler(
        store,
        owner_id=owner_id or f"coordinator-{coordinator_epoch}",
        coordinator_epoch=coordinator_epoch,
        adapter=CompositeDeliveryStageAdapter(
            {
                "plan": task_adapter,
                "implement": task_adapter,
                "verify": task_adapter,
                "integrate": github_adapter,
                "ci": github_adapter,
                "publish": github_adapter,
                "deploy": deployment_adapter,
                "live-verify": deployment_adapter,
            }
        ),
    )
