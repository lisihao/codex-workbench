"""Closed request grammar for D's bounded documentation metadata writes.

The service owns task and scope checks.  This module only turns the explicit
JSON fields for the two D checks into immutable, path-safe request values that
can be embedded in a validation plan without exposing note contents in a
receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import PurePosixPath
import re
from typing import Any, TypeAlias

from .d_integration_profile import NOTE_WRITE_ID, PAIRING_WRITE_ID


MAX_PAIR_ANCHORS = 32
MAX_NOTE_BYTES = 64 * 1024
SUPPORTED_AGENT_NOTE_KINDS = frozenset({
    "architecture",
    "bug-fix",
    "feature",
    "process",
    "simplification",
    "testing",
})
_NOTE_ANCHOR = re.compile(
    r"^\.agents/notes/implemented/"
    r"(architecture|bug-fix|feature|process|simplification|testing)/"
    r"(\d{4}-\d{2}-\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
_FORBIDDEN_PAIR_COMPONENTS = frozenset({
    "agents",
    "agents.md",
    "claude",
    "claude.md",
    "archived",
    "skills",
})


class DIntegrationMetadataError(ValueError):
    """A D metadata request is outside its fixed input grammar."""


@dataclass(frozen=True)
class PairingMetadata:
    """The finite English anchors selected for one D pairing write."""

    anchors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the non-secret selected source paths for a plan receipt."""

        return {"kind": "pairing", "pair_anchors": list(self.anchors)}


@dataclass(frozen=True)
class AgentNoteMetadata:
    """The exact two bytestrings and optional current hashes for a D Agent Note write."""

    anchor: str
    english: bytes
    chinese: bytes
    expected_english_sha256: str | None = None
    expected_chinese_sha256: str | None = None

    @property
    def chinese_path(self) -> str:
        """Return the exact paired Chinese note path."""

        return self.anchor.removesuffix(".md") + ".zh.md"

    def to_dict(self) -> dict[str, object]:
        """Return hashes and lengths without exposing note text in the journal."""

        return {
            "kind": "agent_note",
            "note_anchor": self.anchor,
            "note_chinese_path": self.chinese_path,
            "note_english_sha256": sha256(self.english).hexdigest(),
            "note_chinese_sha256": sha256(self.chinese).hexdigest(),
            "note_english_bytes": len(self.english),
            "note_chinese_bytes": len(self.chinese),
            "note_expected_english_sha256": self.expected_english_sha256,
            "note_expected_chinese_sha256": self.expected_chinese_sha256,
        }


DMetadataRequest: TypeAlias = PairingMetadata | AgentNoteMetadata


def is_d_metadata_check(check_id: object) -> bool:
    """Return whether ``check_id`` has D-only metadata fields."""

    return isinstance(check_id, str) and check_id in {PAIRING_WRITE_ID, NOTE_WRITE_ID}


def metadata_request_from_arguments(
    check_id: object,
    arguments: dict[str, Any],
) -> DMetadataRequest | None:
    """Parse exact D fields or return no metadata for the preserved B checks."""

    if check_id == PAIRING_WRITE_ID:
        return PairingMetadata(_pair_anchors(arguments.get("pair_anchors")))
    if check_id == NOTE_WRITE_ID:
        anchor = _note_anchor(arguments.get("note_anchor"))
        expected_english, expected_chinese = _paired_expected_digest(
            arguments.get("note_expected_english_sha256"),
            arguments.get("note_expected_chinese_sha256"),
        )
        return AgentNoteMetadata(
            anchor=anchor,
            english=_note_bytes(arguments.get("note_english"), "note_english"),
            chinese=_note_bytes(arguments.get("note_chinese"), "note_chinese"),
            expected_english_sha256=expected_english,
            expected_chinese_sha256=expected_chinese,
        )
    return None


def normalize_metadata_request(
    check_id: object,
    request: DMetadataRequest | None,
) -> DMetadataRequest | None:
    """Revalidate a stored request before rebuilding an immutable plan."""

    if check_id == PAIRING_WRITE_ID:
        if not isinstance(request, PairingMetadata):
            raise DIntegrationMetadataError("D pairing validation requires explicit pair_anchors")
        return PairingMetadata(_pair_anchors(list(request.anchors)))
    if check_id == NOTE_WRITE_ID:
        if not isinstance(request, AgentNoteMetadata):
            raise DIntegrationMetadataError("D Agent Note validation requires exact note metadata")
        expected_english, expected_chinese = _paired_expected_digest(
            request.expected_english_sha256,
            request.expected_chinese_sha256,
        )
        return AgentNoteMetadata(
            anchor=_note_anchor(request.anchor),
            english=_stored_note_bytes(request.english, "note_english"),
            chinese=_stored_note_bytes(request.chinese, "note_chinese"),
            expected_english_sha256=expected_english,
            expected_chinese_sha256=expected_chinese,
        )
    if request is not None:
        raise DIntegrationMetadataError("B validation checks do not accept D metadata")
    return None


def pairing_paths(anchor: str) -> tuple[str, str, str]:
    """Return the English, Chinese, and generated sidecar leaves for one anchor."""

    validated = _pair_anchor(anchor)
    stem = validated.removesuffix(".md")
    return validated, stem + ".zh.md", stem + ".i18n.yaml"


def note_paths(anchor: str) -> tuple[str, str]:
    """Return the two note leaves after revalidating the creation anchor."""

    validated = _note_anchor(anchor)
    return validated, validated.removesuffix(".md") + ".zh.md"


def _pair_anchors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PAIR_ANCHORS:
        raise DIntegrationMetadataError(
            f"pair_anchors must be a list of 1 to {MAX_PAIR_ANCHORS} exact Markdown anchors"
        )
    anchors = tuple(_pair_anchor(item) for item in value)
    if len(set(anchors)) != len(anchors):
        raise DIntegrationMetadataError("pair_anchors must not contain duplicates")
    return anchors


def _pair_anchor(value: object) -> str:
    path = _clean_relative_path(value, "pair anchor")
    parts = PurePosixPath(path).parts
    in_document_roots = parts[0] in {"docs", "packages"}
    in_implemented_notes = parts[:3] == (".agents", "notes", "implemented")
    is_cli_composition = path == "apps/cli/composition.md"
    if not (in_document_roots or in_implemented_notes or is_cli_composition):
        raise DIntegrationMetadataError(
            "pair anchors must be under docs/, packages/, .agents/notes/implemented/, "
            "or the fixed apps/cli/composition.md output"
        )
    if any(part.casefold() in _FORBIDDEN_PAIR_COMPONENTS for part in parts):
        raise DIntegrationMetadataError("pair anchor selects an excluded archived, skill, or instruction path")
    if not path.endswith(".md") or path.endswith(".zh.md"):
        raise DIntegrationMetadataError("pair anchors must name English Markdown files")
    return path


def _note_anchor(value: object) -> str:
    path = _clean_relative_path(value, "note_anchor")
    matched = _NOTE_ANCHOR.fullmatch(path)
    if matched is None:
        kinds = ", ".join(sorted(SUPPORTED_AGENT_NOTE_KINDS))
        raise DIntegrationMetadataError(
            "note_anchor must create exactly one implemented Agent Note in "
            f"one of: {kinds}"
        )
    try:
        date.fromisoformat(matched.group(2))
    except ValueError as error:
        raise DIntegrationMetadataError("note_anchor has an invalid calendar date") from error
    return path


def _note_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise DIntegrationMetadataError(f"{field} must be a non-empty UTF-8 string")
    if "\x00" in value:
        raise DIntegrationMetadataError(f"{field} must not contain NUL")
    try:
        payload = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DIntegrationMetadataError(f"{field} must be UTF-8 encodable") from error
    if len(payload) > MAX_NOTE_BYTES:
        raise DIntegrationMetadataError(f"{field} must not exceed {MAX_NOTE_BYTES} UTF-8 bytes")
    return payload


def _stored_note_bytes(value: object, field: str) -> bytes:
    """Revalidate a frozen bytestring without accepting bytes from JSON input."""

    if not isinstance(value, bytes) or not value:
        raise DIntegrationMetadataError(f"{field} must retain non-empty UTF-8 bytes")
    if b"\x00" in value:
        raise DIntegrationMetadataError(f"{field} must not contain NUL")
    if len(value) > MAX_NOTE_BYTES:
        raise DIntegrationMetadataError(f"{field} must not exceed {MAX_NOTE_BYTES} UTF-8 bytes")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DIntegrationMetadataError(f"{field} must retain UTF-8 bytes") from error
    return value


def _paired_expected_digest(english: object, chinese: object) -> tuple[str | None, str | None]:
    """Validate the all-or-none current-byte identities for an existing note pair."""

    if english is None and chinese is None:
        return None, None
    if not isinstance(english, str) or not isinstance(chinese, str):
        raise DIntegrationMetadataError(
            "Agent Note update requires both expected current SHA-256 digests"
        )
    for value, field in (
        (english, "note_expected_english_sha256"),
        (chinese, "note_expected_chinese_sha256"),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise DIntegrationMetadataError(f"{field} must be a lowercase SHA-256 digest")
    return english, chinese


def _clean_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DIntegrationMetadataError(f"{label} must be a non-empty path without outer whitespace")
    if "\\" in value or any(
        character in "*?[]" or ord(character) < 32 or 127 <= ord(character) <= 159
        for character in value
    ):
        raise DIntegrationMetadataError(f"{label} is invalid")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or str(parsed) != value or ".." in parsed.parts:
        raise DIntegrationMetadataError(f"{label} is invalid")
    return value
