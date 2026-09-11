"""Read-only Authority evidence projection for blocked-node recovery.

The projection consumes only the current task snapshot and immutable
content-addressed receipts already attached to the current node result.  It
does not execute a command, inspect a worktree, enumerate ignored files, or
infer an owner from free-form result text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .dependency_inputs import DependencyInputError, accepted_ancestor_nodes
from .execution_attribution import ExecutionAttribution
from .model import canonical_json
from .node_recovery_policy import classify_failure


_ARTIFACT_BYTES_LIMIT = 256 * 1024
_MAX_ARTIFACT_REFS = 64
_MAX_ANCESTORS = 64
_MAX_FAILURES = 64
_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}:[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_VALIDATION_PROFILES = frozenset(
    {
        "dsh-b-ipc-v1",
        "dsh-b-pairing-check-v1",
        "dsh-b-pairing-write-v1",
    }
)
_READINESS_DEPENDENCY_CODES = frozenset(
    {
        "missing-dependency",
        "dependency-unreadable",
        "dependency-kind-mismatch",
        "invalid-dependency",
        "source-unresolved",
        "source-outside-worktree",
    }
)
_KNOWN_ARTIFACT_KEYS = frozenset(
    {
        "execution-readiness",
        "dependency-materialization",
        "dependency-input",
        "patch",
        "recovery-snapshot",
        "validation",
        "controlled-validation",
        "structured-result",
        "harness-failure",
    }
)


def collect_node_observation(
    store: Any,
    task_id: str,
    node_id: str,
    *,
    source_event_cursor: int = 0,
) -> dict[str, Any]:
    """Collect one bounded, read-only recovery observation.

    ``store`` is expected to expose the existing ``get_task`` snapshot and its
    ``artifacts`` property.  The returned mapping is directly usable as the
    observation input for the recovery DAO; ``attempt`` is retained as an
    alias for the DAO's existing field while ``node_attempt`` is the public
    projection name.
    """

    task_id = _identity(task_id, "task_id")
    node_id = _identity(node_id, "node_id")
    if isinstance(source_event_cursor, bool) or not isinstance(source_event_cursor, int):
        raise ValueError("source_event_cursor must be a non-negative integer")
    if source_event_cursor < 0:
        raise ValueError("source_event_cursor must be a non-negative integer")

    task = store.get_task(task_id)
    if not isinstance(task, Mapping) or task.get("task_id") != task_id:
        raise ValueError("store returned an invalid task snapshot")
    task_revision = _integer(task.get("state_revision"), "task.state_revision", minimum=0)
    task_state = _text(task.get("state"), "unknown")
    raw_nodes = task.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise ValueError("store returned a task without a node list")
    node = next(
        (candidate for candidate in raw_nodes if isinstance(candidate, Mapping) and candidate.get("node_id") == node_id),
        None,
    )
    if node is None:
        raise KeyError(f"{task_id}/{node_id}")

    node_attempt = _integer(node.get("attempt"), "node.attempt", minimum=0)
    node_state = _text(node.get("state"), "unknown")
    raw_result = node.get("result")
    result = dict(raw_result) if isinstance(raw_result, Mapping) else {}
    artifacts, artifact_payloads = _result_artifacts(store, result)

    attribution, attribution_present, attribution_current = _current_attribution(
        result.get("execution_attribution"), task_id, node_id, node_attempt
    )
    origin = attribution.failure.origin if attribution_current else "unknown"
    effective_cursor = source_event_cursor
    if attribution_current and attribution is not None and attribution.state.event_cursor is not None:
        effective_cursor = max(effective_cursor, attribution.state.event_cursor)

    readiness = _readiness_report(artifact_payloads.get("execution-readiness"))
    materialization = _materialization_report(artifact_payloads.get("dependency-materialization"))
    validation_profile, validation_succeeded = _validation_facts(artifact_payloads)

    readiness_failure = _first_readiness_failure(readiness)
    readiness_codes = _readiness_failure_codes(readiness)
    readiness_failed = readiness is not None and readiness.get("ready") is False
    readiness_ready = readiness is not None and readiness.get("ready") is True
    dependencies_ready: bool | None = None
    if readiness_ready:
        dependencies_ready = True
    elif readiness_failed:
        dependencies_ready = False
    if materialization is not None:
        if materialization.get("status") == "blocked":
            dependencies_ready = False
        elif materialization.get("valid") is True and not readiness_failed:
            dependencies_ready = True

    only_missing_dependency: bool | None = None
    if readiness_failed and readiness_codes:
        only_missing_dependency = all(_is_dependency_failure(item) for item in readiness_codes)
    elif materialization is not None and materialization.get("status") == "blocked":
        only_missing_dependency = True

    category = _category(
        attribution_current=attribution_current,
        origin=origin,
        readiness_failure=readiness_failure,
        materialization=materialization,
    )
    phase = _phase(
        result,
        attribution_current=attribution_current,
        attribution=attribution,
        readiness_failed=readiness_failed,
    )
    harness_failure = artifact_payloads.get("harness-failure")
    if (attribution_current and origin == "tooling_bug" and attribution is not None
            and attribution.timings.execute.status == "unknown" and attribution.timings.verify.status == "unknown"
            and isinstance(harness_failure, Mapping)
            and harness_failure.get("kind") == "harness-failure"
            and (harness_failure.get("task_id"), harness_failure.get("node_id"), harness_failure.get("attempt"))
                == (task_id, node_id, node_attempt)
            and harness_failure.get("phase") == "pre_execution"
            and harness_failure.get("executor_started") is False):
        phase = "pre_execution"

    implementation_ready: bool | None = None
    if phase == "pre_execution" and attribution_current:
        implementation_ready = False

    accepted_ancestors, ancestor_source_refs = _accepted_ancestors(task, node_id)
    if bool(node.get("verifier")) and accepted_ancestors:
        implementation_ready = True
    source_refs = list(ancestor_source_refs)
    dependency_refs: list[str] = []
    for key in ("dependency-input", "dependency-materialization"):
        ref = artifacts.get(key)
        if ref is not None and ref not in dependency_refs:
            dependency_refs.append(ref)
    current_patch = artifacts.get("patch")
    if current_patch is not None and current_patch not in source_refs:
        source_refs.append(current_patch)

    verification_wait = bool(
        bool(node.get("verifier"))
        and node_state == "blocked"
        and phase == "pre_execution"
        and category in {"dependency", "environment", "network"}
        and bool(accepted_ancestors)
    )
    material_progress = bool(
        effective_cursor > 0
        or
        attribution_current
        or readiness is not None
        or materialization is not None
        or validation_profile is not None
        or accepted_ancestors
        or source_refs
        or dependency_refs
    )
    progress_detail = {
        "authoritative": material_progress,
        "source_event_cursor": effective_cursor,
        "sources": _progress_sources(
            source_event_cursor=effective_cursor,
            readiness=readiness,
            materialization=materialization,
            validation_profile=validation_profile,
            attribution_current=attribution_current,
            accepted_ancestors=accepted_ancestors,
        ),
    }

    fingerprint_code = _failure_code(
        readiness_failure=readiness_failure,
        materialization=materialization,
        validation_profile=validation_profile,
        validation_succeeded=validation_succeeded,
        category=category,
        result=result,
    )
    fingerprint_refs = dict(artifacts)
    fingerprint_refs.update(
        {f"ancestor:{item['node_id']}": item["patch_ref"] for item in accepted_ancestors if item.get("patch_ref")}
    )
    failure_fingerprint = _failure_fingerprint(
        task_id=task_id,
        node_id=node_id,
        attempt=node_attempt,
        code=fingerprint_code,
        origin=origin,
        evidence_refs=fingerprint_refs,
    )

    observation: dict[str, Any] = {
        "task_id": task_id,
        "node_id": node_id,
        "node_attempt": node_attempt,
        "attempt": node_attempt,
        "task_revision": task_revision,
        "task_state": task_state,
        "node_state": node_state,
        "category": category,
        "origin": origin,
        "phase": phase,
        "evidence_refs": artifacts,
        "source_refs": source_refs,
        "dependency_refs": dependency_refs,
        "accepted_ancestors": accepted_ancestors,
        "verification_wait": verification_wait,
        "failure_code": fingerprint_code,
        "failure_fingerprint": failure_fingerprint,
        "implementation_ready": implementation_ready,
        "executor_not_started": phase == "pre_execution" and attribution_current,
        "only_missing_dependency": only_missing_dependency,
        "dependencies_ready": dependencies_ready,
        "validation_profile": validation_profile,
        "validation_succeeded": validation_succeeded,
        "readiness_ready": readiness_ready if readiness is not None else None,
        "source_event_cursor": effective_cursor,
        "material_progress": material_progress,
        "authoritative_material_progress": progress_detail,
    }
    if validation_succeeded is None:
        observation.pop("validation_succeeded")
    if readiness is None:
        observation.pop("readiness_ready")
    return observation


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a non-empty identifier")
    return value


def _text(value: object, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _valid_ref(value: object) -> bool:
    return isinstance(value, str) and _REF_RE.fullmatch(value) is not None


def _result_artifacts(
    store: Any,
    result: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    raw = result.get("artifacts")
    if not isinstance(raw, Mapping):
        return {}, {}
    refs: dict[str, str] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for index, (raw_key, raw_ref) in enumerate(raw.items()):
        if index >= _MAX_ARTIFACT_REFS:
            break
        if not isinstance(raw_key, str) or not raw_key or not _valid_ref(raw_ref):
            continue
        key = raw_key if raw_key in _KNOWN_ARTIFACT_KEYS else raw_key[:128]
        refs[key] = raw_ref
        payload = _read_json_artifact(store, raw_ref)
        if payload is not None:
            payloads[key] = payload
    return refs, payloads


def _read_json_artifact(store: Any, ref: str) -> dict[str, Any] | None:
    artifacts = getattr(store, "artifacts", None)
    if artifacts is None:
        return None
    path: Path | None = None
    try:
        path_for = getattr(artifacts, "path_for", None)
        if callable(path_for):
            candidate = path_for(ref)
            path = candidate if isinstance(candidate, Path) else Path(candidate)
        else:
            verify = getattr(artifacts, "verify", None)
            if callable(verify):
                candidate = verify(ref)
                path = candidate if isinstance(candidate, Path) else Path(candidate)
    except (OSError, ValueError, TypeError):
        return None
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _ARTIFACT_BYTES_LIMIT:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        digest = ref.split(":", 2)[1]
    except (IndexError, AttributeError):
        return None
    if hashlib.sha256(raw).hexdigest() != digest:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _current_attribution(
    raw: object,
    task_id: str,
    node_id: str,
    attempt: int,
) -> tuple[ExecutionAttribution | None, bool, bool]:
    if raw is None:
        return None, False, False
    if not isinstance(raw, Mapping):
        return None, True, False
    try:
        attribution = ExecutionAttribution.from_dict(raw)
    except (TypeError, ValueError):
        return None, True, False
    state = attribution.state
    current = (state.task_id, state.node_id, state.attempt) == (task_id, node_id, attempt)
    return attribution, True, current


def _readiness_report(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        return None
    ready = payload.get("ready")
    failures = payload.get("failures")
    failure_origin = payload.get("failure_origin")
    if not isinstance(ready, bool) or not isinstance(failures, list) or len(failures) > _MAX_FAILURES:
        return None
    if ready and failure_origin is not None:
        return None
    if not ready and failure_origin != "environment":
        return None
    normalized: list[dict[str, str]] = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            return None
        check_id = failure.get("check_id")
        code = failure.get("code")
        origin = failure.get("origin")
        if not all(isinstance(item, str) and item for item in (check_id, code, origin)):
            return None
        if origin != "environment":
            return None
        normalized.append({"check_id": check_id, "code": code, "origin": origin})
    if ready and normalized:
        return None
    if not ready and not normalized:
        return {"ready": False, "failures": [], "valid": True}
    return {"ready": ready, "failures": normalized, "valid": True}


def _materialization_report(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    kind = payload.get("kind") if isinstance(payload, Mapping) else None
    if not isinstance(kind, str) or kind not in {
        "pnpm-offline-materialization",
        "not-applicable",
    }:
        return None
    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        return None
    if status not in {None, "blocked", "succeeded", "passed"}:
        return None
    return {
        "valid": True,
        "kind": kind,
        "status": status,
    }


def _validation_facts(
    payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, bool | None]:
    candidates: list[Mapping[str, Any]] = list(payloads.values())
    for payload in candidates:
        profile = payload.get("check_id")
        if not isinstance(profile, str) or profile not in _VALIDATION_PROFILES:
            continue
        succeeded: bool | None = None
        if isinstance(payload.get("ok"), bool):
            succeeded = payload["ok"]
        elif payload.get("status") in {"passed", "succeeded", "failed"}:
            succeeded = payload.get("status") in {"passed", "succeeded"}
        return profile, succeeded
    return None, None


def _first_readiness_failure(readiness: Mapping[str, Any] | None) -> Mapping[str, str] | None:
    if readiness is None:
        return None
    failures = readiness.get("failures")
    return failures[0] if isinstance(failures, list) and failures and isinstance(failures[0], Mapping) else None


def _readiness_failure_codes(readiness: Mapping[str, Any] | None) -> list[Mapping[str, str]]:
    if readiness is None or not isinstance(readiness.get("failures"), list):
        return []
    return [item for item in readiness["failures"] if isinstance(item, Mapping)]


def _is_dependency_failure(failure: Mapping[str, str]) -> bool:
    code = failure.get("code")
    check_id = failure.get("check_id")
    return code in _READINESS_DEPENDENCY_CODES or check_id == "pnpm:linker"


def _category(
    *,
    attribution_current: bool,
    origin: str,
    readiness_failure: Mapping[str, str] | None,
    materialization: Mapping[str, Any] | None,
) -> str:
    if not attribution_current:
        return "unknown"
    if origin not in {"unknown", "environment"}:
        direct = classify_failure({"source": "direct_attribution", "origin": origin})
        if direct != "unknown":
            return direct
    if readiness_failure is not None:
        classified = classify_failure(
            {
                "source": "authority_readiness",
                "origin": readiness_failure.get("origin"),
                "code": readiness_failure.get("code"),
                "check_id": readiness_failure.get("check_id"),
            }
        )
        if classified != "unknown":
            return classified
        return "unknown"
    if materialization is not None and materialization.get("status") == "blocked":
        return "dependency"
    direct = classify_failure({"source": "direct_attribution", "origin": origin})
    return direct if direct != "unknown" else "unknown"


def _phase(
    result: Mapping[str, Any],
    *,
    attribution_current: bool,
    attribution: ExecutionAttribution | None,
    readiness_failed: bool,
) -> str:
    if readiness_failed and attribution_current and attribution is not None:
        if attribution.timings.execute.status == "unknown" and attribution.timings.verify.status == "unknown":
            return "pre_execution"
    if attribution_current and attribution is not None:
        if attribution.timings.execute.status != "unknown" or attribution.timings.verify.status != "unknown":
            return "post_execution"
    if _text(result.get("status"), "") in {"succeeded", "accepted"}:
        return "completed"
    return "blocked_observed"


def _accepted_ancestors(
    task: Mapping[str, Any],
    node_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        ancestors = accepted_ancestor_nodes(task, node_id)
    except (DependencyInputError, TypeError, ValueError):
        return [], []
    output: list[dict[str, Any]] = []
    refs: list[str] = []
    for ancestor in ancestors[:_MAX_ANCESTORS]:
        ancestor_id = ancestor.get("node_id")
        attempt = ancestor.get("attempt")
        if not isinstance(ancestor_id, str) or not isinstance(attempt, int) or isinstance(attempt, bool):
            continue
        result = ancestor.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        raw_artifacts = result_map.get("artifacts")
        patch_ref = None
        if isinstance(raw_artifacts, Mapping):
            for key in ("patch", "recovery-snapshot"):
                candidate = raw_artifacts.get(key)
                if _valid_ref(candidate):
                    patch_ref = candidate
                    break
        entry = {"node_id": ancestor_id, "attempt": attempt, "patch_ref": patch_ref}
        output.append(entry)
        if patch_ref is not None and patch_ref not in refs:
            refs.append(patch_ref)
    return output, refs


def _progress_sources(
    *,
    source_event_cursor: int,
    readiness: Mapping[str, Any] | None,
    materialization: Mapping[str, Any] | None,
    validation_profile: str | None,
    attribution_current: bool,
    accepted_ancestors: Sequence[Mapping[str, Any]],
) -> list[str]:
    sources: list[str] = []
    if source_event_cursor > 0:
        sources.append("event-cursor")
    if attribution_current:
        sources.append("current-attribution")
    if readiness is not None:
        sources.append("readiness-report")
    if materialization is not None:
        sources.append("materialization-report")
    if validation_profile is not None:
        sources.append("validation-result")
    if accepted_ancestors:
        sources.append("accepted-ancestors")
    return sources


def _failure_code(
    *,
    readiness_failure: Mapping[str, str] | None,
    materialization: Mapping[str, Any] | None,
    validation_profile: str | None,
    validation_succeeded: bool | None,
    category: str,
    result: Mapping[str, Any],
) -> str | None:
    if readiness_failure is not None:
        return readiness_failure.get("code")
    if materialization is not None and materialization.get("status") == "blocked":
        return "dependency-materialization-blocked"
    if validation_profile is not None:
        return "validation-failure" if validation_succeeded is not True else "validation-succeeded"
    if category != "unknown" and result.get("status") in {"failed", "blocked", "indeterminate"}:
        return category
    return None


def _failure_fingerprint(
    *,
    task_id: str,
    node_id: str,
    attempt: int,
    code: str | None,
    origin: str,
    evidence_refs: Mapping[str, str],
) -> str:
    stable_refs = {
        key: value
        for key, value in sorted(evidence_refs.items())
        if isinstance(key, str) and isinstance(value, str) and _valid_ref(value)
    }
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "node_id": node_id,
        "attempt": attempt,
        "code": code,
        "origin": origin,
        "evidence_refs": stable_refs,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["collect_node_observation"]
