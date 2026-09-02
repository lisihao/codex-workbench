"""Archify vendor identity, role contracts, and truthful receipt validation.

The vendored Archify CLI proves schema and rendering constraints.  It does not
prove that a diagram describes the intended system.  This module keeps that
boundary explicit for Workbench callers and fails closed on incomplete receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .worktrees import scope_allows


ARCHIFY_REPOSITORY = "https://github.com/tt-a1i/archify"
ARCHIFY_TAG = "v2.16.0"
ARCHIFY_COMMIT = "c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de"
ARCHIFY_VERSION = "2.16.0"
ARCHIFY_LICENSE = "MIT"
SOURCE_LOCK_FILENAME = "SOURCE-LOCK.json"
CONTENT_MANIFEST_FILENAME = "CONTENT-MANIFEST.json"
CONTENT_MANIFEST_SCHEMA_VERSION = 1
# The checked-in source lock carries the same value.  Keeping the manifest
# digest in the adapter prevents a rewritten lock+manifest pair from silently
# authorizing an altered vendored core.
ARCHIFY_CONTENT_MANIFEST_SHA256 = "de13ea9f8a1c344461b35df1ca4faea4ad90673f969f618af5f3b89dac7ad950"
ARCHIFY_CONTENT_MANIFEST_FILE_COUNT = 190
ARCHIFY_CONTENT_MANIFEST_TREE_SHA256 = "daad1a13acd18647c951bf1776af278347cf59bc29671951aa6f8ea112eb050f"
SKILL_NAME = "archify"
RECEIPT_SCHEMA_VERSION = 1
WORKBENCH_RECEIPT_VERSION = 1
ARCHIFY_MANAGED_MARKER_FILENAME = ".codex-workbench-archify.json"
ARCHIFY_MANAGED_BY = "codex-workbench"
ARCHIFY_INSTALL_AGENTS = {"codex": "codex", "claude-code": "claude"}

DIAGRAM_TYPES = (
    "architecture",
    "workflow",
    "sequence",
    "dataflow",
    "lifecycle",
)

# These are the stable-core seams the Workbench integration relies on.  The
# vendor directory also contains the complete upstream archify/ tree, not just
# this allow-list; the list makes accidental stale/partial copies detectable.
REQUIRED_VENDOR_PATHS = (
    "LICENSE",
    "SKILL.md",
    "package.json",
    "skill-release.json",
    "bin/archify.mjs",
    "bin/visual-check.mjs",
    "delta/architecture-delta.mjs",
    "migrations/workflow-v2.mjs",
    "renderers/shared/generated-validators.mjs",
    "scripts/check-render-output.mjs",
    "schemas/README.md",
    "schemas/common.schema.json",
    "schemas/architecture.schema.json",
    "schemas/workflow.schema.json",
    "schemas/sequence.schema.json",
    "schemas/dataflow.schema.json",
    "schemas/lifecycle.schema.json",
    "renderers/workflow/workflow-compiler.mjs",
    "renderers/workflow/README.md",
    "renderers/dataflow/render-dataflow.mjs",
    "renderers/sequence/render-sequence.mjs",
    "renderers/lifecycle/render-lifecycle.mjs",
    "references/delivery-contract.md",
)

RENDERER_PASS_NOT_SEMANTIC = (
    "Archify renderer validation proves schema and artifact constraints; "
    "it does not prove semantic correctness or runtime causality."
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_MANIFEST_FIELDS = frozenset({"schema_version", "source", "content"})
_CONTENT_MANIFEST_SOURCE_FIELDS = frozenset({"repository", "tag", "commit", "version", "license"})
_CONTENT_MANIFEST_CONTENT_FIELDS = frozenset({"algorithm", "file_count", "tree_sha256"})
_CONTENT_MANIFEST_BINDING_FIELDS = frozenset({"path", "sha256", "file_count", "tree_sha256"})
_CONTENT_MANIFEST_EXCLUDED_FILES = frozenset(
    {SOURCE_LOCK_FILENAME, CONTENT_MANIFEST_FILENAME, ARCHIFY_MANAGED_MARKER_FILENAME}
)

_EXECUTION_BINDING_FIELDS = frozenset({"path", "sha256", "bytes"})
_EXECUTION_SEMANTIC_FIELDS = frozenset({"ok", "source"})
_EXECUTION_COMMON_FIELDS = frozenset(
    {
        "schemaVersion",
        "workbenchReceiptVersion",
        "role",
        "ok",
        "command",
        "type",
        "semantic",
    }
)
# Upstream commands deliberately have different receipt shapes.  Keep this
# table explicit: requiring ``output`` or ``specification`` for every command
# would reject valid compare/visual-check/migrate/validate receipts and would
# encourage workers to forge fields merely to satisfy the adapter.
_EXECUTION_COMMAND_FIELDS: dict[str, frozenset[str]] = {
    "validate": frozenset({"input", "checks", "composition", "engineeringProfile", "evidence"}),
    "deliver": frozenset({"input", "output", "specification", "artifact", "validation", "evidence", "open"}),
    "compare": frozenset(
        {
            "comparatorVersion",
            "canonicalVersion",
            "completeness",
            "proofLevel",
            "base",
            "head",
            "summary",
            "changes",
            "identity",
            "view",
            "limitations",
            "artifact",
            "validation",
        }
    ),
    "visual-check": frozenset(
        {
            "status",
            "visualReview",
            "artifact",
            "state",
            "chrome",
            "diagnostics",
            "containment",
            "readability",
            "viewerChrome",
            "captures",
            "sidecars",
            "error",
        }
    ),
    "migrate": frozenset(
        {
            "source",
            "destination",
            "fromSchemaVersion",
            "toSchemaVersion",
            "preExistingDiagnostics",
            "migrationDiagnostics",
            "newSchemaDiagnostics",
            "changedCoordinates",
            "oldRequiredViewBox",
            "newRequiredViewBox",
            "diagnostics",
            "error",
        }
    ),
}
# Retain one aggregate for callers that need to report the complete closed
# ABI, while command-specific validation above remains authoritative.
_EXECUTION_RECEIPT_FIELDS = _EXECUTION_COMMON_FIELDS | frozenset().union(
    *_EXECUTION_COMMAND_FIELDS.values()
)


class ArchifyContractError(ValueError):
    """Raised when a vendor or receipt violates the Workbench contract."""


@dataclass(frozen=True)
class RoleContract:
    """Machine-readable routing expectations for one Workbench role."""

    name: str
    purpose: str
    diagram_types: tuple[str, ...]
    commands: tuple[str, ...]
    semantic_gate: str
    visual_gate: str
    forbidden_claims: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.name,
            "purpose": self.purpose,
            "diagram_types": list(self.diagram_types),
            "commands": list(self.commands),
            "semantic_gate": self.semantic_gate,
            "visual_gate": self.visual_gate,
            "forbidden_claims": list(self.forbidden_claims),
        }


_FORBIDDEN_CLAIMS = (
    "renderer_pass_is_semantic_correctness",
    "authored_reachability_is_runtime_impact_or_blast_radius",
    "visual_pending_is_reviewed",
)

ROLE_CONTRACTS: dict[str, RoleContract] = {
    "architecture": RoleContract(
        name="architecture",
        purpose="Author a bounded architecture map from requirements or verified repository evidence.",
        diagram_types=("architecture",),
        commands=(
            "guide <scenario> --json",
            "validate architecture <spec.json> --quality showcase --json",
            "deliver architecture <spec.json> <artifact.html> --quality showcase --json",
        ),
        semantic_gate="external-requirements-or-revision-pinned-repository-evidence",
        visual_gate="automated visual-check plus truthful reviewer status",
        forbidden_claims=_FORBIDDEN_CLAIMS,
    ),
    "design": RoleContract(
        name="design",
        purpose="Turn one clear technical story into a readable Archify artifact across its five typed modes.",
        diagram_types=DIAGRAM_TYPES,
        commands=(
            "guide <scenario> --json",
            "validate <type> <spec.json> --quality showcase --json",
            "deliver <type> <spec.json> <artifact.html> --quality showcase --json",
        ),
        semantic_gate="external-requirements-contract-before-layout",
        visual_gate="automated containment is not perceptual review",
        forbidden_claims=_FORBIDDEN_CLAIMS,
    ),
    "review": RoleContract(
        name="review",
        purpose="Review authored changes and evidence without inventing operational risk or mergeability.",
        diagram_types=DIAGRAM_TYPES,
        commands=(
            "validate <type> <spec.json> --quality showcase --json",
            "compare architecture <base.json> <head.json> <delta.html> --json",
            "visual-check <artifact.html> --json",
        ),
        semantic_gate="independent-requirements-or-code-review-evidence",
        visual_gate="reviewer must inspect the exact delivered artifact",
        forbidden_claims=_FORBIDDEN_CLAIMS
        + ("compare_proves_runtime_risk_or_merge_safety",),
    ),
    "requirements": RoleContract(
        name="requirements",
        purpose="Make required nodes, relationships, direction, and evidence explicit before diagram authoring.",
        diagram_types=DIAGRAM_TYPES,
        commands=(
            "guide <scenario> --json",
            "author an external semantic contract",
            "validate <type> <spec.json> --quality showcase --json",
        ),
        semantic_gate="required-and-directional-external-semantic-contract",
        visual_gate="not a requirements gate",
        forbidden_claims=_FORBIDDEN_CLAIMS
        + ("schema_validity_proves_requirement_satisfaction",),
    ),
}


def role_contract(role: str) -> dict[str, Any]:
    """Return a serializable role contract, failing on unknown roles."""

    try:
        return ROLE_CONTRACTS[role].to_dict()
    except KeyError as error:
        raise ArchifyContractError(
            f"unknown Archify role {role!r}; expected {', '.join(ROLE_CONTRACTS)}"
        ) from error


def repository_root() -> Path:
    """Return the Workbench root for the installed source tree."""

    return Path(__file__).resolve().parents[2]


def default_vendor_root() -> Path:
    return repository_root() / "vendor" / "archify"


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArchifyContractError(f"cannot read {label}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ArchifyContractError(f"{label} must be a JSON object: {path}")
    return value


def load_source_lock(vendor_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(vendor_root) if vendor_root is not None else default_vendor_root()
    return _read_json(root / SOURCE_LOCK_FILENAME, "Archify source lock")


def _content_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_manifest_binding(lock: Mapping[str, Any]) -> dict[str, Any]:
    binding = lock.get("content_manifest")
    if not isinstance(binding, Mapping) or set(binding) != _CONTENT_MANIFEST_BINDING_FIELDS:
        raise ArchifyContractError("Archify source lock content_manifest binding is invalid")
    path = binding.get("path")
    digest = binding.get("sha256")
    file_count = binding.get("file_count")
    tree_digest = binding.get("tree_sha256")
    if (
        path != CONTENT_MANIFEST_FILENAME
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count < 1
        or not isinstance(tree_digest, str)
        or _SHA256.fullmatch(tree_digest) is None
    ):
        raise ArchifyContractError("Archify source lock content_manifest binding is invalid")
    expected = {
        "path": CONTENT_MANIFEST_FILENAME,
        "sha256": ARCHIFY_CONTENT_MANIFEST_SHA256,
        "file_count": ARCHIFY_CONTENT_MANIFEST_FILE_COUNT,
        "tree_sha256": ARCHIFY_CONTENT_MANIFEST_TREE_SHA256,
    }
    if dict(binding) != expected:
        raise ArchifyContractError("Archify source lock content_manifest binding does not match pinned identity")
    return dict(binding)


def _vendor_content_tree(root: Path) -> tuple[str, int]:
    """Return the pinned tree digest for every regular vendor-core file.

    Source-lock and content-manifest files are deliberately excluded so the
    manifest can bind the immutable payload without hashing itself.
    """

    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ArchifyContractError(f"Archify stable core cannot contain symlinked files: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in _CONTENT_MANIFEST_EXCLUDED_FILES:
            continue
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        file_count += 1
    return digest.hexdigest(), file_count


def _verify_content_manifest(root: Path, lock: Mapping[str, Any]) -> dict[str, Any]:
    binding = _content_manifest_binding(lock)
    path = root / CONTENT_MANIFEST_FILENAME
    if path.is_symlink() or not path.is_file():
        raise ArchifyContractError(f"Archify content manifest is missing: {path}")
    if _content_sha256(path) != binding["sha256"]:
        raise ArchifyContractError("Archify content manifest digest does not match the source lock")
    manifest = _read_json(path, "Archify content manifest")
    if set(manifest) != _CONTENT_MANIFEST_FIELDS:
        raise ArchifyContractError("Archify content manifest has unknown or missing fields")
    if manifest.get("schema_version") != CONTENT_MANIFEST_SCHEMA_VERSION:
        raise ArchifyContractError("Archify content manifest schema version is unsupported")
    expected_source = {
        "repository": ARCHIFY_REPOSITORY,
        "tag": ARCHIFY_TAG,
        "commit": ARCHIFY_COMMIT,
        "version": ARCHIFY_VERSION,
        "license": ARCHIFY_LICENSE,
    }
    source = manifest.get("source")
    if not isinstance(source, Mapping) or set(source) != _CONTENT_MANIFEST_SOURCE_FIELDS:
        raise ArchifyContractError("Archify content manifest source identity is invalid")
    if dict(source) != expected_source:
        raise ArchifyContractError("Archify content manifest source identity does not match the source lock")
    content = manifest.get("content")
    if not isinstance(content, Mapping) or set(content) != _CONTENT_MANIFEST_CONTENT_FIELDS:
        raise ArchifyContractError("Archify content manifest payload has unknown or missing fields")
    if (
        content.get("algorithm") != "sha256-path-size-bytes-v1"
        or content.get("file_count") != binding["file_count"]
        or content.get("tree_sha256") != binding["tree_sha256"]
    ):
        raise ArchifyContractError("Archify content manifest does not match the source lock")
    actual_tree, actual_file_count = _vendor_content_tree(root)
    if actual_file_count != content["file_count"] or actual_tree != content["tree_sha256"]:
        raise ArchifyContractError("Archify content manifest does not match the vendored core")
    return {
        "path": CONTENT_MANIFEST_FILENAME,
        "sha256": binding["sha256"],
        "file_count": binding["file_count"],
        "tree_sha256": binding["tree_sha256"],
    }


def verify_vendor(vendor_root: str | Path | None = None) -> dict[str, Any]:
    """Verify the vendored core is exactly the pinned stable source.

    No network is used.  The source lock and local package manifest must agree;
    the explicit required-file list prevents an older DSH snapshot from
    masquerading as the stable core.
    """

    root = Path(vendor_root) if vendor_root is not None else default_vendor_root()
    if root.is_symlink() or not root.is_dir():
        raise ArchifyContractError(f"Archify vendor directory is missing: {root}")
    lock = load_source_lock(root)
    package = _read_json(root / "package.json", "Archify package manifest")
    expected = {
        "repository": ARCHIFY_REPOSITORY,
        "tag": ARCHIFY_TAG,
        "commit": ARCHIFY_COMMIT,
        "version": ARCHIFY_VERSION,
        "license": ARCHIFY_LICENSE,
    }
    mismatches = {
        key: {"expected": value, "actual": lock.get(key)}
        for key, value in expected.items()
        if lock.get(key) != value
    }
    if mismatches:
        raise ArchifyContractError(f"Archify source lock mismatch: {mismatches}")
    package_mismatches = {
        "version": {"expected": ARCHIFY_VERSION, "actual": package.get("version")},
        "license": {"expected": ARCHIFY_LICENSE, "actual": package.get("license")},
    }
    if any(item["expected"] != item["actual"] for item in package_mismatches.values()):
        raise ArchifyContractError(f"Archify package identity mismatch: {package_mismatches}")
    symlinked = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_symlink()]
    if symlinked:
        raise ArchifyContractError(
            f"Archify stable core cannot contain symlinked files: {', '.join(symlinked)}"
        )
    missing = [relative for relative in REQUIRED_VENDOR_PATHS if not (root / relative).is_file()]
    if missing:
        raise ArchifyContractError(f"Archify stable core is incomplete; missing: {', '.join(missing)}")
    license_files = lock.get("license_files", ["LICENSE"])
    if not isinstance(license_files, list):
        raise ArchifyContractError("Archify source lock license_files must be a list")
    for license_file in license_files:
        if not isinstance(license_file, str) or not license_file:
            raise ArchifyContractError("Archify source lock license_files must contain paths")
        license_path = root / license_file
        if license_path.is_symlink() or not license_path.is_file():
            raise ArchifyContractError(f"Archify MIT notice is missing: {license_path}")
    if not (root / "LICENSE").is_file():
        raise ArchifyContractError(f"Archify MIT notice is missing: {root / 'LICENSE'}")
    if "MIT" not in (root / "LICENSE").read_text(encoding="utf-8", errors="replace"):
        raise ArchifyContractError(f"Archify LICENSE does not identify MIT: {root / 'LICENSE'}")
    content_manifest = _verify_content_manifest(root, lock)
    return {
        "ok": True,
        "repository": ARCHIFY_REPOSITORY,
        "tag": ARCHIFY_TAG,
        "commit": ARCHIFY_COMMIT,
        "version": ARCHIFY_VERSION,
        "license": ARCHIFY_LICENSE,
        "required_file_count": len(REQUIRED_VENDOR_PATHS),
        "vendor_root": str(root),
        "content_manifest": content_manifest,
    }


def pinned_archify_cli_identity(vendor_root: str | Path | None = None) -> dict[str, Any]:
    """Return the exact core, CLI, and renderer-checker identities."""

    root = Path(vendor_root) if vendor_root is not None else default_vendor_root()
    vendor = verify_vendor(root)
    cli = root / "bin" / "archify.mjs"
    if cli.is_symlink() or not cli.is_file():
        raise ArchifyContractError(f"pinned Archify CLI is missing or a symlink: {cli}")
    checker = root / "scripts" / "check-render-output.mjs"
    if checker.is_symlink() or not checker.is_file():
        raise ArchifyContractError(f"pinned Archify renderer checker is missing or a symlink: {checker}")
    return {
        "source": {
            "repository": ARCHIFY_REPOSITORY,
            "tag": ARCHIFY_TAG,
            "commit": ARCHIFY_COMMIT,
            "version": ARCHIFY_VERSION,
            "license": ARCHIFY_LICENSE,
            "content_manifest": vendor["content_manifest"],
        },
        "cli": {
            "path": str(cli.resolve()),
            "sha256": _content_sha256(cli),
            "version": ARCHIFY_VERSION,
        },
        "checker": {
            "path": str(checker.resolve()),
            "sha256": _content_sha256(checker),
            "version": ARCHIFY_VERSION,
        },
    }


def verify_skill_projection(
    vendor_root: str | Path | None = None,
    projection: str | Path | None = None,
) -> dict[str, Any]:
    """Verify the discoverable Skill file is an exact controlled projection."""

    root = Path(vendor_root) if vendor_root is not None else default_vendor_root()
    target = (
        Path(projection)
        if projection is not None
        else repository_root() / "skills" / SKILL_NAME / "SKILL.md"
    )
    source = root / "SKILL.md"
    if source.is_symlink() or target.is_symlink():
        raise ArchifyContractError(f"Archify Skill projection cannot use symlinks: {source} -> {target}")
    if not source.is_file() or not target.is_file():
        raise ArchifyContractError(f"Archify Skill projection is missing: {source} -> {target}")
    if source.read_bytes() != target.read_bytes():
        raise ArchifyContractError(f"Archify Skill projection drifted: {target}")
    return {"ok": True, "source": str(source), "projection": str(target)}


def installed_archify_status(path: str | Path, agent: str) -> dict[str, Any]:
    """Inspect one installed Archify core without invoking a CLI or network."""

    target = Path(path)
    expected_agent = ARCHIFY_INSTALL_AGENTS.get(agent)
    status: dict[str, Any] = {
        "ok": False,
        "agent": agent,
        "target": str(target),
        "exists": target.is_dir() and not target.is_symlink(),
        "managed_marker_present": False,
        "pinned_identity_matches": False,
        "vendor_verified": False,
        "error": None,
    }
    if expected_agent is None:
        status["error"] = f"unsupported Archify installation agent: {agent}"
        return status
    if target.is_symlink() or not target.is_dir():
        status["error"] = "installed Archify target is missing or a symlink"
        return status
    marker = target / ARCHIFY_MANAGED_MARKER_FILENAME
    if marker.is_symlink() or not marker.is_file():
        status["error"] = "managed Archify marker is missing or a symlink"
        return status
    try:
        marker_value = _read_json(marker, "installed Archify marker")
    except ArchifyContractError as error:
        status["error"] = str(error)
        return status
    status["managed_marker_present"] = bool(
        marker_value.get("schema_version") == 1
        and marker_value.get("managed_by") == ARCHIFY_MANAGED_BY
        and marker_value.get("skill") == SKILL_NAME
        and marker_value.get("agent") == expected_agent
    )
    status["pinned_identity_matches"] = all(
        marker_value.get(key) == value
        for key, value in {
            "repository": ARCHIFY_REPOSITORY,
            "tag": ARCHIFY_TAG,
            "commit": ARCHIFY_COMMIT,
            "version": ARCHIFY_VERSION,
            "license": ARCHIFY_LICENSE,
        }.items()
    )
    try:
        verify_vendor(target)
    except ArchifyContractError as error:
        status["error"] = str(error)
        return status
    status["vendor_verified"] = True
    status["ok"] = bool(
        status["managed_marker_present"]
        and status["pinned_identity_matches"]
        and status["vendor_verified"]
    )
    return status


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_bytes(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _exact_int(value: Any, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _record_failure(reasons: list[str], message: str) -> None:
    reasons.append(message)


def _validate_common_identity(receipt: Mapping[str, Any], reasons: list[str]) -> str | None:
    command = receipt.get("command")
    if command not in {"validate", "deliver", "compare", "visual-check", "migrate"}:
        _record_failure(reasons, "receipt command is unsupported")
    # The stable v2 migration CLI predates the other JSON receipts and emits
    # no top-level receipt schemaVersion.  Preserve that source contract while
    # keeping every other receipt fail-closed on the Workbench receipt schema.
    if command == "migrate":
        if receipt.get("schemaVersion") not in {None, RECEIPT_SCHEMA_VERSION}:
            _record_failure(reasons, "migration receipt schemaVersion must be 1 when present")
    elif receipt.get("schemaVersion") != RECEIPT_SCHEMA_VERSION:
        _record_failure(reasons, "receipt schemaVersion must be 1")
    diagram_type = receipt.get("type")
    if diagram_type is not None and diagram_type not in DIAGRAM_TYPES:
        _record_failure(reasons, "receipt type is unsupported")
    if receipt.get("ok") is not True:
        _record_failure(reasons, "receipt ok must be true")
    return command if isinstance(command, str) else None


def _validate_nine_checks(
    validation: Mapping[str, Any] | None,
    reasons: list[str],
    *,
    compare: bool = False,
) -> bool:
    if not isinstance(validation, Mapping):
        _record_failure(reasons, "renderer validation receipt is missing")
        return False
    passed = validation.get("checksPassed")
    count = validation.get("checkCount")
    if (
        not isinstance(passed, int)
        or isinstance(passed, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or passed != count
    ):
        _record_failure(reasons, "renderer checksPassed must equal checkCount")
    if not compare and count != 9:
        _record_failure(reasons, "showcase validation must report exactly 9 artifact checks")
    if not compare and passed != 9:
        _record_failure(reasons, "showcase validation must pass all 9 artifact checks")
    if not compare:
        if validation.get("compositionProfile") != "showcase":
            _record_failure(reasons, "validation compositionProfile must be showcase")
        if validation.get("compositionStatus") != "pass":
            _record_failure(reasons, "validation compositionStatus must be pass")
        if validation.get("errors") != 0 or validation.get("warnings") != 0:
            _record_failure(reasons, "validation must report zero composition errors and warnings")
    else:
        if validation.get("baseComposition") != "pass" or validation.get("headComposition") != "pass":
            _record_failure(reasons, "compare base/head composition must both pass")
    return not reasons


def _validate_artifact_identity(
    artifact: Mapping[str, Any] | None,
    reasons: list[str],
    *,
    specification: Mapping[str, Any] | None = None,
    require_path: bool = False,
) -> None:
    if not isinstance(artifact, Mapping):
        _record_failure(reasons, "artifact receipt is missing")
        return
    if not _valid_sha256(artifact.get("sha256")):
        _record_failure(reasons, "artifact sha256 must be a lowercase SHA-256 digest")
    if not _positive_bytes(artifact.get("bytes")):
        _record_failure(reasons, "artifact bytes must be a positive integer")
    if require_path and not _is_nonempty_string(artifact.get("path")):
        _record_failure(reasons, "artifact path is missing")
    if specification is not None:
        if not _valid_sha256(specification.get("sha256")):
            _record_failure(reasons, "specification sha256 must be a lowercase SHA-256 digest")
        if not _positive_bytes(specification.get("bytes")):
            _record_failure(reasons, "specification bytes must be a positive integer")


def _semantic_status(receipt: Mapping[str, Any]) -> bool | None:
    semantic = receipt.get("semantic")
    if semantic is None:
        return None
    if not isinstance(semantic, Mapping):
        return False
    source = semantic.get("source")
    return semantic.get("ok") is True and (
        _is_nonempty_string(source) or isinstance(source, Mapping)
    )


def _role_commands(role: str) -> set[str]:
    return {command.split(" ", 1)[0] for command in ROLE_CONTRACTS[role].commands}


def _execution_root(worktree: str | Path, reasons: list[str]) -> Path | None:
    try:
        root = Path(worktree).resolve(strict=True)
    except OSError as error:
        _record_failure(reasons, f"authorized worktree is unavailable: {error}")
        return None
    if not root.is_dir():
        _record_failure(reasons, "authorized worktree is not a directory")
        return None
    return root


def _resolve_bound_file(
    value: Any,
    *,
    label: str,
    root: Path,
    allowed_scopes: Sequence[str],
    write_scopes: Sequence[str],
    require_write_scope: bool,
    reasons: list[str],
) -> Path | None:
    if not _is_nonempty_string(value):
        _record_failure(reasons, f"{label} path is missing")
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        _record_failure(reasons, f"{label} path must not be a symlink")
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        _record_failure(reasons, f"{label} file does not exist: {error}")
        return None
    if not resolved.is_file():
        _record_failure(reasons, f"{label} path must name a regular file")
        return None
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        _record_failure(reasons, f"{label} path is outside the authorized worktree")
        return None
    if not scope_allows(relative, list(allowed_scopes), []):
        _record_failure(reasons, f"{label} path is outside the node read/write scope")
        return None
    if require_write_scope and not scope_allows(relative, list(write_scopes), []):
        _record_failure(reasons, f"{label} path is outside the node write scope")
        return None
    return resolved


def _bound_file(
    value: Any,
    *,
    label: str,
    root: Path,
    allowed_scopes: Sequence[str],
    write_scopes: Sequence[str],
    require_write_scope: bool,
    reasons: list[str],
) -> Path | None:
    if not isinstance(value, Mapping):
        _record_failure(reasons, f"{label} binding is missing")
        return None
    resolved = _resolve_bound_file(
        value.get("path"),
        label=label,
        root=root,
        allowed_scopes=allowed_scopes,
        write_scopes=write_scopes,
        require_write_scope=require_write_scope,
        reasons=reasons,
    )
    if resolved is None:
        return None
    try:
        data = resolved.read_bytes()
    except OSError as error:
        _record_failure(reasons, f"{label} file cannot be read: {error}")
        return None
    actual_bytes = len(data)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if value.get("bytes") != actual_bytes:
        _record_failure(reasons, f"{label} bytes do not match the file")
    if value.get("sha256") != actual_sha256:
        _record_failure(reasons, f"{label} sha256 does not match the file")
    return resolved


def _reject_unknown_execution_fields(
    receipt: Mapping[str, Any],
    reasons: list[str],
    *,
    command: str | None,
) -> None:
    allowed = _EXECUTION_COMMON_FIELDS | _EXECUTION_COMMAND_FIELDS.get(command or "", frozenset())
    unknown = sorted(set(receipt) - allowed)
    if unknown:
        _record_failure(reasons, f"execution receipt has unknown fields: {', '.join(unknown)}")
    for label in ("specification", "artifact", "source", "destination"):
        value = receipt.get(label)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            continue
        unknown_binding = sorted(set(value) - _EXECUTION_BINDING_FIELDS)
        if unknown_binding:
            _record_failure(
                reasons,
                f"{label} binding has unknown fields: {', '.join(unknown_binding)}",
            )
    semantic = receipt.get("semantic")
    if semantic is None:
        return
    if not isinstance(semantic, Mapping):
        return
    unknown_semantic = sorted(set(semantic) - _EXECUTION_SEMANTIC_FIELDS)
    if unknown_semantic:
        _record_failure(
            reasons,
            f"semantic proof has unknown fields: {', '.join(unknown_semantic)}",
        )
    source = semantic.get("source")
    if isinstance(source, Mapping):
        unknown_source = sorted(set(source) - _EXECUTION_BINDING_FIELDS)
        if unknown_source:
            _record_failure(
                reasons,
                f"semantic.source binding has unknown fields: {', '.join(unknown_source)}",
            )


def _validate_execution_receipt(
    receipt: Mapping[str, Any],
    reasons: list[str],
    *,
    role: str | None,
    worktree: str | Path,
    read_scopes: Sequence[str],
    write_scopes: Sequence[str],
    require_output_write_scope: bool,
) -> None:
    """Bind a model-returned receipt to files the request can actually see.

    The upstream CLI does not know a Workbench node's worktree or scopes.  A
    receipt-bearing node therefore uses the minimal Workbench ABI: a role and
    version plus command-specific path/SHA-256/bytes bindings and an
    independent semantic source.  The values are recomputed here;
    model-reported hashes are never accepted as facts.
    """

    if role is None:
        _record_failure(reasons, "execution receipt requires a normalized Archify role")
        return
    command = receipt.get("command")
    _reject_unknown_execution_fields(receipt, reasons, command=command if isinstance(command, str) else None)
    if receipt.get("workbenchReceiptVersion") != WORKBENCH_RECEIPT_VERSION:
        _record_failure(reasons, "receipt workbenchReceiptVersion must be 1")
    if receipt.get("role") != role:
        _record_failure(reasons, "receipt role does not match the normalized node role")
    if command not in _role_commands(role):
        _record_failure(reasons, "receipt command is not permitted by the normalized role")
    diagram_type = receipt.get("type")
    if not (command == "visual-check" and diagram_type is None) and diagram_type not in ROLE_CONTRACTS[role].diagram_types:
        _record_failure(reasons, "receipt type is not permitted by the normalized role")
    root = _execution_root(worktree, reasons)
    if root is None:
        return
    allowed_scopes = tuple(read_scopes) + tuple(write_scopes)
    if not allowed_scopes:
        _record_failure(reasons, "receipt node has no authorized read/write scope")
        return
    output: Path | None = None
    specification: Path | None = None
    artifact: Path | None = None
    bound_inputs: list[tuple[str, Path | None]] = []

    if command == "deliver":
        output = _resolve_bound_file(
            receipt.get("output"),
            label="output",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=require_output_write_scope,
            reasons=reasons,
        )
        specification = _bound_file(
            receipt.get("specification"),
            label="specification",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=False,
            reasons=reasons,
        )
        artifact = _bound_file(
            receipt.get("artifact"),
            label="artifact",
            root=root,
            write_scopes=write_scopes,
            allowed_scopes=allowed_scopes,
            require_write_scope=require_output_write_scope,
            reasons=reasons,
        )
        if output is not None and artifact is not None and output != artifact:
            _record_failure(reasons, "output path must identify the bound artifact file")
        bound_inputs.extend((("specification", specification), ("artifact", artifact)))
    elif command == "compare":
        # compare produces a delta HTML, but its upstream receipt intentionally
        # carries base/head metadata rather than an ``output`` or
        # ``specification`` field.  Workbench adds only the artifact binding.
        artifact = _bound_file(
            receipt.get("artifact"),
            label="artifact",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=require_output_write_scope,
            reasons=reasons,
        )
        bound_inputs.append(("artifact", artifact))
    elif command == "visual-check":
        # visual-check reads an already-delivered HTML and writes sidecars; the
        # HTML itself is not a node output and therefore must remain valid from
        # a read scope even when the worker has no write scope.
        artifact = _bound_file(
            receipt.get("artifact"),
            label="artifact",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=False,
            reasons=reasons,
        )
        bound_inputs.append(("artifact", artifact))
    elif command == "validate":
        input_path = _resolve_bound_file(
            receipt.get("input"),
            label="input",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=False,
            reasons=reasons,
        )
        bound_inputs.append(("input", input_path))
    elif command == "migrate":
        source = _bound_file(
            receipt.get("source"),
            label="source",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=False,
            reasons=reasons,
        )
        destination = _bound_file(
            receipt.get("destination"),
            label="destination",
            root=root,
            allowed_scopes=allowed_scopes,
            write_scopes=write_scopes,
            require_write_scope=require_output_write_scope,
            reasons=reasons,
        )
        if source is not None and destination is not None and source == destination:
            _record_failure(reasons, "migration source and destination must be distinct files")
        bound_inputs.extend((("source", source), ("destination", destination)))
    else:
        _record_failure(reasons, "execution receipt command has no Workbench contract")
    semantic = receipt.get("semantic")
    if not isinstance(semantic, Mapping) or semantic.get("ok") is not True:
        _record_failure(reasons, "semantic proof must be an accepted object")
    else:
        semantic_source = _bound_file(
            semantic.get("source"),
            label="semantic.source",
            root=root,
            # Semantic evidence is deliberately not sourced from generated
            # output.  A worker may only cite a pre-existing read scope.
            allowed_scopes=read_scopes,
            write_scopes=(),
            require_write_scope=False,
            reasons=reasons,
        )
        if semantic_source is not None:
            relative = semantic_source.relative_to(root).as_posix()
            if scope_allows(relative, list(write_scopes), []):
                _record_failure(reasons, "semantic.source path must not be in a node write scope")
            for label, bound in bound_inputs:
                if bound is not None and semantic_source == bound:
                    _record_failure(reasons, f"semantic.source must be distinct from {label}")
            source_binding = semantic.get("source")
            if isinstance(source_binding, Mapping):
                for label, binding in (
                    (label, receipt.get(label))
                    for label, _ in bound_inputs
                ):
                    if (
                        isinstance(binding, Mapping)
                        and source_binding.get("sha256") == binding.get("sha256")
                    ):
                        _record_failure(
                            reasons,
                            f"semantic.source content must be distinct from {label}",
                        )
    # Keep the local variables explicit: a missing specification or artifact is
    # already recorded above, and the bindings must not be optimized away into
    # a model-trusted boolean.
    _ = output, specification, artifact


def validate_receipt(
    receipt: Mapping[str, Any],
    *,
    role: str | None = None,
    require_semantic: bool | None = None,
    require_visual_review: bool = False,
    worktree: str | Path | None = None,
    read_scopes: Sequence[str] = (),
    write_scopes: Sequence[str] = (),
    require_output_write_scope: bool = False,
) -> dict[str, Any]:
    """Return a truthful, machine-readable verdict for an Archify receipt.

    ``renderer_pass`` is intentionally separate from ``semantic_pass``.  A
    normal Archify CLI receipt has no semantic proof and therefore cannot be
    accepted for a role that requires it.  The function never upgrades a
    missing visual reviewer or semantic contract into success.
    """

    if not isinstance(receipt, Mapping):
        raise ArchifyContractError("Archify receipt must be a JSON object")
    if role is not None and role not in ROLE_CONTRACTS:
        raise ArchifyContractError(f"unknown Archify role {role!r}")
    reasons: list[str] = []
    command = _validate_common_identity(receipt, reasons)
    if command == "validate":
        if not _is_nonempty_string(receipt.get("input")):
            _record_failure(reasons, "validate receipt input is missing")
        checks = receipt.get("checks")
        if not isinstance(checks, list):
            _record_failure(reasons, "validate receipt checks are missing")
        else:
            if len(checks) != 9 or any(not isinstance(item, Mapping) or item.get("ok") is not True for item in checks):
                _record_failure(reasons, "validate receipt must contain 9 passing artifact checks")
        composition = receipt.get("composition")
        if not isinstance(composition, Mapping):
            _record_failure(reasons, "validate receipt composition is missing")
        else:
            if composition.get("profile") != "showcase" or composition.get("status") != "pass":
                _record_failure(reasons, "validate composition must be showcase/pass")
            summary = composition.get("summary")
            if not isinstance(summary, Mapping) or summary.get("errors") != 0 or summary.get("warnings") != 0:
                _record_failure(reasons, "validate composition must report zero errors and warnings")
    elif command == "deliver":
        if not _is_nonempty_string(receipt.get("input")):
            _record_failure(reasons, "deliver receipt input is missing")
        _validate_nine_checks(receipt.get("validation"), reasons)
        _validate_artifact_identity(
            receipt.get("artifact"),
            reasons,
            specification=receipt.get("specification"),
        )
        if not _is_nonempty_string(receipt.get("output")):
            _record_failure(reasons, "deliver receipt output path is missing")
    elif command == "compare":
        if receipt.get("completeness") != "complete":
            _record_failure(reasons, "compare receipt completeness must be complete")
        if receipt.get("proofLevel") not in {"authored", "revision-pinned"}:
            _record_failure(reasons, "compare receipt proofLevel is missing or unsupported")
        _validate_nine_checks(receipt.get("validation"), reasons, compare=True)
        _validate_artifact_identity(receipt.get("artifact"), reasons)
    elif command == "visual-check":
        if receipt.get("status") != "pass":
            _record_failure(reasons, "visual-check status must be pass")
        for key in ("containment", "captures"):
            value = receipt.get(key)
            if not isinstance(value, Mapping) or value.get("status") != "pass":
                _record_failure(reasons, f"visual-check {key} status must be pass")
        _validate_artifact_identity(receipt.get("artifact"), reasons, require_path=True)
    elif command == "migrate":
        if receipt.get("type") != "workflow":
            _record_failure(reasons, "migration receipt type must be workflow")
        if (
            receipt.get("fromSchemaVersion") not in {1, 2}
            or isinstance(receipt.get("fromSchemaVersion"), bool)
            or not _exact_int(receipt.get("toSchemaVersion"), 2)
        ):
            _record_failure(reasons, "migration receipt must describe a workflow v1/v2 to v2 transition")
        _validate_artifact_identity(receipt.get("source"), reasons, require_path=True)
        _validate_artifact_identity(receipt.get("destination"), reasons, require_path=True)

    # Renderer acceptance includes the common receipt identity as well as the
    # command-specific checks.  A structurally valid payload with the wrong
    # schema version or a false top-level ``ok`` is not a renderer pass.
    renderer_pass = not reasons
    semantic_pass = _semantic_status(receipt)
    semantic_required = role is not None if require_semantic is None else require_semantic
    if semantic_required and semantic_pass is not True:
        _record_failure(reasons, "semantic proof is required; renderer pass is not semantic correctness")

    visual_pass: bool | None = None
    if command == "visual-check":
        visual_status = receipt.get("visualReview")
        visual_pass = visual_status == "passed"
    if require_visual_review and visual_pass is not True:
        _record_failure(reasons, "an identified visual reviewer must inspect the exact artifact")

    if worktree is not None:
        _validate_execution_receipt(
            receipt,
            reasons,
            role=role,
            worktree=worktree,
            read_scopes=read_scopes,
            write_scopes=write_scopes,
            require_output_write_scope=require_output_write_scope,
        )

    return {
        "ok": not reasons,
        "command": command,
        "type": receipt.get("type"),
        "renderer_pass": renderer_pass,
        "semantic_pass": semantic_pass,
        "visual_pass": visual_pass,
        "truth": RENDERER_PASS_NOT_SEMANTIC,
        "reasons": reasons,
    }


def assert_receipt(
    receipt: Mapping[str, Any],
    *,
    role: str | None = None,
    require_semantic: bool | None = None,
    require_visual_review: bool = False,
    worktree: str | Path | None = None,
    read_scopes: Sequence[str] = (),
    write_scopes: Sequence[str] = (),
    require_output_write_scope: bool = False,
) -> dict[str, Any]:
    """Raise on a non-accepted receipt and return its normalized verdict."""

    verdict = validate_receipt(
        receipt,
        role=role,
        require_semantic=require_semantic,
        require_visual_review=require_visual_review,
        worktree=worktree,
        read_scopes=read_scopes,
        write_scopes=write_scopes,
        require_output_write_scope=require_output_write_scope,
    )
    if not verdict["ok"]:
        raise ArchifyContractError("; ".join(verdict["reasons"]))
    return verdict
