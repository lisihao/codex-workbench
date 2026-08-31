from __future__ import annotations

"""Append-only remediation overlays for pre-A10 task Evidence.

The legacy ledger remains the authority for task and node execution.  A
remediation manifest may only add externally materialized artifacts after it
has been bound back to the immutable task/event chain it describes.
"""

from hashlib import sha256
import json
from typing import Any

from .artifacts import ArtifactStore
from .model import canonical_hash


MANIFEST_SCHEMA = "codex-workbench.legacy-evidence-remediation/v1"


def event_hash(event: dict[str, Any]) -> str:
    """Hash the immutable event identity, including its ledger cursor."""
    return canonical_hash(
        {
            "cursor": event.get("cursor"),
            "event_type": event.get("event_type"),
            "task_id": event.get("task_id"),
            "node_id": event.get("node_id"),
            "payload": event.get("payload"),
            "created_at": event.get("created_at"),
        }
    )


def load_manifest(artifacts: ArtifactStore, manifest_ref: str) -> tuple[dict[str, Any], str]:
    path = artifacts.verify(manifest_ref)
    try:
        manifest = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("legacy remediation manifest must be a JSON object") from error
    if not isinstance(manifest, dict):
        raise ValueError("legacy remediation manifest must be a JSON object")
    return manifest, canonical_hash(manifest)


def _artifact(
    artifacts: ArtifactStore,
    descriptor: object,
    *,
    required_nonempty: bool = True,
) -> dict[str, Any]:
    if not isinstance(descriptor, dict):
        raise ValueError("legacy remediation artifact must include ref/hash/kind/size")
    ref = descriptor.get("ref")
    digest = descriptor.get("hash")
    kind = descriptor.get("kind")
    size = descriptor.get("size")
    if not isinstance(ref, str) or not isinstance(digest, str) or not isinstance(kind, str):
        raise ValueError("legacy remediation artifact must include ref/hash/kind/size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("legacy remediation artifact size must be a non-negative integer")
    try:
        algorithm, ref_digest, suffix = ref.split(":", 2)
    except ValueError as error:
        raise ValueError("legacy remediation artifact ref is invalid") from error
    if algorithm != "sha256" or digest != ref_digest or kind != suffix:
        raise ValueError("legacy remediation artifact metadata does not match its ref")
    path = artifacts.verify(ref)
    actual = path.read_bytes()
    if len(actual) != size or sha256(actual).hexdigest() != digest:
        raise ValueError("legacy remediation artifact hash or size does not match")
    if required_nonempty and not actual:
        raise ValueError("legacy remediation artifact must not be empty")
    return {"ref": ref, "hash": digest, "kind": kind, "size": size}


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"legacy remediation {label} must be a non-empty list of strings")
    return tuple(value)


def _attempt(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("legacy remediation attempt must be a positive integer")
    return value


def _source_event(
    event_by_cursor: dict[int, dict[str, Any]],
    cursor: object,
    expected_hash: object,
    *,
    task_id: str,
    node_id: str | None = None,
    event_type: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    if not isinstance(cursor, int) or isinstance(cursor, bool) or not isinstance(expected_hash, str):
        raise ValueError("legacy remediation source cursor/hash is required")
    event = event_by_cursor.get(cursor)
    if event is None or event.get("task_id") != task_id or event_hash(event) != expected_hash:
        raise ValueError("legacy remediation source event cursor/hash does not match the ledger")
    if node_id is not None and event.get("node_id") != node_id:
        raise ValueError("legacy remediation source event node does not match")
    if event_type is not None and event.get("event_type") != event_type:
        raise ValueError("legacy remediation source event type does not match")
    if attempt is not None:
        try:
            matches = int(event.get("payload", {}).get("attempt")) == attempt
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError("legacy remediation source event attempt does not match")
    return event


def validate_manifest(
    manifest: dict[str, Any],
    task: dict[str, Any],
    events: list[dict[str, Any]],
    artifacts: ArtifactStore,
    review_task: dict[str, Any] | None = None,
    review_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a manifest solely against durable ledger rows and artifacts."""
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("unsupported legacy remediation manifest schema")
    source = manifest.get("source")
    overlay = manifest.get("overlay")
    if not isinstance(source, dict) or not isinstance(overlay, dict):
        raise ValueError("legacy remediation manifest requires source and overlay objects")
    task_id = task.get("task_id")
    contract = task.get("contract")
    if task.get("state") != "accepted" or not isinstance(task_id, str) or not isinstance(contract, dict):
        raise ValueError("legacy remediation source task must be an accepted task")
    if source.get("task_id") != task_id or source.get("contract_hash") != task.get("contract_hash"):
        raise ValueError("legacy remediation source task or contract hash does not match")
    if source.get("base_sha") != contract.get("base_sha"):
        raise ValueError("legacy remediation source base SHA does not match")

    event_by_cursor = {int(event["cursor"]): event for event in events if event.get("task_id") == task_id}
    first = _source_event(
        event_by_cursor, source.get("event_first"), source.get("event_first_hash"), task_id=task_id
    )
    last = _source_event(
        event_by_cursor, source.get("event_last"), source.get("event_last_hash"), task_id=task_id
    )
    if int(first["cursor"]) > int(last["cursor"]):
        raise ValueError("legacy remediation source event range is invalid")
    if first.get("event_type") != "task.created":
        raise ValueError("legacy remediation source range must begin with task.created")
    if (
        last.get("event_type") != "task.state_changed"
        or last.get("payload", {}).get("to") != "accepted"
    ):
        raise ValueError("legacy remediation source range must end with task acceptance")

    node_by_id = {str(node["node_id"]): node for node in task.get("nodes", [])}
    records = source.get("nodes")
    if not isinstance(records, list) or not records:
        raise ValueError("legacy remediation source nodes are required")
    source_nodes: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("node_id"), str):
            raise ValueError("legacy remediation source node is invalid")
        node_id = record["node_id"]
        attempt = _attempt(record.get("attempt"))
        node = node_by_id.get(node_id)
        if node is None or int(node.get("attempt", 0)) != attempt or node.get("state") != "accepted":
            raise ValueError("legacy remediation source node state or attempt does not match")
        started = _source_event(
            event_by_cursor, record.get("started_cursor"), record.get("started_hash"),
            task_id=task_id, node_id=node_id, event_type="node.started", attempt=attempt,
        )
        accepted = _source_event(
            event_by_cursor, record.get("accepted_cursor"), record.get("accepted_hash"),
            task_id=task_id, node_id=node_id, event_type="node.accepted", attempt=attempt,
        )
        if not (int(first["cursor"]) <= int(started["cursor"]) < int(accepted["cursor"]) <= int(last["cursor"])):
            raise ValueError("legacy remediation source node events fall outside its source range")
        key = (node_id, attempt)
        if key in source_nodes:
            raise ValueError("legacy remediation source node is duplicated")
        source_nodes[key] = node

    workers = overlay.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ValueError("legacy remediation overlay requires worker Evidence")
    normalized_workers: list[dict[str, Any]] = []
    worker_artifacts: list[dict[str, Any]] = []
    for item in workers:
        if not isinstance(item, dict) or not isinstance(item.get("node_id"), str):
            raise ValueError("legacy remediation worker overlay is invalid")
        attempt = _attempt(item.get("attempt"))
        node = source_nodes.get((item["node_id"], attempt))
        if node is None or node.get("verifier"):
            raise ValueError("legacy remediation worker does not bind a source worker node")
        executor = node.get("effective_executor") or node.get("executor")
        result = node.get("result") if isinstance(node.get("result"), dict) else {}
        actual_model = item.get("actual_model")
        if executor not in {"codex", "claude"} or actual_model != result.get("actual_model"):
            raise ValueError("legacy remediation worker model does not match the source result")
        artifacts_raw = item.get("artifacts")
        if not isinstance(artifacts_raw, dict) or "patch" not in artifacts_raw:
            raise ValueError("legacy remediation worker requires a patch artifact")
        normalized_artifacts = {
            name: _artifact(artifacts, value, required_nonempty=name == "patch")
            for name, value in artifacts_raw.items()
            if isinstance(name, str)
        }
        if len(normalized_artifacts) != len(artifacts_raw):
            raise ValueError("legacy remediation worker artifact names are invalid")
        source_artifact_refs = set(result.get("artifacts", {}).values())
        if any(item["ref"] not in source_artifact_refs for item in normalized_artifacts.values()):
            raise ValueError("legacy remediation worker artifact is not bound to the source worker result")
        normalized_workers.append(
            {"node_id": item["node_id"], "attempt": attempt, "actual_model": actual_model,
             "checks": _strings(item.get("checks"), "worker checks"), "artifacts": normalized_artifacts}
        )
        worker_artifacts.extend(normalized_artifacts.values())

    verifier_items = overlay.get("verifiers", [])
    if not isinstance(verifier_items, list):
        raise ValueError("legacy remediation verifier overlay must be a list")
    normalized_verifiers: list[dict[str, Any]] = []
    for item in verifier_items:
        if not isinstance(item, dict) or not isinstance(item.get("node_id"), str):
            raise ValueError("legacy remediation verifier overlay is invalid")
        attempt = _attempt(item.get("attempt"))
        node = source_nodes.get((item["node_id"], attempt))
        result = node.get("result") if node and isinstance(node.get("result"), dict) else {}
        if (
            node is None or not node.get("verifier")
            or (node.get("effective_executor") or node.get("executor")) != "codex"
            or item.get("actual_model") != result.get("actual_model")
            or "sol" not in str(item.get("actual_model", "")).lower()
            or result.get("result_kind") != "verifier"
            or result.get("verdict") != "accepted"
        ):
            raise ValueError("legacy remediation verifier is not a structured accepted source Codex Sol result")
        result_checks = _strings(result.get("checks"), "source verifier checks")
        declared_checks = _strings(item.get("checks"), "verifier checks")
        if declared_checks != result_checks:
            raise ValueError("legacy remediation verifier checks do not match the source result")
        artifacts_raw = item.get("artifacts")
        if not isinstance(artifacts_raw, dict) or not {"test-log", "verdict"} <= set(artifacts_raw):
            raise ValueError("legacy remediation verifier requires test-log and verdict artifacts")
        normalized_artifacts = {
            name: _artifact(
                artifacts,
                value,
                required_nonempty=name in {"test-log", "verdict"},
            )
            for name, value in artifacts_raw.items()
            if isinstance(name, str)
        }
        evidence = [
            _artifact(artifacts, value, required_nonempty=False)
            for value in item.get("evidence", [])
        ]
        if not evidence:
            raise ValueError("legacy remediation verifier requires Evidence artifacts")
        result_artifacts = result.get("artifacts")
        result_evidence = result.get("evidence")
        if not isinstance(result_artifacts, dict) or not isinstance(result_evidence, (list, tuple)):
            raise ValueError("legacy remediation source verifier lacks native artifact Evidence")
        source_artifact_refs = set(result_artifacts.values())
        source_evidence_refs = tuple(result_evidence)
        if (
            any(item["ref"] not in source_artifact_refs for item in normalized_artifacts.values())
            or tuple(item["ref"] for item in evidence) != source_evidence_refs
        ):
            raise ValueError("legacy remediation verifier artifact is not bound to the source verifier result")
        normalized_verifiers.append(
            {"node_id": item["node_id"], "attempt": attempt, "actual_model": item["actual_model"],
             "checks": result_checks, "artifacts": normalized_artifacts,
             "evidence": evidence}
        )

    supplemental = overlay.get("supplemental_sol_review")
    normalized_supplemental: dict[str, Any] | None = None
    if supplemental is not None:
        if not isinstance(supplemental, dict):
            raise ValueError("legacy remediation supplemental Sol review is invalid")
        patch = _artifact(artifacts, supplemental.get("patch"))
        declared_worker_artifacts = supplemental.get("worker_artifacts")
        if not isinstance(declared_worker_artifacts, list) or not declared_worker_artifacts:
            raise ValueError("legacy remediation supplemental Sol review must bind worker artifacts")
        bound_worker_artifacts = [
            _artifact(artifacts, value, required_nonempty=False)
            for value in declared_worker_artifacts
        ]
        worker_refs = {item["ref"] for item in worker_artifacts}
        if patch["ref"] not in worker_refs or any(item["ref"] not in worker_refs for item in bound_worker_artifacts):
            raise ValueError("legacy remediation supplemental Sol review does not bind the worker overlay")
        source_verifier = supplemental.get("source_verifier")
        if not isinstance(source_verifier, dict) or not isinstance(source_verifier.get("node_id"), str):
            raise ValueError("legacy remediation supplemental Sol review must bind its source verifier")
        source_verifier_attempt = _attempt(source_verifier.get("attempt"))
        verifier_node = source_nodes.get((source_verifier["node_id"], source_verifier_attempt))
        if isinstance(verifier_node, dict):
            source_executor = verifier_node.get("effective_executor") or verifier_node.get("executor")
            source_model = verifier_node.get("effective_model") or verifier_node.get("model")
            source_result = verifier_node.get("result") if isinstance(verifier_node.get("result"), dict) else {}
        else:
            source_executor = None
            source_model = None
            source_result = {}
        legacy_source_verifier = (
            source_executor == "deterministic" and source_model == "local"
        ) or (
            source_executor == "codex"
            and "sol" in str(source_model).lower()
            and "sol" in str(source_result.get("actual_model", "")).lower()
        )
        if verifier_node is None or not verifier_node.get("verifier") or not legacy_source_verifier:
            raise ValueError("legacy remediation supplemental review must bind its original source verifier")

        review_source = supplemental.get("review_source")
        if not isinstance(review_source, dict) or not isinstance(review_source.get("task_id"), str):
            raise ValueError("legacy remediation supplemental Sol review requires review_source")
        if review_task is None or review_events is None or review_task.get("task_id") != review_source["task_id"]:
            raise ValueError("legacy remediation review source task is unavailable")
        if review_task.get("task_id") == task_id or review_task.get("state") != "accepted":
            raise ValueError("legacy remediation review task must be independent and accepted")
        review_contract = review_task.get("contract")
        if not isinstance(review_contract, dict):
            raise ValueError("legacy remediation review task contract is invalid")
        if (
            review_source.get("contract_hash") != review_task.get("contract_hash")
            or review_contract.get("repository") != contract.get("repository")
            or review_contract.get("base_sha") != contract.get("base_sha")
            or task_id not in review_contract.get("dependencies", ())
            or task_id not in str(review_contract.get("objective", ""))
            or str(task.get("contract_hash")) not in str(review_contract.get("objective", ""))
        ):
            raise ValueError("legacy remediation review task contract is not bound to the source task")
        review_events_by_cursor = {
            int(event["cursor"]): event
            for event in review_events
            if event.get("task_id") == review_task["task_id"]
        }
        review_node_id = review_source.get("node_id")
        review_attempt = _attempt(review_source.get("attempt"))
        if not isinstance(review_node_id, str):
            raise ValueError("legacy remediation review source node is required")
        review_node = next((node for node in review_task.get("nodes", []) if node.get("node_id") == review_node_id), None)
        review_result = review_node.get("result") if isinstance(review_node, dict) and isinstance(review_node.get("result"), dict) else {}
        if (
            not isinstance(review_node, dict) or review_node.get("verifier") is not True
            or review_node.get("state") != "accepted" or int(review_node.get("attempt", 0)) != review_attempt
            or (review_node.get("effective_executor") or review_node.get("executor")) != "codex"
            or "sol" not in str(review_node.get("effective_model") or review_node.get("model", "")).lower()
            or "sol" not in str(review_result.get("actual_model", "")).lower()
            or review_result.get("result_kind") != "verifier" or review_result.get("verdict") != "accepted"
        ):
            raise ValueError("legacy remediation review source is not an accepted Codex Sol verifier")
        review_nodes = {
            str(node.get("node_id")): node
            for node in review_task.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("node_id"), str)
        }
        review_dependencies = tuple(review_node.get("depends_on", ()))
        review_workers = [
            review_nodes.get(str(node_id))
            for node_id in review_dependencies
        ]
        review_workers = [
            node
            for node in review_workers
            if isinstance(node, dict)
            and node.get("verifier") is not True
            and node.get("state") == "accepted"
        ]
        if not review_workers:
            raise ValueError("legacy remediation review verifier must depend on an accepted review worker")
        patch_reviewed_by_worker = False
        for review_worker in review_workers:
            worker_result = review_worker.get("result")
            if not isinstance(worker_result, dict):
                continue
            worker_artifact_refs = set(worker_result.get("artifacts", {}).values())
            if (
                (review_worker.get("effective_executor") or review_worker.get("executor"))
                in {"deterministic", "codex"}
                and worker_result.get("result_kind") == "worker"
                and worker_result.get("checks")
                and patch["ref"] in worker_artifact_refs
            ):
                patch_reviewed_by_worker = True
                break
        if not patch_reviewed_by_worker:
            raise ValueError("legacy remediation review worker did not materialize and check the source patch")
        review_started = _source_event(
            review_events_by_cursor, review_source.get("started_cursor"), review_source.get("started_hash"),
            task_id=review_task["task_id"], node_id=review_node_id, event_type="node.started", attempt=review_attempt,
        )
        review_accepted = _source_event(
            review_events_by_cursor, review_source.get("accepted_cursor"), review_source.get("accepted_hash"),
            task_id=review_task["task_id"], node_id=review_node_id, event_type="node.accepted", attempt=review_attempt,
        )
        review_task_accepted = _source_event(
            review_events_by_cursor, review_source.get("task_accepted_cursor"), review_source.get("task_accepted_hash"),
            task_id=review_task["task_id"], event_type="task.state_changed",
        )
        if review_task_accepted.get("payload", {}).get("to") != "accepted" or not (
            int(review_started["cursor"]) < int(review_accepted["cursor"]) <= int(review_task_accepted["cursor"])
        ):
            raise ValueError("legacy remediation review source event chain is invalid")
        review_artifact_refs = set(review_result.get("artifacts", {}).values())
        review_evidence_refs = tuple(review_result.get("evidence", ()))
        if not _strings(review_result.get("checks"), "review verifier checks") or not review_artifact_refs or not review_evidence_refs:
            raise ValueError("legacy remediation review verifier lacks structured Evidence")
        test_log = _artifact(artifacts, supplemental.get("test_log"))
        verdict = _artifact(artifacts, supplemental.get("verdict_artifact"))
        review_transcript = _artifact(artifacts, supplemental.get("review_transcript"))
        review_receipt = _artifact(artifacts, supplemental.get("review_receipt"))
        evidence_raw = supplemental.get("evidence")
        if not isinstance(evidence_raw, list) or not evidence_raw:
            raise ValueError("legacy remediation supplemental Sol review requires Evidence artifacts")
        evidence = [
            _artifact(artifacts, value, required_nonempty=False)
            for value in evidence_raw
        ]
        if (
            any(item["ref"] not in review_artifact_refs for item in (test_log, verdict, review_transcript, review_receipt))
            or tuple(item["ref"] for item in evidence) != review_evidence_refs
        ):
            raise ValueError("legacy remediation supplemental artifacts are not bound to the review verifier")
        normalized_supplemental = {
            "actual_model": review_result["actual_model"], "checks": tuple(review_result["checks"]),
            "patch": patch, "worker_artifacts": bound_worker_artifacts, "test_log": test_log, "verdict_artifact": verdict,
            "evidence": evidence, "review_transcript": review_transcript, "review_receipt": review_receipt,
            "review_task_id": review_task["task_id"], "review_node_id": review_node_id,
        }

    if not normalized_verifiers and normalized_supplemental is None:
        raise ValueError("legacy remediation requires source Sol verification or a bound supplemental Sol review")
    return {
        "task_id": task_id, "contract_hash": task["contract_hash"], "base_sha": contract["base_sha"],
        "event_first": int(first["cursor"]), "event_last": int(last["cursor"]),
        "workers": normalized_workers, "verifiers": normalized_verifiers, "supplemental_sol_review": normalized_supplemental,
    }
