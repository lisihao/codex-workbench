"""Pure constructors for versioned Workbench connection evidence."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


CONNECTION_EVIDENCE_VERSION = "workbench-connection-evidence/v1"
AUTHORITY_SERVICE_PROTOCOL = "workbench-authority-service/v1"
_IDENTIFIER_LIMIT = 200
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_LAYER_NAMES = ("host_catalog", "bridge", "adapter", "authority")
_MEASUREMENT_NAMES = (
    "restart_requests",
    "recovery_duration_ms",
    "duplicate_effects",
    "orphan_processes",
    "data_loss",
)


def unknown_connection_evidence() -> dict[str, Any]:
    """Return an independent evidence record with no unobserved claims."""

    return {
        "schema_version": CONNECTION_EVIDENCE_VERSION,
        **{name: {"status": "unknown"} for name in _LAYER_NAMES},
        "measurements": {name: None for name in _MEASUREMENT_NAMES},
    }


def _short_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > _IDENTIFIER_LIMIT:
        return None
    if any(not character.isprintable() for character in value):
        return None
    normalized = value.strip()
    return normalized or None


def _lower_hex_digest(value: object) -> str | None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        return None
    return value


def authority_observation(status: Mapping[str, Any]) -> dict[str, Any]:
    """Record an Authority identity only when its current status is complete."""

    if not isinstance(status, Mapping):
        return {"status": "unknown", "reason": "incomplete_authority_status"}

    instance_id = _short_identifier(status.get("service_instance"))
    version = _short_identifier(status.get("version"))
    protocol = status.get("service_protocol")
    capabilities_sha256 = _lower_hex_digest(status.get("tools_sha256"))
    if (
        instance_id is None
        or version is None
        or protocol != AUTHORITY_SERVICE_PROTOCOL
        or capabilities_sha256 is None
    ):
        return {"status": "unknown", "reason": "incomplete_authority_status"}

    return {
        "status": "observed",
        "instance_id": instance_id,
        "version": version,
        "protocol": AUTHORITY_SERVICE_PROTOCOL,
        "capabilities_sha256": capabilities_sha256,
    }
