"""Bind allowlisted validation to an existing blocked allocation and receipt.

The Authority request journal owns idempotency. This operation writes only
validation artifacts and the explicitly previewed pairing sidecars; it never
creates an attempt or changes a historical worker result.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .config import WorkbenchConfig
from .controlled_validation import plan_validation, resolve_runtime, run_validation
from .d_integration_metadata import (
    AgentNoteMetadata,
    DMetadataRequest,
    PairingMetadata,
    metadata_request_from_arguments,
    note_paths,
    pairing_paths,
)
from .d_integration_profile import NOTE_WRITE_ID, PAIRING_WRITE_ID
from .dirty_worktree_recovery import inspect_indeterminate_source_delta
from .model import canonical_hash, canonical_json, now_iso
from .store import StateConflictError, WorkbenchStore
from .worktrees import scope_allows


TOOL_NAME = "workbench_validate_blocked_node"
PAIRING_WRITE = "dsh-b-pairing-write-v1"
D_PAIRING_WRITE = PAIRING_WRITE_ID
D_NOTE_WRITE = NOTE_WRITE_ID
PAIRING_ANCHORS = (
    "packages/client/connection/README.md",
    "packages/core/system-prompt/README.md",
    "packages/physical-operator/resident-operator-local/README.md",
    "packages/physical-operator/resident-operator/README.md",
    "packages/physical-operator/tool-physical-operator/README.md",
)
PAIRING_SIDECARS = tuple(path.removesuffix(".md") + ".i18n.yaml" for path in PAIRING_ANCHORS)

VALIDATION_TOOL = {
    "name": TOOL_NAME,
    "description": "Preview or run one fixed, sandboxed validation on a blocked node's current allocation. No shell, model, new attempt, acceptance or recovery. Query a lost run via workbench_get_service_request using the same request_id.",
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["task_id", "node_id", "expected_revision", "expected_attempt",
                     "worktree", "check_id", "reason", "dry_run"],
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "node_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "expected_attempt": {"type": "integer", "minimum": 1},
            "worktree": {"type": "string", "minLength": 1},
            "check_id": {"enum": [
                "dsh-b-ipc-v1",
                "dsh-b-pairing-check-v1",
                PAIRING_WRITE,
                D_PAIRING_WRITE,
                D_NOTE_WRITE,
            ]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "dry_run": {"type": "boolean"},
            "validation_id": {"type": "string", "minLength": 1, "maxLength": 200,
                              "description": "Run only: must equal the stable Authority request_id."},
            "expected_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "confirm_pairing_write": {"type": "boolean", "default": False},
            "pair_anchors": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1},
            },
            "note_anchor": {"type": "string", "minLength": 1},
            "note_english": {"type": "string", "minLength": 1, "maxLength": 65536},
            "note_chinese": {"type": "string", "minLength": 1, "maxLength": 65536},
            "confirm_note_write": {"type": "boolean", "default": False},
        },
    },
}


def _arguments(raw: dict[str, Any]) -> dict[str, Any]:
    schema = VALIDATION_TOOL["inputSchema"]
    if set(raw) - schema["properties"].keys() or set(schema["required"]) - raw.keys():
        raise ValueError("validation accepts only its explicit identity, check and preview fields")
    values = dict(raw)
    for key in ("task_id", "node_id", "worktree", "check_id", "reason"):
        value = values[key]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{key} must be a non-empty string without outer whitespace")
    if len(values["reason"]) > 500:
        raise ValueError("validation reason must be at most 500 characters")
    for key, minimum in (("expected_revision", 0), ("expected_attempt", 1)):
        if type(values[key]) is not int or values[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    if type(values["dry_run"]) is not bool:
        raise ValueError("dry_run must be a boolean")
    if type(values.get("confirm_pairing_write", False)) is not bool:
        raise ValueError("confirm_pairing_write must be a boolean")
    if type(values.get("confirm_note_write", False)) is not bool:
        raise ValueError("confirm_note_write must be a boolean")
    if values["check_id"] not in schema["properties"]["check_id"]["enum"]:
        raise ValueError("check_id is not an allowlisted validation")
    if "expected_fingerprint" in values and (
        not isinstance(values["expected_fingerprint"], str)
        or re.fullmatch(r"[0-9a-f]{64}", values["expected_fingerprint"]) is None
    ):
        raise ValueError("expected_fingerprint must be a lowercase SHA-256 digest")
    if not values["dry_run"]:
        if "expected_fingerprint" not in values:
            raise ValueError("run requires expected_fingerprint from a fresh preview")
        validation_id = values.get("validation_id")
        if (not isinstance(validation_id, str) or not validation_id or len(validation_id) > 200
                or any(ord(character) < 32 for character in validation_id)):
            raise ValueError("run requires a stable validation_id")
        if values["check_id"] in {PAIRING_WRITE, D_PAIRING_WRITE} and values.get("confirm_pairing_write") is not True:
            raise ValueError("pairing writes require confirm_pairing_write=true")
        if values["check_id"] == D_NOTE_WRITE and values.get("confirm_note_write") is not True:
            raise ValueError("Agent Note writes require confirm_note_write=true")
    if values["check_id"] not in {PAIRING_WRITE, D_PAIRING_WRITE} and values.get("confirm_pairing_write", False):
        raise ValueError("confirm_pairing_write is valid only for the pairing-write check")
    _validate_d_fields(values)
    if not Path(values["worktree"]).is_absolute():
        raise ValueError("worktree must be the absolute allocated path")
    return values


def _validate_d_fields(values: dict[str, Any]) -> None:
    """Reject metadata fields outside the one D check that owns them."""

    check_id = values["check_id"]
    note_fields = {"note_anchor", "note_english", "note_chinese", "confirm_note_write"}
    if check_id == D_PAIRING_WRITE:
        if any(field in values for field in note_fields):
            raise ValueError("D pairing writes do not accept Agent Note fields")
        metadata_request_from_arguments(check_id, values)
        return
    if check_id == D_NOTE_WRITE:
        if "pair_anchors" in values or "confirm_pairing_write" in values:
            raise ValueError("D Agent Note writes do not accept pairing fields")
        metadata_request_from_arguments(check_id, values)
        return
    if "pair_anchors" in values or any(field in values for field in note_fields):
        raise ValueError("B validation checks do not accept D metadata fields")


def _snapshot(
    store: WorkbenchStore,
    arguments: dict[str, Any],
    metadata: DMetadataRequest | None,
) -> dict[str, Any]:
    with store.connection() as connection:
        snapshot = store._blocked_source_only_recovery_durable_snapshot(
            connection, arguments["task_id"], arguments["node_id"],
            expected_revision=arguments["expected_revision"],
            expected_attempt=arguments["expected_attempt"],
        )
        candidate = snapshot["candidate"]
        if candidate["task"]["state"] != "blocked" or candidate["node"]["state"] != "blocked":
            raise StateConflictError("validation requires a blocked task and blocked node")
        if candidate["source"]["allocation_id"] in store._source_only_recovery_hold_ids(connection):
            raise StateConflictError("a sealed recovery source cannot be used for validation")
        active = connection.execute(
            "SELECT node_id FROM nodes WHERE task_id = ? AND state = 'running' LIMIT 1",
            (arguments["task_id"],),
        ).fetchone()
        if active is not None:
            raise StateConflictError("validation requires the task's workers to have stopped")
    allocated = Path(candidate["source"]["worktree"]).resolve(strict=True)
    if Path(arguments["worktree"]).resolve(strict=True) != allocated:
        raise StateConflictError("worktree does not match the blocked node's active allocation")
    if arguments["check_id"] == PAIRING_WRITE:
        for path in PAIRING_SIDECARS:
            if not scope_allows(path, candidate["task"]["allowed_scope"], candidate["task"]["forbidden_scope"]) or not scope_allows(path, candidate["node"]["write_scopes"], ()):
                raise StateConflictError(f"pairing sidecar is outside the owned scope: {path}")
    if arguments["check_id"] in {D_PAIRING_WRITE, D_NOTE_WRITE}:
        if metadata is None:
            raise StateConflictError("D validation metadata is absent")
        _assert_d_scope_ownership(snapshot, arguments, metadata)
    return snapshot


def _assert_d_scope_ownership(
    snapshot: dict[str, Any],
    arguments: dict[str, Any],
    metadata: DMetadataRequest,
) -> None:
    """Require D's fixed blocked node and every selected source/output scope."""

    candidate = snapshot["candidate"]
    node = candidate["node"]
    if arguments["node_id"] != "D" or node["node_id"] != "D" or node["state"] != "blocked":
        raise StateConflictError("D metadata validation requires the fixed blocked D node")
    try:
        specification = json.loads(snapshot["spec_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise StateConflictError("D metadata validation node specification is invalid") from error
    read_scopes = specification.get("read_scopes") if isinstance(specification, dict) else None
    if not isinstance(read_scopes, list) or not all(isinstance(scope, str) for scope in read_scopes):
        raise StateConflictError("D metadata validation requires explicit node read scopes")
    write_scopes = node["write_scopes"]
    task_allowed = candidate["task"]["allowed_scope"]
    task_forbidden = candidate["task"]["forbidden_scope"]

    def require_read(path: str) -> None:
        if not scope_allows(path, task_allowed, task_forbidden) or not scope_allows(path, read_scopes, ()):
            raise StateConflictError(f"D metadata source is outside the owned read scope: {path}")

    def require_write(path: str) -> None:
        if not scope_allows(path, task_allowed, task_forbidden) or not scope_allows(path, write_scopes, ()):
            raise StateConflictError(f"D metadata output is outside the owned write scope: {path}")

    if isinstance(metadata, PairingMetadata):
        for anchor in metadata.anchors:
            source, translated, sidecar = pairing_paths(anchor)
            require_read(source)
            require_read(translated)
            require_write(sidecar)
        return
    if isinstance(metadata, AgentNoteMetadata):
        english, chinese = note_paths(metadata.anchor)
        require_write(english)
        require_write(chinese)
        return
    raise StateConflictError("D metadata validation has an unsupported request type")


def _delta(store: WorkbenchStore, snapshot: dict[str, Any]):
    return inspect_indeterminate_source_delta(
        snapshot["source_candidate"], dependency_input_ref=snapshot["dependency_input_ref"],
        artifacts=store.artifacts, recovery_label="blocked",
    )


def _require_journal_reservation(store: WorkbenchStore, arguments: dict[str, Any]) -> None:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT tool, task_id, state FROM authority_requests WHERE request_id = ?",
            (arguments["validation_id"],),
        ).fetchone()
    if row is None or (row["tool"], row["task_id"], row["state"]) != (
        TOOL_NAME, arguments["task_id"], "executing",
    ):
        raise StateConflictError("validation must run through its reserved Authority service request")


def validate_blocked_node(
    config: WorkbenchConfig, store: WorkbenchStore, raw_arguments: dict[str, Any],
) -> dict[str, Any]:
    """Preview a current allocation or run the exact preview without task CAS."""
    arguments = _arguments(raw_arguments)
    metadata = metadata_request_from_arguments(arguments["check_id"], arguments)
    if not arguments["dry_run"]:
        _require_journal_reservation(store, arguments)
    before = _snapshot(store, arguments, metadata)
    manifest_sha256 = sha256(config.install_manifest.read_bytes()).hexdigest()
    source_delta = _delta(store, before)
    plan = plan_validation(
        Path(arguments["worktree"]),
        arguments["check_id"],
        resolve_runtime(config),
        metadata=metadata,
    )
    plan_data = plan.to_dict()
    if canonical_json(_snapshot(store, arguments, metadata)) != canonical_json(before):
        raise StateConflictError("validation's durable binding changed during preview")
    if _delta(store, before).sha256 != source_delta.sha256:
        raise StateConflictError("validation source changed during preview")
    if sha256(config.install_manifest.read_bytes()).hexdigest() != manifest_sha256:
        raise StateConflictError("Authority installation changed during preview")
    fingerprint = canonical_hash({
        "profile": "controlled-validation/v1", "durable": canonical_hash(before),
        "source_delta": source_delta.sha256, "plan": plan_data,
        "install_manifest_sha256": manifest_sha256,
    })
    identity = {key: arguments[key] for key in (
        "task_id", "node_id", "expected_revision", "expected_attempt", "worktree", "check_id",
    )}
    metadata_response = {"requires_pairing": True} if arguments["check_id"] == D_NOTE_WRITE else {}
    if arguments["dry_run"]:
        return {**identity, "ok": True, "dry_run": True, "fingerprint": fingerprint,
                "source_delta_sha256": source_delta.sha256, "plan": plan_data,
                "task_state_changed": False, "historical_result_unchanged": True,
                "creates_attempt": False, "accepts_task": False, **metadata_response}
    if fingerprint != arguments["expected_fingerprint"]:
        raise StateConflictError("validation fingerprint changed; obtain a fresh preview")
    started = now_iso()
    result_data = run_validation(plan, store.artifacts).to_dict()
    after_digest: str | None = None
    postflight_error: str | None = None
    try:
        after = _snapshot(store, arguments, metadata)
        if sha256(config.install_manifest.read_bytes()).hexdigest() != manifest_sha256:
            raise StateConflictError("Authority installation changed during execution")
        if canonical_json(after) != canonical_json(before):
            raise StateConflictError("validation's durable binding changed during execution")
        after_delta = _delta(store, after)
        after_digest = after_delta.sha256
        allowed_changes = _allowed_source_changes(arguments["check_id"], metadata)
        before_entries = {entry.path: entry.to_dict() for entry in source_delta.entries if entry.path not in allowed_changes}
        after_entries = {entry.path: entry.to_dict() for entry in after_delta.entries if entry.path not in allowed_changes}
        if before_entries != after_entries:
            detail = "outputs" if arguments["check_id"] in {D_PAIRING_WRITE, D_NOTE_WRITE} else "sidecars"
            raise StateConflictError(f"validation changed source outside its previewed {detail}")
    except (OSError, ValueError, StateConflictError) as error:
        postflight_error = str(error)
    ok = result_data.get("ok") is True and postflight_error is None
    audit = {
        **identity, "validation_id": arguments["validation_id"], "fingerprint": fingerprint,
        "reason": arguments["reason"], "started_at": started, "finished_at": now_iso(),
        "source_delta_before": source_delta.sha256, "source_delta_after": after_digest,
        "durable_binding_sha256": canonical_hash(before), "plan": plan_data,
        "install_manifest_sha256": manifest_sha256,
        "execution": result_data, "postflight_error": postflight_error, "ok": ok,
        "historical_result_unchanged": postflight_error is None,
    }
    audit_ref = store.artifacts.put_text(canonical_json(audit), "validation.json")
    return {**identity, "validation_id": arguments["validation_id"], "fingerprint": fingerprint,
            "ok": ok, "status": "passed" if ok else "failed", "audit_ref": audit_ref,
            "source_delta_before": source_delta.sha256, "source_delta_after": after_digest,
            "postflight_error": postflight_error, "task_state_changed": False,
            "historical_result_unchanged": postflight_error is None, "creates_attempt": False,
            "accepts_task": False, "recovery_requires_fresh_preview": True, **metadata_response}


def _allowed_source_changes(
    check_id: str,
    metadata: DMetadataRequest | None,
) -> set[str]:
    """Return exactly the D leaves that a completed previewed write may change."""

    if check_id == PAIRING_WRITE:
        return set(PAIRING_SIDECARS)
    if check_id == D_PAIRING_WRITE:
        if not isinstance(metadata, PairingMetadata):
            raise StateConflictError("D pairing source whitelist is invalid")
        return {pairing_paths(anchor)[2] for anchor in metadata.anchors}
    if check_id == D_NOTE_WRITE:
        if not isinstance(metadata, AgentNoteMetadata):
            raise StateConflictError("D Agent Note source whitelist is invalid")
        return set(note_paths(metadata.anchor))
    return set()
