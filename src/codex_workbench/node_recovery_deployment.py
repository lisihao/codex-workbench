"""Read one linked repair's local deployment evidence without causing effects.

This projection deliberately does not dispatch a delivery stage, invoke an
adapter, read a token, or probe a runtime.  It only decides whether durable
deployment receipts and the currently installed manifest prove that a linked
repair is deployed on the configured local Workbench authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Any

from .config import WorkbenchConfig
from .delivery_lifecycle import IDENTITY_FIELDS, live_verification_requirements
from .deployment_helper import SourceIdentity, read_deployment_json
from .model import canonical_hash
from .store import WorkbenchStore


_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_REQUIRED_RUNTIME_IDENTITIES = frozenset(IDENTITY_FIELDS)
_MAX_READINESS_ARTIFACT_BYTES = 256 * 1024
_READINESS_ARTIFACT_KIND = "node-recovery-readiness/v1"
_READINESS_ARTIFACT_FIELDS = frozenset({
    "kind",
    "task_id",
    "node_id",
    "node_attempt",
    "task_revision",
    "authority_epoch",
    "authority_instance_id",
    "install_manifest_sha256",
    "report",
})
_READINESS_REPORT_FIELDS = frozenset({
    "schema_version",
    "worktree",
    "ready",
    "failure_origin",
    "summary",
    "elapsed_ms",
    "checks",
    "failures",
})


def observe_repair_delivery(
    store: WorkbenchStore,
    config: WorkbenchConfig,
    episode: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the current read-only deployment state for one linked repair.

    A verified result is intentionally narrower than a completed repair task:
    it requires the complete delivery objective, exact successful deploy and
    live-verification receipts, their settled dispatches, matching identities,
    the configured local target, and the current installed manifest.  A newer
    Authority epoch leaves prior live verification historical and returns a
    wait for the caller's fresh readiness path instead of inventing a probe.

    @param store: Existing durable task and delivery authority.
    @param config: Current local Workbench configuration and install manifest.
    @param episode: A recovery episode with an already linked repair record.
    @returns: A JSON-safe waiting, needs-action, or verified projection.
    """

    repair = _repair_link(episode)
    if repair is None:
        return _waiting("repair_binding_unknown")
    request_id = repair["repair_request_id"]
    task_id = repair["repair_task_id"]

    try:
        planning = store.get_planning_request(request_id)
    except (KeyError, ValueError):
        return _waiting("repair_reservation_unknown", repair=repair)
    if planning.get("task_id") != task_id:
        return _waiting("repair_reservation_unknown", repair=repair)
    planning_state = planning.get("state")
    if planning_state in {"pending", "running"}:
        return _waiting(f"repair_planning_{planning_state}", repair=repair)
    if planning_state in {"indeterminate", None}:
        return _waiting("repair_reservation_unknown", repair=repair)
    if planning_state == "failed":
        return _needs_action("repair_planning_failed", repair=repair, owner="authority")
    if planning_state != "succeeded":
        return _waiting("repair_reservation_unknown", repair=repair)

    try:
        task = store.get_task(task_id)
    except (KeyError, ValueError):
        return _waiting("repair_task_materialization_pending", repair=repair)
    task_state = task.get("state")
    if task_state == "needs_approval":
        return _needs_action(
            "repair_task_needs_approval", repair=repair, owner="operator", requires_authorization=True
        )
    if task_state in {"needs_fix", "blocked", "cancelled"}:
        return _needs_action("repair_task_failed", repair=repair, owner="authority")
    if task_state != "accepted":
        return _waiting("repair_task_pending", repair=repair)

    try:
        objective = store.get_delivery_objective_for_task(task_id)
    except (KeyError, ValueError):
        return _waiting("repair_delivery_unknown", repair=repair)
    if objective is None:
        return _waiting("repair_accepted_not_deployed", repair=repair)
    if objective.get("task_id") != task_id:
        return _waiting("repair_delivery_unknown", repair=repair)
    objective_state = objective.get("state")
    if objective_state == "needs_decision":
        return _needs_action(
            "repair_delivery_needs_decision",
            repair=repair,
            owner="operator",
            requires_authorization=True,
        )
    if objective_state == "cancelled":
        return _needs_action(
            "repair_delivery_cancelled",
            repair=repair,
            owner="operator",
            requires_authorization=True,
        )
    if objective_state in {"active", "waiting"}:
        return _waiting(f"repair_delivery_{objective_state}", repair=repair)
    if objective_state != "complete" or objective.get("stage") != "live-verify":
        return _waiting("repair_delivery_unknown", repair=repair)

    target = _objective_target(objective)
    configured_target = _configured_local_target(config)
    if target is None or configured_target is None or target != configured_target:
        return _waiting("repair_delivery_unknown", repair=repair)
    if not _required_objective_closure(objective):
        return _waiting("repair_delivery_unknown", repair=repair)

    deploy = _successful_stage(objective, "deploy")
    live = _successful_stage(objective, "live-verify")
    if deploy is None or live is None:
        return _waiting("repair_delivery_unknown", repair=repair)
    deploy_receipt, deploy_dispatch = deploy
    live_receipt, live_dispatch = live
    deploy_source = _deploy_source(
        objective, deploy_receipt, deploy_dispatch, target
    )
    if deploy_source is None:
        return _waiting("repair_delivery_unknown", repair=repair)
    runtime = _live_runtime(
        objective,
        live_receipt,
        live_dispatch,
        deploy_dispatch,
        deploy_source,
        target,
    )
    if runtime is None:
        return _waiting("repair_delivery_unknown", repair=repair)
    if not _current_manifest_matches(config, deploy_source):
        return _waiting("repair_delivery_unknown", repair=repair)

    try:
        authority = store.authority_status()
    except (OSError, ValueError):
        authority = None
    readiness_ref: str | None = None
    if not _same_current_authority(authority, runtime):
        if _authority_epoch_drifted(authority, runtime):
            readiness_ref = _current_readiness_proof(store, config, episode, authority)
        if readiness_ref is None:
            return _fresh_readiness_wait(repair)

    evidence = objective.get("evidence_fingerprints")
    assert isinstance(evidence, Mapping)
    verified = canonical_hash(
        {
            "commit": deploy_source["commit"],
            "version": deploy_source["version"],
            "source_manifest": deploy_source["manifest"],
            "deploy_dispatch_id": deploy_dispatch["dispatch_id"],
            "live_verify_dispatch_id": live_dispatch["dispatch_id"],
            "deploy_receipt_id": deploy_receipt["receipt_id"],
            "live_verify_receipt_id": live_receipt["receipt_id"],
            "evidence_fingerprints": {
                "deploy": evidence["deploy"],
                "live-verify": evidence["live-verify"],
            },
        }
    )
    if verified == repair["repair_fingerprint"]:
        return _waiting("repair_delivery_unknown", repair=repair)
    evidence_refs: dict[str, str] = {
        "objective_id": objective["objective_id"],
        "deploy_receipt_id": deploy_receipt["receipt_id"],
        "live_verify_receipt_id": live_receipt["receipt_id"],
    }
    if readiness_ref is not None:
        evidence_refs["recovery_readiness_ref"] = readiness_ref
    return {
        "state": "verified",
        "reason_kind": "repair_deployment_verified",
        "owner": "authority",
        "requires_authorization": False,
        "repair_request_id": request_id,
        "repair_task_id": task_id,
        "expected_repair_fingerprint": repair["repair_fingerprint"],
        "verified_deployment_fingerprint": verified,
        "fresh_readiness_confirmed": readiness_ref is not None,
        "historical_live_verification_retained": True,
        "evidence_refs": evidence_refs,
    }


def _repair_link(episode: Mapping[str, Any]) -> dict[str, str] | None:
    if not isinstance(episode, Mapping):
        return None
    raw = episode.get("repair")
    if not isinstance(raw, Mapping):
        return None
    request_id = _text(raw.get("repair_request_id"))
    task_id = _text(raw.get("repair_task_id"))
    fingerprint = _fingerprint(raw.get("repair_fingerprint"))
    if request_id is None or task_id is None or fingerprint is None:
        return None
    return {
        "repair_request_id": request_id,
        "repair_task_id": task_id,
        "repair_fingerprint": fingerprint,
    }


def _objective_target(objective: Mapping[str, Any]) -> str | None:
    endpoints = objective.get("requested_endpoints")
    deployment = endpoints.get("deployment") if isinstance(endpoints, Mapping) else None
    return _text(deployment.get("target")) if isinstance(deployment, Mapping) else None


def _configured_local_target(config: WorkbenchConfig) -> str | None:
    if config.deployment_role != "authority":
        return None
    try:
        raw = json.loads(config.config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    local = raw.get("local_deployment") if isinstance(raw, Mapping) else None
    if not isinstance(local, Mapping) or local.get("schema_version") != 1:
        return None
    return _text(local.get("target"))


def _required_objective_closure(objective: Mapping[str, Any]) -> bool:
    stages = objective.get("required_stages")
    identities = objective.get("required_identities")
    current_identities = objective.get("identities")
    evidence = objective.get("evidence_fingerprints")
    return (
        isinstance(stages, list)
        and all(isinstance(stage, str) for stage in stages)
        and {"deploy", "live-verify"}.issubset(stages)
        and isinstance(identities, list)
        and all(isinstance(identity, str) for identity in identities)
        and _REQUIRED_RUNTIME_IDENTITIES.issubset(set(identities))
        and isinstance(current_identities, Mapping)
        and _REQUIRED_RUNTIME_IDENTITIES.issubset(set(current_identities))
        and isinstance(evidence, Mapping)
    )


def _successful_stage(
    objective: Mapping[str, Any], stage: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    receipts = objective.get("stage_receipts")
    dispatches = objective.get("stage_dispatches")
    if not isinstance(receipts, list) or not isinstance(dispatches, list):
        return None
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for receipt in receipts:
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("stage") != stage
            or receipt.get("state") != "succeeded"
            or not isinstance(receipt.get("attempt"), int)
            or isinstance(receipt.get("attempt"), bool)
            or not _text(receipt.get("receipt_id"))
            or not _text(receipt.get("evidence_fingerprint"))
            or not isinstance(receipt.get("receipt"), Mapping)
        ):
            continue
        matching_dispatches = [
            dispatch
            for dispatch in dispatches
            if isinstance(dispatch, Mapping)
            and dispatch.get("stage") == stage
            and dispatch.get("attempt") == receipt["attempt"]
            and dispatch.get("state") == "settled"
            and dispatch.get("receipt_id") == receipt["receipt_id"]
            and _text(dispatch.get("dispatch_id"))
        ]
        if len(matching_dispatches) == 1:
            matches.append((dict(receipt), dict(matching_dispatches[0])))
    return matches[0] if len(matches) == 1 else None


def _deploy_source(
    objective: Mapping[str, Any],
    receipt: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    target: str,
) -> dict[str, Any] | None:
    body = receipt.get("receipt")
    identities = receipt.get("identities")
    objective_identities = objective.get("identities")
    evidence = objective.get("evidence_fingerprints")
    if not (
        isinstance(body, Mapping)
        and isinstance(identities, Mapping)
        and isinstance(objective_identities, Mapping)
        and isinstance(evidence, Mapping)
        and body.get("target") == target
        and body.get("helper_status") == "succeeded"
    ):
        return None
    source = _source_identity(body.get("source"))
    deploy_identity = identities.get("deploy")
    if source is None or not isinstance(deploy_identity, Mapping):
        return None
    if (
        deploy_identity.get("target") != target
        or deploy_identity.get("dispatch_id") != dispatch.get("dispatch_id")
        or deploy_identity.get("commit") != source["commit"]
        or deploy_identity.get("version") != source["version"]
        or deploy_identity.get("source_manifest") != source["manifest"]
        or _fingerprint(deploy_identity.get("request_fingerprint")) is None
        or not _positive_int(deploy_identity.get("pre_install_coordinator_epoch"))
        or objective_identities.get("deploy") != deploy_identity
        or not _objective_evidence_matches(evidence, "deploy", receipt)
    ):
        return None
    return source


def _live_runtime(
    objective: Mapping[str, Any],
    receipt: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    deploy_dispatch: Mapping[str, Any],
    source: Mapping[str, Any],
    target: str,
) -> dict[str, Any] | None:
    body = receipt.get("receipt")
    identities = receipt.get("identities")
    objective_identities = objective.get("identities")
    evidence = objective.get("evidence_fingerprints")
    if not (
        isinstance(body, Mapping)
        and isinstance(identities, Mapping)
        and isinstance(objective_identities, Mapping)
        and isinstance(evidence, Mapping)
        and body.get("target") == target
        and body.get("deployment_dispatch_id") == deploy_dispatch.get("dispatch_id")
        and _source_identity(body.get("expected_source")) == source
        and _manifest_identity(body.get("installed_manifest"))
        == {"commit": source["commit"], "version": source["version"]}
        and _live_checks_match(objective, body.get("checks"), source)
        and _objective_evidence_matches(evidence, "live-verify", receipt)
    ):
        return None
    runtime = identities.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    expected_runtime = {
        "target": target,
        "commit": source["commit"],
        "version": source["version"],
        "health_url": runtime.get("health_url"),
        "functional_url": runtime.get("functional_url"),
        "coordinator_epoch": runtime.get("coordinator_epoch"),
        "coordinator_instance_id": runtime.get("coordinator_instance_id"),
    }
    if (
        runtime != expected_runtime
        or not _positive_int(runtime.get("coordinator_epoch"))
        or _text(runtime.get("coordinator_instance_id")) is None
        or objective_identities.get("runtime") != runtime
    ):
        return None
    return dict(runtime)


def _live_checks_match(
    objective: Mapping[str, Any], checks: object, source: Mapping[str, Any]
) -> bool:
    if not isinstance(checks, Mapping):
        return False
    try:
        required = live_verification_requirements({
            "requested_endpoints": objective["requested_endpoints"],
        })
    except (KeyError, ValueError):
        return False
    observed: list[Mapping[str, Any]] = []
    for kind in ("health", "functional"):
        entries = checks.get(kind)
        names = required.get(kind)
        if not isinstance(entries, Mapping) or not isinstance(names, tuple):
            return False
        for name in names:
            entry = entries.get(name)
            if not isinstance(entry, Mapping) or not _check_identity_matches(entry, source):
                return False
            observed.append(entry)
    epochs = {entry.get("coordinator_epoch") for entry in observed}
    instances = {entry.get("coordinator_instance_id") for entry in observed}
    return len(epochs) == 1 and len(instances) == 1


def _check_identity_matches(check: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
    return (
        check.get("status") == "passed"
        and check.get("http_status") == 200
        and check.get("version") == source["version"]
        and check.get("build_commit") == source["commit"]
        and check.get("build_version") == source["version"]
        and _positive_int(check.get("coordinator_epoch"))
        and _text(check.get("coordinator_instance_id")) is not None
    )


def _objective_evidence_matches(
    evidence: Mapping[str, Any], stage: str, receipt: Mapping[str, Any]
) -> bool:
    return evidence.get(stage) == {
        "attempt": receipt.get("attempt"),
        "fingerprint": receipt.get("evidence_fingerprint"),
    }


def _source_identity(value: object) -> dict[str, Any] | None:
    try:
        return SourceIdentity.from_dict(value).to_dict()
    except ValueError:
        return None


def _manifest_identity(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    commit = _text(value.get("commit"))
    version = _text(value.get("version"))
    if commit is None or version is None:
        return None
    return {"commit": commit, "version": version}


def _current_manifest_matches(config: WorkbenchConfig, source: Mapping[str, Any]) -> bool:
    try:
        manifest = read_deployment_json(config.install_manifest)
    except ValueError:
        return False
    return _manifest_identity(manifest) == {
        "commit": source["commit"],
        "version": source["version"],
    }


def _same_current_authority(
    authority: object, runtime: Mapping[str, Any]
) -> bool:
    return (
        isinstance(authority, Mapping)
        and authority.get("active") is True
        and authority.get("authority_epoch") == runtime.get("coordinator_epoch")
        and authority.get("instance_id") == runtime.get("coordinator_instance_id")
    )


def _authority_epoch_drifted(authority: object, runtime: Mapping[str, Any]) -> bool:
    return (
        isinstance(authority, Mapping)
        and authority.get("active") is True
        and _positive_int(authority.get("authority_epoch"))
        and authority.get("authority_epoch") != runtime.get("coordinator_epoch")
    )


def _current_readiness_proof(
    store: WorkbenchStore,
    config: WorkbenchConfig,
    episode: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> str | None:
    """Return a verified current-Authority readiness ref, never a live probe."""

    parent = _episode_parent_identity(episode)
    observation = episode.get("observation")
    ref = (
        _text(observation.get("recovery_readiness_ref"))
        if isinstance(observation, Mapping)
        else None
    )
    if parent is None or ref is None:
        return None
    try:
        candidate = store.artifacts.path_for(ref)
        if candidate.stat().st_size > _MAX_READINESS_ARTIFACT_BYTES:
            return None
        artifact = store.artifacts.verify(ref)
        payload_bytes = artifact.read_bytes()
        manifest_bytes = config.install_manifest.read_bytes()
    except (OSError, ValueError):
        return None
    if len(payload_bytes) > _MAX_READINESS_ARTIFACT_BYTES:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not _valid_readiness_payload(payload, parent, authority, manifest_bytes):
        return None
    return ref


def _episode_parent_identity(episode: Mapping[str, Any]) -> dict[str, Any] | None:
    task_id = _text(episode.get("task_id"))
    node_id = _text(episode.get("node_id"))
    node_attempt = episode.get("node_attempt")
    task_revision = episode.get("task_revision")
    if (
        task_id is None
        or node_id is None
        or not _nonnegative_int(node_attempt)
        or not _positive_int(task_revision)
    ):
        return None
    return {
        "task_id": task_id,
        "node_id": node_id,
        "node_attempt": node_attempt,
        "task_revision": task_revision,
    }


def _valid_readiness_payload(
    payload: object,
    parent: Mapping[str, Any],
    authority: Mapping[str, Any],
    manifest_bytes: bytes,
) -> bool:
    if not isinstance(payload, Mapping) or set(payload) != _READINESS_ARTIFACT_FIELDS:
        return False
    if (
        payload.get("kind") != _READINESS_ARTIFACT_KIND
        or payload.get("task_id") != parent["task_id"]
        or payload.get("node_id") != parent["node_id"]
        or payload.get("node_attempt") != parent["node_attempt"]
        or payload.get("task_revision") != parent["task_revision"]
        or not _positive_int(payload.get("authority_epoch"))
        or _text(payload.get("authority_instance_id")) is None
        or _fingerprint(payload.get("install_manifest_sha256")) is None
        or payload.get("authority_epoch") != authority.get("authority_epoch")
        or payload.get("authority_instance_id") != authority.get("instance_id")
        or payload.get("install_manifest_sha256") != sha256(manifest_bytes).hexdigest()
    ):
        return False
    return _valid_ready_report(payload.get("report"))


def _valid_ready_report(report: object) -> bool:
    if not isinstance(report, Mapping) or set(report) != _READINESS_REPORT_FIELDS:
        return False
    if (
        report.get("schema_version") != 1
        or _text(report.get("worktree")) is None
        or report.get("ready") is not True
        or report.get("failure_origin") is not None
        or _text(report.get("summary")) is None
        or not _nonnegative_int(report.get("elapsed_ms"))
        or not isinstance(report.get("checks"), list)
        or report.get("failures") != []
    ):
        return False
    for check in report["checks"]:
        if not isinstance(check, Mapping):
            return False
        allowed = {"id", "kind", "status", "detail", "failure_code"}
        if (
            not {"id", "kind", "status", "detail"}.issubset(check)
            or not set(check).issubset(allowed)
            or _text(check.get("id")) is None
            or _text(check.get("kind")) is None
            or check.get("status") not in {"passed", "not-applicable"}
            or not isinstance(check.get("detail"), Mapping)
            or (
                "failure_code" in check
                and _text(check.get("failure_code")) is None
            )
        ):
            return False
    return True


def _waiting(reason_kind: str, *, repair: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _result("waiting", reason_kind, "authority", False, repair)


def _needs_action(
    reason_kind: str,
    *,
    repair: Mapping[str, str],
    owner: str,
    requires_authorization: bool = False,
) -> dict[str, Any]:
    return _result("needs_action", reason_kind, owner, requires_authorization, repair)


def _fresh_readiness_wait(repair: Mapping[str, str]) -> dict[str, Any]:
    result = _waiting("fresh_authority_readiness_required", repair=repair)
    result["fresh_readiness_required"] = True
    return result


def _result(
    state: str,
    reason_kind: str,
    owner: str,
    requires_authorization: bool,
    repair: Mapping[str, str] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "reason_kind": reason_kind,
        "owner": owner,
        "requires_authorization": requires_authorization,
    }
    if repair is not None:
        result.update({
            "repair_request_id": repair["repair_request_id"],
            "repair_task_id": repair["repair_task_id"],
            "expected_repair_fingerprint": repair["repair_fingerprint"],
        })
    return result


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fingerprint(value: object) -> str | None:
    return value if isinstance(value, str) and _FINGERPRINT.fullmatch(value) else None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
