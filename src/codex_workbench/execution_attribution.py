"""Pure, lossless execution-attribution records.

This contract intentionally has no executor, scheduler, store, or network
dependency.  The coordinator can persist its ``to_dict`` result in the
existing event/result envelope, while analysis can read it back without
guessing facts that were never observed.

In particular, a requested model is a routing input, not an observation of the
model that ran.  Only a direct provider/native-CLI receipt with a bounded
reference may produce an ``attested`` observed identity.  Unknown timestamps,
call identifiers, and observed models remain explicitly unknown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Literal


EXECUTION_ATTRIBUTION_SCHEMA_VERSION = 1

FailureOrigin = Literal[
    "environment",
    "auth",
    "quota",
    "transport",
    "model",
    "verification",
    "scope",
    "cancel",
    "unknown",
]
ObservedIdentityStatus = Literal["attested", "unattested", "unknown"]
CandidateDisposition = Literal["selected", "rejected"]
ConditionStatus = Literal["observed", "unknown"]
ReferenceKind = Literal[
    "artifact",
    "snapshot",
    "event",
    "receipt",
    "quota_snapshot",
    "readiness_report",
    "worktree",
    "dependency_report",
    "scope_contract",
]
ProvenanceSource = Literal[
    "task_contract",
    "routing_decision",
    "retry_policy",
    "event_ledger",
    "native_cli_response",
    "provider_receipt",
    "executor_result",
    "worker_result",
    "legacy_result",
    "unknown",
]
ExecutionConditionKind = Literal[
    "dependency",
    "scope",
    "provider_quota",
    "environment_readiness",
    "cpu_wait",
    "memory_wait",
    "io_wait",
]

FAILURE_ORIGINS = frozenset(
    {
        "environment",
        "auth",
        "quota",
        "transport",
        "model",
        "verification",
        "scope",
        "cancel",
        "unknown",
    }
)
REFERENCE_KINDS = frozenset(
    {
        "artifact",
        "snapshot",
        "event",
        "receipt",
        "quota_snapshot",
        "readiness_report",
        "worktree",
        "dependency_report",
        "scope_contract",
    }
)
PROVENANCE_SOURCES = frozenset(
    {
        "task_contract",
        "routing_decision",
        "retry_policy",
        "event_ledger",
        "native_cli_response",
        "provider_receipt",
        "executor_result",
        "worker_result",
        "legacy_result",
        "unknown",
    }
)
# A result copied from a requested model or a worker's self-report remains
# useful audit context, but cannot be used as a quality-model observation.
ATTESTED_IDENTITY_SOURCES = frozenset({"native_cli_response", "provider_receipt"})
EXECUTION_CONDITION_KINDS = frozenset(
    {
        "dependency",
        "scope",
        "provider_quota",
        "environment_readiness",
        "cpu_wait",
        "memory_wait",
        "io_wait",
    }
)
RESOURCE_WAIT_KINDS = frozenset({"cpu_wait", "memory_wait", "io_wait"})

MAX_REFERENCES = 32
MAX_PROVENANCE_REFERENCES = 8
MAX_CANDIDATE_DECISIONS = 64
MAX_CANDIDATE_REASONS = 16
MAX_IDENTIFIER_LENGTH = 512
MAX_TEXT_LENGTH = 512
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,511}$")


def _required_text(value: object, field_name: str, *, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds its {maximum}-character bound")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} must not contain line breaks")
    return value


def _optional_text(value: object, field_name: str, *, maximum: int = MAX_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, maximum=maximum)


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _REFERENCE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _optional_timestamp(value: object, field_name: str) -> str | None:
    timestamp = _optional_text(value, field_name)
    if timestamp is None:
        return None
    try:
        # Validate the supplied timestamp but retain it byte-for-byte.  The
        # contract must not synthesize a replacement "now" value or alter its
        # original offset representation.
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp or null") from error
    return timestamp


def _nonnegative_int(value: object, field_name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        comparator = f">= {minimum}"
        raise ValueError(f"{field_name} must be an integer {comparator}")
    return value


def _sequence(value: object, field_name: str, *, maximum: int) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    values = tuple(value)
    if len(values) > maximum:
        raise ValueError(f"{field_name} exceeds its {maximum}-item bound")
    return values


def _typed_references(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> tuple["AttributionReference", ...]:
    values = _sequence(value, field_name, maximum=maximum)
    if not all(isinstance(item, AttributionReference) for item in values):
        raise ValueError(f"{field_name} must contain AttributionReference values")
    return tuple(values)  # type: ignore[return-value]


def _expect_object(raw: object, name: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be an object")
    present = frozenset(str(key) for key in raw)
    if present != fields:
        missing = sorted(fields - present)
        unexpected = sorted(present - fields)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise ValueError(f"{name} has invalid fields ({'; '.join(details)})")
    return raw


def _read_sequence(raw: object, name: str, *, maximum: int) -> tuple[object, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError(f"{name} must be an array")
    values = tuple(raw)
    if len(values) > maximum:
        raise ValueError(f"{name} exceeds its {maximum}-item bound")
    return values


@dataclass(frozen=True)
class ExecutionStateReference:
    """A bounded pointer to the existing task/node attempt state.

    It is deliberately a pointer, rather than an embedded task snapshot or
    event transcript.  ``event_cursor`` is optional because a caller may know
    the claim before an event has been durably appended.
    """

    task_id: str
    node_id: str
    attempt: int
    event_cursor: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.task_id, "state.task_id", maximum=MAX_IDENTIFIER_LENGTH)
        _required_text(self.node_id, "state.node_id", maximum=MAX_IDENTIFIER_LENGTH)
        _nonnegative_int(self.attempt, "state.attempt", minimum=1)
        if self.event_cursor is not None:
            _nonnegative_int(self.event_cursor, "state.event_cursor")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "event_cursor": self.event_cursor,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ExecutionStateReference":
        value = _expect_object(
            raw,
            "state",
            frozenset({"task_id", "node_id", "attempt", "event_cursor"}),
        )
        return cls(
            task_id=value["task_id"],
            node_id=value["node_id"],
            attempt=value["attempt"],
            event_cursor=value["event_cursor"],
        )


@dataclass(frozen=True)
class AttributionReference:
    """A bounded identifier for an existing artifact, snapshot, or state item.

    ``ref`` is identifier-shaped and capped; it cannot contain transcript
    content.  The referenced object remains in the authoritative artifact or
    state store rather than being copied into an attribution record.
    """

    kind: ReferenceKind
    ref: str
    cursor: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in REFERENCE_KINDS:
            raise ValueError(f"reference.kind is unsupported: {self.kind!r}")
        if not isinstance(self.ref, str) or _REFERENCE_ID.fullmatch(self.ref) is None:
            raise ValueError("reference.ref must be a bounded identifier, not embedded content")
        if self.cursor is not None:
            _nonnegative_int(self.cursor, "reference.cursor")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "ref": self.ref, "cursor": self.cursor}

    @classmethod
    def from_dict(cls, raw: object) -> "AttributionReference":
        value = _expect_object(raw, "reference", frozenset({"kind", "ref", "cursor"}))
        return cls(kind=value["kind"], ref=value["ref"], cursor=value["cursor"])


@dataclass(frozen=True)
class IdentityProvenance:
    """How an identity value entered the record, without embedding its source."""

    source: ProvenanceSource
    references: tuple[AttributionReference, ...] = ()
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if self.source not in PROVENANCE_SOURCES:
            raise ValueError(f"identity provenance source is unsupported: {self.source!r}")
        object.__setattr__(
            self,
            "references",
            _typed_references(
                self.references,
                "identity provenance references",
                maximum=MAX_PROVENANCE_REFERENCES,
            ),
        )
        object.__setattr__(self, "observed_at", _optional_timestamp(self.observed_at, "observed_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "references": [reference.to_dict() for reference in self.references],
            "observed_at": self.observed_at,
        }

    @classmethod
    def unknown(cls) -> "IdentityProvenance":
        return cls(source="unknown")

    @classmethod
    def from_dict(cls, raw: object) -> "IdentityProvenance":
        value = _expect_object(
            raw,
            "identity provenance",
            frozenset({"source", "references", "observed_at"}),
        )
        references = tuple(
            AttributionReference.from_dict(item)
            for item in _read_sequence(
                value["references"],
                "identity provenance references",
                maximum=MAX_PROVENANCE_REFERENCES,
            )
        )
        return cls(
            source=value["source"],
            references=references,
            observed_at=value["observed_at"],
        )


@dataclass(frozen=True)
class RequestedModelIdentity:
    """A model requested by a contract/routing decision, never an observation."""

    provider: str | None
    model_id: str | None
    provenance: IdentityProvenance = field(default_factory=IdentityProvenance.unknown)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _optional_text(self.provider, "requested_model.provider"))
        object.__setattr__(self, "model_id", _optional_text(self.model_id, "requested_model.model_id"))
        if not isinstance(self.provenance, IdentityProvenance):
            raise ValueError("requested_model.provenance must be IdentityProvenance")

    @property
    def status(self) -> Literal["requested", "unknown"]:
        return "requested" if self.model_id is not None else "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model_id": self.model_id,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "RequestedModelIdentity":
        value = _expect_object(
            raw,
            "requested_model",
            frozenset({"status", "provider", "model_id", "provenance"}),
        )
        requested = cls(
            provider=value["provider"],
            model_id=value["model_id"],
            provenance=IdentityProvenance.from_dict(value["provenance"]),
        )
        if value["status"] != requested.status:
            raise ValueError("requested_model.status does not match its supplied model_id")
        return requested


@dataclass(frozen=True)
class ObservedModelIdentity:
    """The independently observed model identity, or an explicit non-observation.

    ``unattested`` retains a claimed value for auditability but must not enter
    attested-quality buckets.  It is intentionally impossible for this type to
    copy a requested model automatically.
    """

    status: ObservedIdentityStatus
    provider: str | None = None
    model_id: str | None = None
    provenance: IdentityProvenance = field(default_factory=IdentityProvenance.unknown)

    def __post_init__(self) -> None:
        if self.status not in {"attested", "unattested", "unknown"}:
            raise ValueError(f"observed_model.status is unsupported: {self.status!r}")
        object.__setattr__(self, "provider", _optional_text(self.provider, "observed_model.provider"))
        object.__setattr__(self, "model_id", _optional_text(self.model_id, "observed_model.model_id"))
        if not isinstance(self.provenance, IdentityProvenance):
            raise ValueError("observed_model.provenance must be IdentityProvenance")
        if self.status == "unknown":
            if self.provider is not None or self.model_id is not None:
                raise ValueError("unknown observed_model must not claim a provider or model_id")
        elif self.model_id is None:
            raise ValueError("non-unknown observed_model requires model_id")
        if self.status == "attested":
            if self.provenance.source not in ATTESTED_IDENTITY_SOURCES:
                raise ValueError("attested observed_model requires direct receipt/native-CLI provenance")
            if not self.provenance.references:
                raise ValueError("attested observed_model requires a bounded provenance reference")

    @classmethod
    def unknown(
        cls,
        *,
        provenance: IdentityProvenance | None = None,
    ) -> "ObservedModelIdentity":
        return cls(status="unknown", provenance=provenance or IdentityProvenance.unknown())

    @classmethod
    def attested(
        cls,
        *,
        provider: str | None,
        model_id: str,
        provenance: IdentityProvenance,
    ) -> "ObservedModelIdentity":
        return cls(
            status="attested",
            provider=provider,
            model_id=model_id,
            provenance=provenance,
        )

    @classmethod
    def unattested(
        cls,
        *,
        provider: str | None,
        model_id: str,
        provenance: IdentityProvenance | None = None,
    ) -> "ObservedModelIdentity":
        return cls(
            status="unattested",
            provider=provider,
            model_id=model_id,
            provenance=provenance or IdentityProvenance.unknown(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model_id": self.model_id,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ObservedModelIdentity":
        value = _expect_object(
            raw,
            "observed_model",
            frozenset({"status", "provider", "model_id", "provenance"}),
        )
        return cls(
            status=value["status"],
            provider=value["provider"],
            model_id=value["model_id"],
            provenance=IdentityProvenance.from_dict(value["provenance"]),
        )


def _physical_call_usage_key(provider: str, call_id: str) -> str:
    material = json.dumps(
        {"provider": provider, "call_id": call_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "physical-call-v1:" + sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PhysicalCallIdentity:
    """An observed provider call ID for safe fallback/retry usage de-duplication.

    The derived key intentionally exists only for an attested provider call.
    An unknown or unattested identity is never grouped with another attempt;
    that is safer than silently collapsing two possibly separate paid calls.
    """

    status: ObservedIdentityStatus
    provider: str | None = None
    call_id: str | None = None
    provenance: IdentityProvenance = field(default_factory=IdentityProvenance.unknown)

    def __post_init__(self) -> None:
        if self.status not in {"attested", "unattested", "unknown"}:
            raise ValueError(f"physical_call.status is unsupported: {self.status!r}")
        object.__setattr__(self, "provider", _optional_text(self.provider, "physical_call.provider"))
        object.__setattr__(self, "call_id", _optional_identifier(self.call_id, "physical_call.call_id"))
        if not isinstance(self.provenance, IdentityProvenance):
            raise ValueError("physical_call.provenance must be IdentityProvenance")
        if self.status == "unknown" and self.call_id is not None:
            raise ValueError("unknown physical_call must not claim call_id")
        if self.status == "unattested" and self.call_id is None:
            raise ValueError("unattested physical_call requires call_id")
        if self.status == "attested":
            if self.provider is None or self.call_id is None:
                raise ValueError("attested physical_call requires provider and call_id")
            if self.provenance.source not in ATTESTED_IDENTITY_SOURCES:
                raise ValueError("attested physical_call requires direct receipt/native-CLI provenance")
            if not self.provenance.references:
                raise ValueError("attested physical_call requires a bounded provenance reference")

    @classmethod
    def unknown(
        cls,
        *,
        provider: str | None = None,
        provenance: IdentityProvenance | None = None,
    ) -> "PhysicalCallIdentity":
        return cls(
            status="unknown",
            provider=provider,
            provenance=provenance or IdentityProvenance.unknown(),
        )

    @property
    def usage_deduplication_key(self) -> str | None:
        if self.status != "attested" or self.provider is None or self.call_id is None:
            return None
        return _physical_call_usage_key(self.provider, self.call_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "call_id": self.call_id,
            "provenance": self.provenance.to_dict(),
            "usage_deduplication_key": self.usage_deduplication_key,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PhysicalCallIdentity":
        value = _expect_object(
            raw,
            "physical_call",
            frozenset(
                {
                    "status",
                    "provider",
                    "call_id",
                    "provenance",
                    "usage_deduplication_key",
                }
            ),
        )
        identity = cls(
            status=value["status"],
            provider=value["provider"],
            call_id=value["call_id"],
            provenance=IdentityProvenance.from_dict(value["provenance"]),
        )
        if value["usage_deduplication_key"] != identity.usage_deduplication_key:
            raise ValueError("physical_call.usage_deduplication_key is not bound to provider/call_id")
        return identity


def physical_call_usage_key(value: PhysicalCallIdentity) -> str | None:
    """Return the safe cross-attempt usage key, or ``None`` when unverified."""

    if not isinstance(value, PhysicalCallIdentity):
        raise ValueError("physical_call_usage_key requires PhysicalCallIdentity")
    return value.usage_deduplication_key


@dataclass(frozen=True)
class CandidateDecision:
    """One selected or rejected routing candidate and its explicit reasons.

    This type has no score, propensity, price, or quota fields.  Those values
    must only be added by their own observed/persisted contracts, never guessed
    during attribution.
    """

    candidate_id: str
    disposition: CandidateDisposition
    reasons: tuple[str, ...]
    provider: str | None = None
    model_id: str | None = None
    references: tuple[AttributionReference, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.candidate_id, "candidate.candidate_id", maximum=MAX_IDENTIFIER_LENGTH)
        if self.disposition not in {"selected", "rejected"}:
            raise ValueError(f"candidate.disposition is unsupported: {self.disposition!r}")
        reasons = _sequence(self.reasons, "candidate.reasons", maximum=MAX_CANDIDATE_REASONS)
        if not reasons:
            raise ValueError("candidate.reasons must explain a selected or rejected candidate")
        normalized_reasons = tuple(
            _required_text(reason, "candidate.reason") for reason in reasons
        )
        if len(set(normalized_reasons)) != len(normalized_reasons):
            raise ValueError("candidate.reasons must not repeat the same reason")
        object.__setattr__(self, "reasons", normalized_reasons)
        object.__setattr__(self, "provider", _optional_text(self.provider, "candidate.provider"))
        object.__setattr__(self, "model_id", _optional_text(self.model_id, "candidate.model_id"))
        object.__setattr__(
            self,
            "references",
            _typed_references(
                self.references,
                "candidate.references",
                maximum=MAX_PROVENANCE_REFERENCES,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "provider": self.provider,
            "model_id": self.model_id,
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "CandidateDecision":
        value = _expect_object(
            raw,
            "candidate",
            frozenset(
                {
                    "candidate_id",
                    "disposition",
                    "reasons",
                    "provider",
                    "model_id",
                    "references",
                }
            ),
        )
        return cls(
            candidate_id=value["candidate_id"],
            disposition=value["disposition"],
            reasons=_read_sequence(
                value["reasons"], "candidate.reasons", maximum=MAX_CANDIDATE_REASONS
            ),
            provider=value["provider"],
            model_id=value["model_id"],
            references=tuple(
                AttributionReference.from_dict(item)
                for item in _read_sequence(
                    value["references"],
                    "candidate.references",
                    maximum=MAX_PROVENANCE_REFERENCES,
                )
            ),
        )


@dataclass(frozen=True)
class FailureAttribution:
    """An explicit, non-inferred failure-origin classification."""

    origin: FailureOrigin = "unknown"
    detail: str | None = None
    references: tuple[AttributionReference, ...] = ()

    def __post_init__(self) -> None:
        if self.origin not in FAILURE_ORIGINS:
            raise ValueError(f"failure.origin is unsupported: {self.origin!r}")
        object.__setattr__(self, "detail", _optional_text(self.detail, "failure.detail"))
        object.__setattr__(
            self,
            "references",
            _typed_references(
                self.references,
                "failure.references",
                maximum=MAX_PROVENANCE_REFERENCES,
            ),
        )

    @classmethod
    def unknown(cls) -> "FailureAttribution":
        return cls(origin="unknown")

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "detail": self.detail,
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "FailureAttribution":
        value = _expect_object(
            raw,
            "failure",
            frozenset({"origin", "detail", "references"}),
        )
        return cls(
            origin=value["origin"],
            detail=value["detail"],
            references=tuple(
                AttributionReference.from_dict(item)
                for item in _read_sequence(
                    value["references"],
                    "failure.references",
                    maximum=MAX_PROVENANCE_REFERENCES,
                )
            ),
        )


@dataclass(frozen=True)
class ExecutionCondition:
    """A separately sourced operational condition or local resource wait.

    Conditions make dependency, scope, provider-quota, and environment
    readiness evidence distinct from model quality.  CPU, memory, and I/O wait
    values can only be marked observed when an actual duration is supplied;
    missing measurements remain ``unknown`` rather than being estimated.
    """

    kind: ExecutionConditionKind
    status: ConditionStatus = "unknown"
    duration_ms: int | None = None
    detail: str | None = None
    references: tuple[AttributionReference, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in EXECUTION_CONDITION_KINDS:
            raise ValueError(f"execution condition kind is unsupported: {self.kind!r}")
        if self.status not in {"observed", "unknown"}:
            raise ValueError(f"execution condition status is unsupported: {self.status!r}")
        if self.kind not in RESOURCE_WAIT_KINDS and self.duration_ms is not None:
            raise ValueError("only CPU, memory, and I/O conditions may carry duration_ms")
        if self.duration_ms is not None:
            _nonnegative_int(self.duration_ms, "execution condition duration_ms")
        object.__setattr__(self, "detail", _optional_text(self.detail, "execution condition detail"))
        object.__setattr__(
            self,
            "references",
            _typed_references(
                self.references,
                "execution condition references",
                maximum=MAX_PROVENANCE_REFERENCES,
            ),
        )
        if self.status == "observed":
            if not self.references:
                raise ValueError("observed execution condition requires a bounded evidence reference")
            if self.kind in RESOURCE_WAIT_KINDS and self.duration_ms is None:
                raise ValueError("observed CPU, memory, and I/O wait requires a measured duration")
        elif self.duration_ms is not None:
            raise ValueError("unknown execution condition must not claim a measured duration")

    @classmethod
    def unknown(
        cls,
        kind: ExecutionConditionKind,
        *,
        detail: str | None = None,
        references: tuple[AttributionReference, ...] = (),
    ) -> "ExecutionCondition":
        return cls(kind=kind, status="unknown", detail=detail, references=references)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ExecutionCondition":
        value = _expect_object(
            raw,
            "execution condition",
            frozenset({"kind", "status", "duration_ms", "detail", "references"}),
        )
        return cls(
            kind=value["kind"],
            status=value["status"],
            duration_ms=value["duration_ms"],
            detail=value["detail"],
            references=tuple(
                AttributionReference.from_dict(item)
                for item in _read_sequence(
                    value["references"],
                    "execution condition references",
                    maximum=MAX_PROVENANCE_REFERENCES,
                )
            ),
        )


@dataclass(frozen=True)
class ExecutionConditions:
    """Every P0 operational category, defaulting losslessly to unknown."""

    dependency: ExecutionCondition = field(
        default_factory=lambda: ExecutionCondition.unknown("dependency")
    )
    scope: ExecutionCondition = field(default_factory=lambda: ExecutionCondition.unknown("scope"))
    provider_quota: ExecutionCondition = field(
        default_factory=lambda: ExecutionCondition.unknown("provider_quota")
    )
    environment_readiness: ExecutionCondition = field(
        default_factory=lambda: ExecutionCondition.unknown("environment_readiness")
    )
    cpu_wait: ExecutionCondition = field(default_factory=lambda: ExecutionCondition.unknown("cpu_wait"))
    memory_wait: ExecutionCondition = field(
        default_factory=lambda: ExecutionCondition.unknown("memory_wait")
    )
    io_wait: ExecutionCondition = field(default_factory=lambda: ExecutionCondition.unknown("io_wait"))

    def __post_init__(self) -> None:
        for field_name, kind in (
            ("dependency", "dependency"),
            ("scope", "scope"),
            ("provider_quota", "provider_quota"),
            ("environment_readiness", "environment_readiness"),
            ("cpu_wait", "cpu_wait"),
            ("memory_wait", "memory_wait"),
            ("io_wait", "io_wait"),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, ExecutionCondition) or value.kind != kind:
                raise ValueError(f"conditions.{field_name} must be an ExecutionCondition for {kind}")

    def to_dict(self) -> dict[str, object]:
        return {
            "dependency": self.dependency.to_dict(),
            "scope": self.scope.to_dict(),
            "provider_quota": self.provider_quota.to_dict(),
            "environment_readiness": self.environment_readiness.to_dict(),
            "cpu_wait": self.cpu_wait.to_dict(),
            "memory_wait": self.memory_wait.to_dict(),
            "io_wait": self.io_wait.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ExecutionConditions":
        value = _expect_object(
            raw,
            "conditions",
            frozenset(
                {
                    "dependency",
                    "scope",
                    "provider_quota",
                    "environment_readiness",
                    "cpu_wait",
                    "memory_wait",
                    "io_wait",
                }
            ),
        )
        return cls(
            dependency=ExecutionCondition.from_dict(value["dependency"]),
            scope=ExecutionCondition.from_dict(value["scope"]),
            provider_quota=ExecutionCondition.from_dict(value["provider_quota"]),
            environment_readiness=ExecutionCondition.from_dict(value["environment_readiness"]),
            cpu_wait=ExecutionCondition.from_dict(value["cpu_wait"]),
            memory_wait=ExecutionCondition.from_dict(value["memory_wait"]),
            io_wait=ExecutionCondition.from_dict(value["io_wait"]),
        )


@dataclass(frozen=True)
class TimingBoundary:
    """A supplied time boundary, or the explicit absence of one."""

    at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _optional_timestamp(self.at, "timing boundary"))

    @property
    def status(self) -> Literal["observed", "unknown"]:
        return "observed" if self.at is not None else "unknown"

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "at": self.at}

    @classmethod
    def from_dict(cls, raw: object) -> "TimingBoundary":
        value = _expect_object(raw, "timing boundary", frozenset({"status", "at"}))
        boundary = cls(at=value["at"])
        if value["status"] != boundary.status:
            raise ValueError("timing boundary status does not match its supplied timestamp")
        return boundary


@dataclass(frozen=True)
class PhaseTiming:
    """Timing for a phase without fabricating missing phase boundaries."""

    started: TimingBoundary = field(default_factory=TimingBoundary)
    finished: TimingBoundary = field(default_factory=TimingBoundary)
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.started, TimingBoundary) or not isinstance(self.finished, TimingBoundary):
            raise ValueError("phase timing boundaries must be TimingBoundary values")
        if self.duration_ms is not None:
            _nonnegative_int(self.duration_ms, "phase.duration_ms")

    @property
    def status(self) -> Literal["complete", "partial", "unknown"]:
        if self.started.at is not None and self.finished.at is not None:
            return "complete"
        if self.started.at is not None or self.finished.at is not None:
            return "partial"
        return "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "started": self.started.to_dict(),
            "finished": self.finished.to_dict(),
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PhaseTiming":
        value = _expect_object(
            raw,
            "phase timing",
            frozenset({"status", "started", "finished", "duration_ms"}),
        )
        timing = cls(
            started=TimingBoundary.from_dict(value["started"]),
            finished=TimingBoundary.from_dict(value["finished"]),
            duration_ms=value["duration_ms"],
        )
        if value["status"] != timing.status:
            raise ValueError("phase timing status does not match its boundaries")
        return timing


@dataclass(frozen=True)
class ExecutionTimings:
    """Queue, prepare, execute, and verify timings with no inferred values."""

    queue: PhaseTiming = field(default_factory=PhaseTiming)
    prepare: PhaseTiming = field(default_factory=PhaseTiming)
    execute: PhaseTiming = field(default_factory=PhaseTiming)
    verify: PhaseTiming = field(default_factory=PhaseTiming)

    def __post_init__(self) -> None:
        for name in ("queue", "prepare", "execute", "verify"):
            if not isinstance(getattr(self, name), PhaseTiming):
                raise ValueError(f"timings.{name} must be PhaseTiming")

    def to_dict(self) -> dict[str, object]:
        return {
            "queue": self.queue.to_dict(),
            "prepare": self.prepare.to_dict(),
            "execute": self.execute.to_dict(),
            "verify": self.verify.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ExecutionTimings":
        value = _expect_object(
            raw,
            "timings",
            frozenset({"queue", "prepare", "execute", "verify"}),
        )
        return cls(
            queue=PhaseTiming.from_dict(value["queue"]),
            prepare=PhaseTiming.from_dict(value["prepare"]),
            execute=PhaseTiming.from_dict(value["execute"]),
            verify=PhaseTiming.from_dict(value["verify"]),
        )


@dataclass(frozen=True)
class ExecutionAttribution:
    """The serializable P0-B contract for one task/node attempt.

    ``candidate_decisions`` keeps both selected and rejected choices.  A retry
    gets its own ``ExecutionStateReference`` and may share a physical-call key
    only when the same provider call was independently attested.
    """

    state: ExecutionStateReference
    requested_model: RequestedModelIdentity
    observed_model: ObservedModelIdentity = field(default_factory=ObservedModelIdentity.unknown)
    physical_call: PhysicalCallIdentity = field(default_factory=PhysicalCallIdentity.unknown)
    candidate_decisions: tuple[CandidateDecision, ...] = ()
    failure: FailureAttribution = field(default_factory=FailureAttribution.unknown)
    conditions: ExecutionConditions = field(default_factory=ExecutionConditions)
    timings: ExecutionTimings = field(default_factory=ExecutionTimings)
    references: tuple[AttributionReference, ...] = ()
    schema_version: int = EXECUTION_ATTRIBUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_ATTRIBUTION_SCHEMA_VERSION:
            raise ValueError("unsupported execution attribution schema version")
        if not isinstance(self.state, ExecutionStateReference):
            raise ValueError("attribution.state must be ExecutionStateReference")
        if not isinstance(self.requested_model, RequestedModelIdentity):
            raise ValueError("attribution.requested_model must be RequestedModelIdentity")
        if not isinstance(self.observed_model, ObservedModelIdentity):
            raise ValueError("attribution.observed_model must be ObservedModelIdentity")
        if not isinstance(self.physical_call, PhysicalCallIdentity):
            raise ValueError("attribution.physical_call must be PhysicalCallIdentity")
        if not isinstance(self.failure, FailureAttribution):
            raise ValueError("attribution.failure must be FailureAttribution")
        if not isinstance(self.conditions, ExecutionConditions):
            raise ValueError("attribution.conditions must be ExecutionConditions")
        if not isinstance(self.timings, ExecutionTimings):
            raise ValueError("attribution.timings must be ExecutionTimings")
        decisions = _sequence(
            self.candidate_decisions,
            "attribution.candidate_decisions",
            maximum=MAX_CANDIDATE_DECISIONS,
        )
        if not all(isinstance(item, CandidateDecision) for item in decisions):
            raise ValueError("attribution.candidate_decisions must contain CandidateDecision values")
        object.__setattr__(self, "candidate_decisions", tuple(decisions))
        object.__setattr__(
            self,
            "references",
            _typed_references(
                self.references,
                "attribution.references",
                maximum=MAX_REFERENCES,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.to_dict(),
            "requested_model": self.requested_model.to_dict(),
            "observed_model": self.observed_model.to_dict(),
            "physical_call": self.physical_call.to_dict(),
            "candidate_decisions": [decision.to_dict() for decision in self.candidate_decisions],
            "failure": self.failure.to_dict(),
            "conditions": self.conditions.to_dict(),
            "timings": self.timings.to_dict(),
            "references": [reference.to_dict() for reference in self.references],
        }

    def to_json(self) -> str:
        """Return canonical JSON suitable for the existing result/event envelopes."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: object) -> "ExecutionAttribution":
        value = _expect_object(
            raw,
            "execution attribution",
            frozenset(
                {
                    "schema_version",
                    "state",
                    "requested_model",
                    "observed_model",
                    "physical_call",
                    "candidate_decisions",
                    "failure",
                    "conditions",
                    "timings",
                    "references",
                }
            ),
        )
        return cls(
            schema_version=value["schema_version"],
            state=ExecutionStateReference.from_dict(value["state"]),
            requested_model=RequestedModelIdentity.from_dict(value["requested_model"]),
            observed_model=ObservedModelIdentity.from_dict(value["observed_model"]),
            physical_call=PhysicalCallIdentity.from_dict(value["physical_call"]),
            candidate_decisions=tuple(
                CandidateDecision.from_dict(item)
                for item in _read_sequence(
                    value["candidate_decisions"],
                    "attribution.candidate_decisions",
                    maximum=MAX_CANDIDATE_DECISIONS,
                )
            ),
            failure=FailureAttribution.from_dict(value["failure"]),
            conditions=ExecutionConditions.from_dict(value["conditions"]),
            timings=ExecutionTimings.from_dict(value["timings"]),
            references=tuple(
                AttributionReference.from_dict(item)
                for item in _read_sequence(
                    value["references"],
                    "attribution.references",
                    maximum=MAX_REFERENCES,
                )
            ),
        )

    @classmethod
    def from_json(cls, text: str) -> "ExecutionAttribution":
        try:
            raw = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("execution attribution must be valid JSON") from error
        return cls.from_dict(raw)
