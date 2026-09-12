"""Replay durable lockfile handoffs as immutable worker-input overlays.

The temporary lockfile owner is not an ancestor worker. Its only permitted
effect is therefore a content-addressed replacement of pnpm-lock.yaml in
the input seen by the interrupted owner and that owner's DAG descendants.
This module keeps that replacement out of worker patches while preserving a
replayable DependencyInput tree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any

from .artifacts import ArtifactStore
from .model import canonical_json
from .worktrees import WorktreeError


_LOCKFILE_PATH = "pnpm-lock.yaml"
_LOCKFILE_HANDOFF_KIND = "lockfile-handoff-v1"
_SHA256_LENGTH = 64
_HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "state",
        "request_id",
        "request_fingerprint",
        "preview_fingerprint",
        "task_id",
        "task_revision",
        "contract_hash",
        "blocked_node_id",
        "root_node_id",
        "blocked_attempt",
        "repository",
        "original_owner",
        "temporary_owner",
        "source_allocation",
        "repair_worktree",
        "source_fingerprint",
        "accepted_input_lineage",
        "input_fingerprints",
        "manifest_fingerprint",
        "lockfile",
        "changed_importers",
        "frozen_validation",
        "lease_event_cursor",
        "ready_event_cursor",
        "after_ancestors",
    }
)


class LockfileHandoffInputError(WorktreeError):
    """A ready lockfile handoff cannot be made part of a worker input."""


@dataclass(frozen=True)
class LockfileHandoff:
    """One normalized immutable replacement applied after a source closure."""

    receipt: dict[str, Any]

    @property
    def request_id(self) -> str:
        """Return the durable handoff request identifier."""

        return str(self.receipt["request_id"])

    @property
    def after_ancestors(self) -> tuple[tuple[str, int, str | None], ...]:
        """Return the exact original ancestor closure preceding this overlay."""

        return _ancestor_tuples(self.receipt["after_ancestors"], "after_ancestors")

    @property
    def artifact_ref(self) -> str:
        """Return the content-addressed repaired lockfile artifact."""

        lockfile = self.receipt["lockfile"]
        assert isinstance(lockfile, Mapping)
        return str(lockfile["artifact_ref"])

    @property
    def sha256(self) -> str:
        """Return the expected repaired lockfile digest."""

        lockfile = self.receipt["lockfile"]
        assert isinstance(lockfile, Mapping)
        return str(lockfile["sha256"])


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Return the canonical task-contract digest used by handoff receipts."""

    return sha256(canonical_json(dict(contract)).encode("utf-8")).hexdigest()


def normalize_lockfile_handoffs(
    artifacts: ArtifactStore,
    raw_handoffs: object,
    *,
    task_id: str,
    contract: Mapping[str, Any] | None = None,
    contract_hash: str | None = None,
) -> tuple[LockfileHandoff, ...]:
    """Validate and normalize a durable sequence of ready handoff receipts.

    The lifecycle reader validates the durable event record before returning
    it. This second validation protects persisted dependency-input artifacts,
    which must remain replayable even after their request is no longer the
    newest active handoff.
    """

    if isinstance(raw_handoffs, (str, bytes)) or not isinstance(raw_handoffs, Sequence):
        raise LockfileHandoffInputError("lockfile handoffs are invalid")
    if contract_hash is not None:
        expected_contract = _sha256(contract_hash, "lockfile handoff task contract_hash")
    else:
        expected_contract = contract_fingerprint(contract) if contract is not None else None
    normalized: list[LockfileHandoff] = []
    request_ids: set[str] = set()
    previous_order: tuple[int, str] | None = None
    for raw in raw_handoffs:
        handoff = _normalize_handoff(
            artifacts,
            raw,
            task_id=task_id,
            expected_contract=expected_contract,
        )
        request_id = handoff.request_id
        if request_id in request_ids:
            raise LockfileHandoffInputError("lockfile handoffs duplicate a request")
        request_ids.add(request_id)
        order = (int(handoff.receipt["ready_event_cursor"]), request_id)
        if previous_order is not None and order < previous_order:
            raise LockfileHandoffInputError("lockfile handoffs are not in durable ready order")
        previous_order = order
        normalized.append(handoff)
    return tuple(normalized)


def select_lockfile_handoffs_for_target(
    artifacts: ArtifactStore,
    task: Mapping[str, Any],
    node_id: str,
    *,
    ancestors: Sequence[Mapping[str, Any]],
    ready_handoffs: object,
) -> tuple[LockfileHandoff, ...]:
    """Select ready handoffs that apply to one original owner or descendant.

    An unrelated parallel branch never inherits a lockfile handoff. A
    descendant must prove that the resumed original owner recorded the same
    overlay in its own dependency input before it can receive the lock bytes.
    """

    task_id = _text(task.get("task_id"), "lockfile handoff task_id")
    contract = task.get("contract")
    if not isinstance(contract, Mapping):
        raise LockfileHandoffInputError("lockfile handoff task contract is invalid")
    nodes = _task_nodes(task)
    target = nodes.get(node_id)
    if target is None:
        raise LockfileHandoffInputError(f"lockfile handoff target {node_id} is missing")
    actual_ancestors = _ancestor_tuples(ancestors, "target ancestors")
    handoffs = normalize_lockfile_handoffs(
        artifacts,
        ready_handoffs,
        task_id=task_id,
        contract=contract,
        contract_hash=(
            task.get("contract_hash")
            if isinstance(task.get("contract_hash"), str)
            else None
        ),
    )
    if not handoffs:
        return ()
    target_attempt: int | None = None
    selected: list[LockfileHandoff] = []
    for handoff in handoffs:
        receipt = handoff.receipt
        blocked_node_id = str(receipt["blocked_node_id"])
        blocked_attempt = int(receipt["blocked_attempt"])
        if node_id == blocked_node_id:
            if target_attempt is None:
                target_attempt = _positive_int(
                    target.get("attempt"), "lockfile handoff target attempt"
                )
            if target_attempt < blocked_attempt + 1:
                raise LockfileHandoffInputError(
                    "lockfile handoff original owner attempt has not advanced"
                )
            if actual_ancestors != handoff.after_ancestors:
                raise LockfileHandoffInputError(
                    "lockfile handoff original owner ancestors drifted from its receipt"
                )
            selected.append(handoff)
            continue
        if not _depends_transitively(nodes, node_id, blocked_node_id):
            continue
        if not _ordered_subsequence(handoff.after_ancestors, actual_ancestors):
            raise LockfileHandoffInputError(
                "lockfile handoff descendant ancestors drifted from its receipt"
            )
        source = nodes.get(blocked_node_id)
        if source is None:
            raise LockfileHandoffInputError("lockfile handoff original owner is missing")
        if source.get("state") != "accepted":
            raise LockfileHandoffInputError(
                "lockfile handoff descendant lacks an accepted resumed owner"
            )
        source_attempt = _positive_int(
            source.get("attempt"), "lockfile handoff resumed owner attempt"
        )
        if source_attempt < blocked_attempt + 1:
            raise LockfileHandoffInputError(
                "lockfile handoff descendant retains the pre-handoff owner attempt"
            )
        if not _source_records_handoff(artifacts, source, handoff):
            raise LockfileHandoffInputError(
                "lockfile handoff descendant lacks the resumed owner input receipt"
            )
        selected.append(handoff)
    return tuple(selected)


def apply_pending_lockfile_handoffs(
    worktree: Path,
    artifacts: ArtifactStore,
    handoffs: Sequence[LockfileHandoff],
    applied_ancestors: Sequence[Mapping[str, Any]],
    *,
    applied_request_ids: set[str],
    manifest_phase: str = "baseline",
) -> None:
    """Apply every handoff whose recorded predecessor closure is now present.

    Baseline replay proves the target manifests still match the sealed
    accepted-input tree. Recovery preparation instead uses the prepared phase
    after the worker's own source patch has restored the manifest bytes that
    the handoff importer plan validated.
    """

    if manifest_phase not in {"baseline", "prepared"}:
        raise LockfileHandoffInputError("lockfile handoff manifest phase is invalid")
    actual = _ancestor_tuples(applied_ancestors, "applied ancestors")
    for handoff in handoffs:
        if handoff.request_id in applied_request_ids:
            continue
        if not _ordered_subsequence(handoff.after_ancestors, actual):
            continue
        _apply_handoff(worktree, artifacts, handoff, manifest_phase=manifest_phase)
        applied_request_ids.add(handoff.request_id)


def require_all_handoffs_applied(
    worktree: Path,
    artifacts: ArtifactStore,
    handoffs: Sequence[LockfileHandoff],
    *,
    applied_request_ids: set[str],
) -> None:
    """Require every selected handoff exactly once and retain its final bytes."""

    expected_ids = {handoff.request_id for handoff in handoffs}
    if applied_request_ids != expected_ids:
        raise LockfileHandoffInputError(
            "lockfile handoff predecessor closure was not replayed before overlay"
        )
    if handoffs:
        _require_after_bytes(worktree, artifacts, handoffs[-1])


def validate_lockfile_handoff_manifests(
    worktree: Path,
    handoffs: Sequence[LockfileHandoff],
    *,
    manifest_phase: str,
) -> None:
    """Validate the sealed baseline or prepared manifest bytes without writing."""

    if manifest_phase not in {"baseline", "prepared"}:
        raise LockfileHandoffInputError("lockfile handoff manifest phase is invalid")
    for handoff in handoffs:
        if manifest_phase == "baseline":
            _validate_baseline_manifest_files(worktree, handoff)
        else:
            _validate_prepared_manifest_files(worktree, handoff)


def derive_lockfile_handoff_input_tree(
    worktree: Path,
    baseline_tree_sha: str,
    artifacts: ArtifactStore,
    handoffs: Sequence[LockfileHandoff],
) -> str:
    """Create an index tree with only the final lockfile replacement changed.

    The real worktree can already contain a recovered worker patch. A
    temporary index starts from the old dependency-input tree and stages only
    pnpm-lock.yaml so that source recovery never folds worker code into the
    new input baseline.
    """

    if not handoffs:
        return _resolve_tree(worktree, baseline_tree_sha)
    _require_after_bytes(worktree, artifacts, handoffs[-1])
    lockfile = _lockfile_path(worktree)
    mode = _tree_file_mode(worktree, baseline_tree_sha, _LOCKFILE_PATH)
    data = lockfile.read_bytes()
    descriptor, index_path = tempfile.mkstemp(prefix="codex-workbench-lockfile-index-")
    os.close(descriptor)
    Path(index_path).unlink(missing_ok=True)
    environment = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        _git_bytes(worktree, "read-tree", baseline_tree_sha, environment=environment)
        blob_sha = _git_bytes(
            worktree,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=data,
            environment=environment,
        ).decode("ascii", errors="strict").strip()
        if not _is_sha256_like_git_object(blob_sha):
            raise LockfileHandoffInputError("cannot write lockfile overlay blob")
        _git_bytes(
            worktree,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{mode},{blob_sha},{_LOCKFILE_PATH}",
            environment=environment,
        )
        tree = _git_bytes(worktree, "write-tree", environment=environment).decode(
            "ascii", errors="strict"
        ).strip()
        return _resolve_tree(worktree, tree)
    finally:
        Path(index_path).unlink(missing_ok=True)
        Path(index_path + ".lock").unlink(missing_ok=True)


def recorded_handoff_identity(handoff: LockfileHandoff) -> tuple[str, int, str, str]:
    """Return the fields that bind a handoff to a recorded worker input."""

    return (
        handoff.request_id,
        int(handoff.receipt["ready_event_cursor"]),
        handoff.artifact_ref,
        handoff.sha256,
    )


def _normalize_handoff(
    artifacts: ArtifactStore,
    raw: object,
    *,
    task_id: str,
    expected_contract: str | None,
) -> LockfileHandoff:
    if not isinstance(raw, Mapping) or set(raw) != _HANDOFF_FIELDS:
        raise LockfileHandoffInputError("lockfile handoff receipt has an invalid shape")
    if raw.get("schema_version") != 1 or raw.get("kind") != _LOCKFILE_HANDOFF_KIND:
        raise LockfileHandoffInputError("lockfile handoff receipt schema is unsupported")
    if raw.get("state") != "ready":
        raise LockfileHandoffInputError("lockfile handoff receipt is not ready")
    normalized = {key: raw[key] for key in _HANDOFF_FIELDS}
    for key in (
        "request_id",
        "request_fingerprint",
        "preview_fingerprint",
        "task_id",
        "contract_hash",
        "blocked_node_id",
        "root_node_id",
        "repository",
        "original_owner",
        "temporary_owner",
        "repair_worktree",
        "manifest_fingerprint",
    ):
        _text(normalized[key], f"lockfile handoff {key}")
    for key in ("request_fingerprint", "preview_fingerprint", "contract_hash", "manifest_fingerprint"):
        _sha256(normalized[key], f"lockfile handoff {key}")
    if normalized["task_id"] != task_id:
        raise LockfileHandoffInputError("lockfile handoff receipt belongs to another task")
    if expected_contract is not None and normalized["contract_hash"] != expected_contract:
        raise LockfileHandoffInputError("lockfile handoff receipt has another contract")
    if normalized["root_node_id"] != normalized["blocked_node_id"]:
        raise LockfileHandoffInputError("lockfile handoff root owner is inconsistent")
    for key in ("task_revision", "blocked_attempt", "lease_event_cursor", "ready_event_cursor"):
        _positive_int(normalized[key], f"lockfile handoff {key}")
    _source_allocation(normalized["source_allocation"])
    _source_fingerprint(normalized["source_fingerprint"])
    lineage = _lineage(normalized["accepted_input_lineage"])
    after = _ancestor_tuples(normalized["after_ancestors"], "lockfile handoff after_ancestors")
    if after != _ancestor_tuples(lineage["ancestors"], "lockfile handoff lineage ancestors"):
        raise LockfileHandoffInputError(
            "lockfile handoff after_ancestors does not match accepted input lineage"
        )
    _input_fingerprints(normalized["input_fingerprints"])
    input_fingerprints = normalized["input_fingerprints"]
    assert isinstance(input_fingerprints, Mapping)
    manifest_files = input_fingerprints["manifest_files"]
    assert isinstance(manifest_files, Mapping)
    if sha256(canonical_json(dict(manifest_files)).encode("utf-8")).hexdigest() != normalized[
        "manifest_fingerprint"
    ]:
        raise LockfileHandoffInputError(
            "lockfile handoff manifest_fingerprint does not match manifest_files"
        )
    if (
        isinstance(normalized["changed_importers"], (str, bytes))
        or not isinstance(normalized["changed_importers"], Sequence)
    ):
        raise LockfileHandoffInputError("lockfile handoff changed_importers are invalid")
    for changed_importer in normalized["changed_importers"]:
        _changed_importer(changed_importer)
    _frozen_validation(normalized["frozen_validation"])
    lockfile = _lockfile(normalized["lockfile"])
    frozen = normalized["frozen_validation"]
    assert isinstance(frozen, Mapping)
    if frozen["lockfile_sha256"] != lockfile["sha256"]:
        raise LockfileHandoffInputError(
            "lockfile handoff frozen_validation differs from repaired lockfile"
        )
    fingerprints = input_fingerprints
    assert isinstance(fingerprints, Mapping)
    input_files = fingerprints["input_files"]
    assert isinstance(input_files, Mapping)
    if input_files.get(_LOCKFILE_PATH) != lockfile["before_sha256"]:
        raise LockfileHandoffInputError(
            "lockfile handoff input_files disagree with lockfile before bytes"
        )
    source_fingerprint = normalized["source_fingerprint"]
    assert isinstance(source_fingerprint, Mapping)
    if source_fingerprint["input_tree_sha"] != lineage["input_tree_sha"]:
        raise LockfileHandoffInputError(
            "lockfile handoff source fingerprint disagrees with input lineage"
        )
    _verify_lineage_artifact(artifacts, lineage)
    _verify_artifact_digest(
        artifacts,
        str(lockfile["before_artifact_ref"]),
        str(lockfile["before_sha256"]),
        "lockfile handoff before artifact",
    )
    _verify_artifact_digest(
        artifacts,
        str(lockfile["artifact_ref"]),
        str(lockfile["sha256"]),
        "lockfile handoff artifact",
    )
    return LockfileHandoff(normalized)


def _source_allocation(raw: object) -> None:
    value = _require_mapping(raw, "lockfile handoff source_allocation")
    required = {"allocation_id", "worktree", "branch", "base_sha"}
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff source_allocation is invalid")
    for key in required:
        _text(value[key], f"lockfile handoff source_allocation {key}")


def _source_fingerprint(raw: object) -> None:
    value = _require_mapping(raw, "lockfile handoff source_fingerprint")
    required = {"sha256", "source_patch_sha256", "input_tree_sha"}
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff source_fingerprint is invalid")
    _sha256(value["sha256"], "lockfile handoff source_fingerprint sha256")
    _sha256(
        value["source_patch_sha256"],
        "lockfile handoff source_fingerprint source_patch_sha256",
    )
    _text(value["input_tree_sha"], "lockfile handoff source_fingerprint input_tree_sha")


def _lineage(raw: object) -> Mapping[str, Any]:
    value = _require_mapping(raw, "lockfile handoff accepted_input_lineage")
    required = {"ref", "receipt", "input_tree_sha", "ancestors"}
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff accepted_input_lineage is invalid")
    ref = value["ref"]
    if ref is not None:
        _text(ref, "lockfile handoff accepted_input_lineage ref")
    receipt = value["receipt"]
    if not isinstance(receipt, Mapping):
        raise LockfileHandoffInputError(
            "lockfile handoff accepted_input_lineage receipt is invalid"
        )
    _text(value["input_tree_sha"], "lockfile handoff accepted_input_lineage input_tree_sha")
    ancestors = _ancestor_tuples(
        value["ancestors"], "lockfile handoff accepted_input_lineage ancestors"
    )
    if (
        receipt.get("input_tree_sha") != value["input_tree_sha"]
        or _ancestor_tuples(
            receipt.get("ancestors"),
            "lockfile handoff accepted_input_lineage receipt ancestors",
        )
        != ancestors
    ):
        raise LockfileHandoffInputError(
            "lockfile handoff accepted_input_lineage receipt disagrees with its projection"
        )
    return value


def _verify_lineage_artifact(
    artifacts: ArtifactStore,
    lineage: Mapping[str, Any],
) -> None:
    ref = lineage["ref"]
    if ref is None:
        return
    assert isinstance(ref, str)
    try:
        payload = json.loads(artifacts.verify(ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise LockfileHandoffInputError(
            "lockfile handoff accepted_input_lineage artifact is unavailable"
        ) from error
    receipt = lineage["receipt"]
    assert isinstance(receipt, Mapping)
    if not isinstance(payload, Mapping) or canonical_json(dict(payload)) != canonical_json(
        dict(receipt)
    ):
        raise LockfileHandoffInputError(
            "lockfile handoff accepted_input_lineage artifact drifted from its receipt"
        )


def _input_fingerprints(raw: object) -> None:
    value = _require_mapping(raw, "lockfile handoff input_fingerprints")
    required = {"input_files", "manifest_files", "workspace_entries", "engine_fingerprint"}
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff input_fingerprints are invalid")
    normalized_files: dict[str, dict[str, str]] = {}
    for field in ("input_files", "manifest_files"):
        files = _require_mapping(value[field], f"lockfile handoff {field}")
        if not files:
            raise LockfileHandoffInputError(f"lockfile handoff {field} are empty")
        normalized: dict[str, str] = {}
        for path, digest in files.items():
            relative = _relative_input_path(path, f"lockfile handoff {field} path")
            normalized[relative.as_posix()] = _sha256(
                digest, f"lockfile handoff {field} digest"
            )
        normalized_files[field] = normalized
    if (
        not set(normalized_files["manifest_files"]).issubset(
            normalized_files["input_files"]
        )
        or any(
            normalized_files["input_files"][path] != digest
            for path, digest in normalized_files["manifest_files"].items()
        )
    ):
        raise LockfileHandoffInputError(
            "lockfile handoff manifest_files differ from input_files"
        )
    if _LOCKFILE_PATH not in normalized_files["input_files"]:
        raise LockfileHandoffInputError(
            "lockfile handoff input_files omit pnpm-lock.yaml"
        )
    if any(not path.endswith("/package.json") and path != "package.json" for path in normalized_files["manifest_files"]):
        raise LockfileHandoffInputError(
            "lockfile handoff manifest_files contain a non-package manifest"
        )
    if isinstance(value["workspace_entries"], (str, bytes)) or not isinstance(
        value["workspace_entries"], Sequence
    ):
        raise LockfileHandoffInputError("lockfile handoff workspace_entries are invalid")
    previous_entry: tuple[int, str, str, str] | None = None
    for entry in value["workspace_entries"]:
        normalized_entry = _workspace_entry(entry)
        order = (
            0 if normalized_entry["importer"] == "." else 1,
            str(normalized_entry["importer"]),
            str(normalized_entry["section"]),
            str(normalized_entry["name"]),
        )
        if previous_entry is not None and order < previous_entry:
            raise LockfileHandoffInputError(
                "lockfile handoff workspace_entries are not in deterministic order"
            )
        previous_entry = order
    _sha256(value["engine_fingerprint"], "lockfile handoff engine_fingerprint")


def _lockfile(raw: object) -> Mapping[str, Any]:
    value = _require_mapping(raw, "lockfile handoff lockfile")
    required = {
        "path",
        "before_artifact_ref",
        "before_sha256",
        "artifact_ref",
        "sha256",
    }
    if set(value) != required or value.get("path") != _LOCKFILE_PATH:
        raise LockfileHandoffInputError("lockfile handoff lockfile is invalid")
    for key in ("before_artifact_ref", "artifact_ref"):
        _text(value[key], f"lockfile handoff lockfile {key}")
    for key in ("before_sha256", "sha256"):
        _sha256(value[key], f"lockfile handoff lockfile {key}")
    return value


def _frozen_validation(raw: object) -> None:
    value = _require_mapping(raw, "lockfile handoff frozen_validation")
    required = {
        "kind",
        "ready",
        "pnpm_version",
        "lockfile_sha256",
        "command_exit_codes",
        "template_state",
    }
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff frozen_validation is invalid")
    _text(value["kind"], "lockfile handoff frozen_validation kind")
    if value["ready"] is not True:
        raise LockfileHandoffInputError("lockfile handoff frozen_validation is not ready")
    _text(value["pnpm_version"], "lockfile handoff frozen_validation pnpm_version")
    _sha256(
        value["lockfile_sha256"],
        "lockfile handoff frozen_validation lockfile_sha256",
    )
    if (
        isinstance(value["command_exit_codes"], (str, bytes))
        or not isinstance(value["command_exit_codes"], Sequence)
        or not value["command_exit_codes"]
        or not all(
            isinstance(exit_code, int) and not isinstance(exit_code, bool)
            and exit_code == 0 for exit_code in value["command_exit_codes"]
        )
    ):
        raise LockfileHandoffInputError(
            "lockfile handoff frozen_validation command_exit_codes are invalid"
        )
    _text(
        value["template_state"],
        "lockfile handoff frozen_validation template_state",
    )


def _workspace_entry(raw: object) -> Mapping[str, Any]:
    value = _require_mapping(raw, "lockfile handoff workspace_entry")
    required = {
        "importer",
        "manifest",
        "package_name",
        "section",
        "name",
        "specifier",
        "version",
        "target_importer",
        "status",
    }
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff workspace_entry is invalid")
    for key in (
        "importer",
        "manifest",
        "name",
        "specifier",
        "version",
        "target_importer",
    ):
        _text(value[key], f"lockfile handoff workspace_entry {key}")
    _workspace_importer(
        value["importer"], "lockfile handoff workspace_entry importer"
    )
    _workspace_importer(
        value["target_importer"],
        "lockfile handoff workspace_entry target_importer",
    )
    manifest = _relative_input_path(
        value["manifest"], "lockfile handoff workspace_entry manifest"
    )
    if not manifest.as_posix().endswith("package.json"):
        raise LockfileHandoffInputError(
            "lockfile handoff workspace_entry manifest is invalid"
        )
    if value["package_name"] is not None:
        _text(value["package_name"], "lockfile handoff workspace_entry package_name")
    if value["section"] not in {
        "dependencies",
        "devDependencies",
        "optionalDependencies",
    }:
        raise LockfileHandoffInputError("lockfile handoff workspace_entry section is invalid")
    if not str(value["version"]).startswith("link:"):
        raise LockfileHandoffInputError("lockfile handoff workspace_entry version is invalid")
    if not str(value["specifier"]).startswith("workspace:"):
        raise LockfileHandoffInputError(
            "lockfile handoff workspace_entry specifier is invalid"
        )
    if value["status"] not in {"present", "missing"}:
        raise LockfileHandoffInputError("lockfile handoff workspace_entry status is invalid")
    return value


def _changed_importer(raw: object) -> Mapping[str, Any]:
    value = _require_mapping(raw, "lockfile handoff changed_importer")
    required = {"importer", "manifest", "created", "added"}
    if set(value) != required:
        raise LockfileHandoffInputError("lockfile handoff changed_importer is invalid")
    _workspace_importer(value["importer"], "lockfile handoff changed_importer importer")
    manifest = _relative_input_path(
        value["manifest"], "lockfile handoff changed_importer manifest"
    )
    if not manifest.as_posix().endswith("package.json"):
        raise LockfileHandoffInputError(
            "lockfile handoff changed_importer manifest is invalid"
        )
    if not isinstance(value["created"], bool):
        raise LockfileHandoffInputError("lockfile handoff changed_importer created is invalid")
    added = value["added"]
    if isinstance(added, (str, bytes)) or not isinstance(added, Sequence):
        raise LockfileHandoffInputError("lockfile handoff changed_importer added is invalid")
    for entry in added:
        item = _require_mapping(entry, "lockfile handoff changed_importer added entry")
        required_added = {
            "section",
            "name",
            "specifier",
            "version",
            "target_importer",
        }
        if set(item) != required_added:
            raise LockfileHandoffInputError(
                "lockfile handoff changed_importer added entry is invalid"
            )
        for key in required_added:
            _text(item[key], f"lockfile handoff changed_importer added {key}")
        if item["section"] not in {
            "dependencies",
            "devDependencies",
            "optionalDependencies",
        }:
            raise LockfileHandoffInputError(
                "lockfile handoff changed_importer added section is invalid"
            )
        if not str(item["version"]).startswith("link:"):
            raise LockfileHandoffInputError(
                "lockfile handoff changed_importer added version is invalid"
            )
        if not str(item["specifier"]).startswith("workspace:"):
            raise LockfileHandoffInputError(
                "lockfile handoff changed_importer added specifier is invalid"
            )
        _workspace_importer(
            item["target_importer"],
            "lockfile handoff changed_importer added target_importer",
        )
    return value


def _apply_handoff(
    worktree: Path,
    artifacts: ArtifactStore,
    handoff: LockfileHandoff,
    *,
    manifest_phase: str,
) -> None:
    if manifest_phase == "baseline":
        _validate_baseline_manifest_files(worktree, handoff)
    else:
        _validate_prepared_manifest_files(worktree, handoff)
    lockfile = handoff.receipt["lockfile"]
    assert isinstance(lockfile, Mapping)
    before = _artifact_bytes(
        artifacts,
        str(lockfile["before_artifact_ref"]),
        str(lockfile["before_sha256"]),
        "lockfile handoff before artifact",
    )
    after = _artifact_bytes(
        artifacts,
        str(lockfile["artifact_ref"]),
        str(lockfile["sha256"]),
        "lockfile handoff artifact",
    )
    path = _lockfile_path(worktree)
    try:
        current = path.read_bytes()
    except OSError as error:
        raise LockfileHandoffInputError("lockfile handoff target lockfile is unavailable") from error
    if current != before:
        raise LockfileHandoffInputError(
            "lockfile handoff target lockfile does not match its recorded before bytes"
        )
    _replace_file_bytes(path, after)
    _require_after_bytes(worktree, artifacts, handoff)


def _validate_prepared_manifest_files(worktree: Path, handoff: LockfileHandoff) -> None:
    fingerprints = handoff.receipt["input_fingerprints"]
    assert isinstance(fingerprints, Mapping)
    manifest_files = fingerprints["manifest_files"]
    assert isinstance(manifest_files, Mapping)
    for raw_path, expected in manifest_files.items():
        relative = _relative_input_path(raw_path, "lockfile handoff manifest path")
        path = worktree / relative
        if path.is_symlink() or not path.is_file():
            raise LockfileHandoffInputError(
                f"lockfile handoff manifest is unavailable: {relative.as_posix()}"
            )
        try:
            actual = sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise LockfileHandoffInputError(
                f"lockfile handoff cannot read manifest: {relative.as_posix()}"
            ) from error
        if actual != expected:
            raise LockfileHandoffInputError(
                f"lockfile handoff manifest drifted: {relative.as_posix()}"
            )


def _validate_baseline_manifest_files(worktree: Path, handoff: LockfileHandoff) -> None:
    lineage = handoff.receipt["accepted_input_lineage"]
    assert isinstance(lineage, Mapping)
    input_tree_sha = _text(
        lineage["input_tree_sha"],
        "lockfile handoff accepted_input_lineage input_tree_sha",
    )
    _resolve_tree(worktree, input_tree_sha)
    fingerprints = handoff.receipt["input_fingerprints"]
    assert isinstance(fingerprints, Mapping)
    manifest_files = fingerprints["manifest_files"]
    assert isinstance(manifest_files, Mapping)
    for raw_path in manifest_files:
        relative = _relative_input_path(raw_path, "lockfile handoff manifest path")
        path = worktree / relative
        if path.is_symlink() or not path.is_file():
            raise LockfileHandoffInputError(
                f"lockfile handoff baseline manifest is unavailable: {relative.as_posix()}"
            )
        try:
            expected = _git_bytes(
                worktree,
                "show",
                f"{input_tree_sha}:{relative.as_posix()}",
            )
            actual = path.read_bytes()
        except (LockfileHandoffInputError, OSError) as error:
            raise LockfileHandoffInputError(
                f"lockfile handoff baseline manifest is unavailable: {relative.as_posix()}"
            ) from error
        if sha256(actual).hexdigest() != sha256(expected).hexdigest():
            raise LockfileHandoffInputError(
                f"lockfile handoff baseline manifest drifted: {relative.as_posix()}"
            )


def _require_after_bytes(
    worktree: Path,
    artifacts: ArtifactStore,
    handoff: LockfileHandoff,
) -> None:
    lockfile = handoff.receipt["lockfile"]
    assert isinstance(lockfile, Mapping)
    expected = _artifact_bytes(
        artifacts,
        str(lockfile["artifact_ref"]),
        str(lockfile["sha256"]),
        "lockfile handoff artifact",
    )
    path = _lockfile_path(worktree)
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise LockfileHandoffInputError("lockfile handoff target lockfile is unavailable") from error
    if actual != expected:
        raise LockfileHandoffInputError(
            "lockfile handoff target lockfile does not match its recorded repaired bytes"
        )


def _source_records_handoff(
    artifacts: ArtifactStore,
    source: Mapping[str, Any],
    handoff: LockfileHandoff,
) -> bool:
    result = source.get("result")
    artifacts_map = result.get("artifacts") if isinstance(result, Mapping) else None
    ref = artifacts_map.get("dependency-input") if isinstance(artifacts_map, Mapping) else None
    if not isinstance(ref, str) or not ref:
        return False
    try:
        payload = json.loads(artifacts.verify(ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 2:
        return False
    raw_handoffs = payload.get("lockfile_handoffs")
    if isinstance(raw_handoffs, (str, bytes)) or not isinstance(raw_handoffs, Sequence):
        return False
    identity = recorded_handoff_identity(handoff)
    for raw in raw_handoffs:
        if not isinstance(raw, Mapping):
            continue
        try:
            lockfile = _require_mapping(
                raw.get("lockfile"), "recorded lockfile handoff lockfile"
            )
            candidate = (
                _text(raw.get("request_id"), "recorded lockfile handoff request_id"),
                _positive_int(
                    raw.get("ready_event_cursor"),
                    "recorded lockfile handoff ready_event_cursor",
                ),
                _text(
                    lockfile.get("artifact_ref"),
                    "recorded lockfile handoff artifact_ref",
                ),
                _text(
                    lockfile.get("sha256"),
                    "recorded lockfile handoff sha256",
                ),
            )
        except LockfileHandoffInputError:
            continue
        if candidate == identity:
            return canonical_json(dict(raw)) == canonical_json(handoff.receipt)
    return False


def _task_nodes(task: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_nodes = task.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise LockfileHandoffInputError("lockfile handoff task nodes are invalid")
    nodes: dict[str, Mapping[str, Any]] = {}
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise LockfileHandoffInputError("lockfile handoff task node is invalid")
        node_id = _text(raw.get("node_id"), "lockfile handoff task node_id")
        if node_id in nodes:
            raise LockfileHandoffInputError("lockfile handoff task duplicates a node")
        nodes[node_id] = raw
    return nodes


def _depends_transitively(
    nodes: Mapping[str, Mapping[str, Any]],
    target_node_id: str,
    expected_ancestor_id: str,
) -> bool:
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visited:
            return False
        visited.add(node_id)
        node = nodes.get(node_id)
        if node is None:
            raise LockfileHandoffInputError(
                f"lockfile handoff dependency {node_id} is missing"
            )
        dependencies = node.get("depends_on", ())
        if isinstance(dependencies, (str, bytes)) or not isinstance(dependencies, Sequence):
            raise LockfileHandoffInputError("lockfile handoff node dependencies are invalid")
        for dependency_id in dependencies:
            if not isinstance(dependency_id, str) or not dependency_id:
                raise LockfileHandoffInputError("lockfile handoff node dependency is invalid")
            if dependency_id == expected_ancestor_id or visit(dependency_id):
                return True
        return False

    return visit(target_node_id)


def _ancestor_tuples(
    raw: object,
    label: str,
) -> tuple[tuple[str, int, str | None], ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise LockfileHandoffInputError(f"{label} are invalid")
    result: list[tuple[str, int, str | None]] = []
    seen_node_ids: set[str] = set()
    for source in raw:
        if not isinstance(source, Mapping) or set(source) != {
            "node_id",
            "attempt",
            "patch_ref",
        }:
            raise LockfileHandoffInputError(f"{label} entry is invalid")
        node_id = _text(source["node_id"], f"{label} node_id")
        if node_id in seen_node_ids:
            raise LockfileHandoffInputError(f"{label} duplicate node")
        seen_node_ids.add(node_id)
        attempt = _positive_int(source["attempt"], f"{label} attempt")
        patch_ref = source["patch_ref"]
        if patch_ref is not None:
            _text(patch_ref, f"{label} patch_ref")
        result.append((node_id, attempt, patch_ref))
    return tuple(result)


def _ordered_subsequence(
    expected: Sequence[tuple[str, int, str | None]],
    actual: Sequence[tuple[str, int, str | None]],
) -> bool:
    iterator = iter(actual)
    return all(any(candidate == value for candidate in iterator) for value in expected)


def _artifact_bytes(
    artifacts: ArtifactStore,
    ref: str,
    expected_sha256: str,
    label: str,
) -> bytes:
    _sha256(expected_sha256, label)
    try:
        data = artifacts.verify(ref).read_bytes()
    except (OSError, ValueError) as error:
        raise LockfileHandoffInputError(f"{label} is unavailable") from error
    if sha256(data).hexdigest() != expected_sha256:
        raise LockfileHandoffInputError(f"{label} digest does not match its receipt")
    return data


def _verify_artifact_digest(
    artifacts: ArtifactStore,
    ref: str,
    expected_sha256: str,
    label: str,
) -> None:
    _artifact_bytes(artifacts, ref, expected_sha256, label)


def _lockfile_path(worktree: Path) -> Path:
    path = worktree / _LOCKFILE_PATH
    if path.is_symlink() or not path.is_file():
        raise LockfileHandoffInputError("lockfile handoff target lacks a regular pnpm-lock.yaml")
    return path


def _replace_file_bytes(path: Path, data: bytes) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".codex-workbench-lockfile-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise LockfileHandoffInputError("cannot replace handoff lockfile bytes") from error


def _tree_file_mode(worktree: Path, tree_sha: str, path: str) -> str:
    output = _git_bytes(worktree, "ls-tree", tree_sha, "--", path).decode(
        "utf-8", errors="strict"
    ).strip()
    fields = output.split(maxsplit=3)
    if len(fields) != 4 or fields[1] != "blob" or fields[3] != path:
        raise LockfileHandoffInputError("dependency input tree lacks pnpm-lock.yaml")
    mode = fields[0]
    if mode not in {"100644", "100755"}:
        raise LockfileHandoffInputError("dependency input lockfile has an unsupported mode")
    return mode


def _resolve_tree(worktree: Path, value: str) -> str:
    resolved = _git_bytes(worktree, "rev-parse", "--verify", f"{value}^{{tree}}").decode(
        "ascii", errors="strict"
    ).strip()
    if not resolved:
        raise LockfileHandoffInputError("lockfile handoff input tree is invalid")
    return resolved


def _git_bytes(
    worktree: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            input=input_bytes,
            capture_output=True,
            env=dict(environment) if environment is not None else None,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LockfileHandoffInputError(
            f"lockfile handoff Git command failed: {error}"
        ) from error
    if result.returncode:
        raise LockfileHandoffInputError(
            result.stderr.decode(errors="replace").strip()
            or result.stdout.decode(errors="replace").strip()
            or "lockfile handoff Git command failed"
        )
    return result.stdout


def _relative_input_path(raw: object, label: str) -> Path:
    value = _text(raw, label)
    candidate = Path(value)
    if (
        "\\" in value
        or candidate.is_absolute()
        or not value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise LockfileHandoffInputError(f"{label} is invalid")
    if any(part in {".git", "node_modules"} for part in candidate.parts):
        raise LockfileHandoffInputError(f"{label} is invalid")
    return candidate


def _workspace_importer(raw: object, label: str) -> str:
    value = _text(raw, label)
    if value == ".":
        return value
    return _relative_input_path(value, label).as_posix()


def _is_sha256_like_git_object(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _sha256(raw: object, label: str) -> str:
    value = _text(raw, label)
    if len(value) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise LockfileHandoffInputError(f"{label} is invalid")
    return value


def _positive_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise LockfileHandoffInputError(f"{label} is invalid")
    return raw


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise LockfileHandoffInputError(f"{label} is invalid")
    return raw


def _require_mapping(raw: object, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise LockfileHandoffInputError(f"{label} is invalid")
    return raw
