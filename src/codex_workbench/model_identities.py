"""Receipt-derived identity bindings for Claude CLI model aliases.

Claude's local CLI exposes selection aliases (``opus``, ``sonnet``, and
``fable``), while a terminal receipt may attest the exact model identifier
selected by the provider.  This module joins those two observations without
guessing from model names or mutating the authoritative capability catalog.

The functions are deliberately pure: they inspect supplied event/task
snapshots and return ordinary dictionaries.  They never probe a CLI, contact
the network, or persist a catalog generation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from typing import Any


CLAUDE_ALIAS_SELECTORS = ("opus", "sonnet", "fable")
_CLAUDE_ALIAS_SET = frozenset(CLAUDE_ALIAS_SELECTORS)
_NATIVE_TERMINAL_EVENTS = frozenset(
    {"node.accepted", "node.failed", "node.blocked", "node.indeterminate"}
)
_IDENTITY_LIFETIME = timedelta(days=7)
_ACTUAL_MODEL = re.compile(r"^claude-(opus|sonnet|fable)-.+$")


@dataclass(frozen=True)
class _Observation:
    selection_id: str
    canonical_model_id: str
    agent_cli_version: str
    observed_at: datetime
    valid_until: datetime
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "selection_id": self.selection_id,
            "canonical_model_id": self.canonical_model_id,
            "agent_cli_version": self.agent_cli_version,
            "observed_at": _format_timestamp(self.observed_at),
            "valid_until": _format_timestamp(self.valid_until),
            "evidence": deepcopy(self.evidence),
        }


_REASON_ORDER = (
    "catalog_alias_missing",
    "no_valid_observation",
    "timestamp_missing",
    "timestamp_invalid",
    "future_observation",
    "stale_observation",
    "provider_missing",
    "provider_not_claude",
    "executor_not_claude",
    "ambiguous_requested_alias",
    "requested_alias_missing",
    "verifier_path",
    "evidence_reused",
    "agent_version_missing",
    "agent_version_mismatch",
    "exit_code_not_zero",
    "missing_actual_model",
    "invalid_actual_model",
    "family_mismatch",
    "attempt_missing",
    "cursor_missing",
    "cursor_invalid",
    "conflicting_canonical_ids_at_newest_timestamp",
)
_REASON_INDEX = {reason: index for index, reason in enumerate(_REASON_ORDER)}


def derive_model_identities(
    events: Iterable[Mapping[str, Any]],
    tasks: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
    *,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Derive strict, short-lived Claude alias identity bindings.

    A binding requires all of the following evidence from one native terminal
    event: an exact Claude provider receipt, an exact requested alias traced to
    that node/attempt's start or node specification, a matching exact
    ``claude-<family>-...`` model identifier, the current catalog Claude CLI
    version, integer exit code zero, and a non-reused non-verifier path.

    The returned report contains one deterministic binding per alias at most.
    Newer valid observations replace older ones.  If the newest timestamp has
    more than one exact canonical identifier, that alias remains unresolved.
    """

    reference_now = _coerce_now(now)
    current_cli_version = _catalog_claude_cli_version(catalog)
    catalog_aliases = _catalog_aliases(catalog)

    task_index = _task_index(tasks)
    event_rows = [dict(event) for event in events if isinstance(event, Mapping)]
    starts, reused_nodes = _index_start_and_reuse_events(event_rows)
    observations: dict[str, list[_Observation]] = defaultdict(list)
    reasons: dict[str, set[str]] = defaultdict(set)

    for event in event_rows:
        event_type = event.get("event_type")
        if event_type not in _NATIVE_TERMINAL_EVENTS:
            continue

        task_id = _nonempty_text(event.get("task_id"))
        node_id = _nonempty_text(event.get("node_id"))
        if task_id is None or node_id is None:
            # Without a task/node pair there is no safe way to trace an alias;
            # this is an irrelevant event rather than an alias-specific error.
            continue
        task = task_index.get(task_id)
        spec = _node_spec(task, node_id)
        if spec is None:
            continue

        payload = event.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        result = payload_map.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        attempt = _positive_int(payload_map.get("attempt"))
        if attempt is None:
            attempt = _positive_int(event.get("attempt"))
        indexed_start_rows = starts.get((task_id, node_id, attempt), ()) if attempt is not None else ()
        start_rows = tuple(
            start
            for start in indexed_start_rows
            if _start_precedes_terminal(start, event)
        )
        aliases, trace_reasons = _trace_aliases(spec, start_rows)
        if not aliases:
            # A receipt that cannot identify an exact requested alias must not
            # be attributed to a family inferred from actual_model.
            continue

        if len(aliases) != 1:
            for alias in aliases:
                reasons[alias].add("ambiguous_requested_alias")
            continue
        alias = next(iter(aliases))
        if alias not in _CLAUDE_ALIAS_SET:
            continue

        reasons[alias].update(trace_reasons)
        if alias not in catalog_aliases:
            reasons[alias].add("catalog_alias_missing")
        if current_cli_version is None:
            # The current catalog version is part of the trust boundary.  Do
            # not let a receipt bind when that attestation is unavailable.
            reasons[alias].add("agent_version_mismatch")

        if (task_id, node_id) in reused_nodes:
            reasons[alias].add("evidence_reused")

        if spec.get("verifier") is True or _lower(spec.get("executor")) in {
            "fixture",
            "deterministic",
        } or _lower(spec.get("model")) == "fixture":
            reasons[alias].add("verifier_path" if spec.get("verifier") is True else "executor_not_claude")

        provider = result_map.get("provider")
        if provider is None:
            reasons[alias].add("provider_missing")
        elif provider != "claude":
            reasons[alias].add("provider_not_claude")

        if result_map.get("result_kind") == "verifier":
            reasons[alias].add("verifier_path")

        if not _same_attempt_executor_is_claude(spec, start_rows, result_map):
            reasons[alias].add("executor_not_claude")

        actual_model = result_map.get("actual_model")
        actual_family: str | None = None
        if actual_model is None:
            reasons[alias].add("missing_actual_model")
        elif not isinstance(actual_model, str):
            reasons[alias].add("invalid_actual_model")
        else:
            actual_match = _ACTUAL_MODEL.fullmatch(actual_model)
            if actual_match is None:
                # A well-formed Claude ID for another family gets a more
                # useful reason than a generic malformed ID.
                if actual_model.startswith("claude-") and actual_model.count("-") >= 2:
                    reasons[alias].add("family_mismatch")
                else:
                    reasons[alias].add("invalid_actual_model")
            else:
                actual_family = actual_match.group(1)
                if actual_family != alias:
                    reasons[alias].add("family_mismatch")

        agent_version = result_map.get("agent_version")
        if not isinstance(agent_version, str) or not agent_version:
            reasons[alias].add("agent_version_missing")
        elif current_cli_version is None or agent_version != current_cli_version:
            reasons[alias].add("agent_version_mismatch")

        exit_code = result_map.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            reasons[alias].add("exit_code_not_zero")

        observed_at, timestamp_reason = _event_timestamp(event, reference_now)
        if timestamp_reason is not None:
            reasons[alias].add(timestamp_reason)

        cursor = event.get("cursor")
        if cursor is None:
            reasons[alias].add("cursor_missing")
        elif not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            reasons[alias].add("cursor_invalid")

        if (
            current_cli_version is None
            or alias not in catalog_aliases
            or (task_id, node_id) in reused_nodes
            or trace_reasons
            or not _same_attempt_executor_is_claude(spec, start_rows, result_map)
            or provider != "claude"
            or result_map.get("result_kind") == "verifier"
            or spec.get("verifier") is True
            or _lower(spec.get("executor")) in {"fixture", "deterministic"}
            or _lower(spec.get("model")) == "fixture"
            or not isinstance(actual_model, str)
            or actual_family != alias
            or not isinstance(agent_version, str)
            or not agent_version
            or agent_version != current_cli_version
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or exit_code != 0
            or observed_at is None
            or timestamp_reason is not None
            or not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
            or attempt is None
        ):
            continue

        # _ACTUAL_MODEL was checked above; retaining the exact receipt value
        # here is intentional.  In particular, do not strip a date suffix or
        # derive a canonical ID from the alias/model_id.
        assert isinstance(actual_model, str)
        observations[alias].append(
            _Observation(
                selection_id=alias,
                canonical_model_id=actual_model,
                agent_cli_version=current_cli_version,
                observed_at=observed_at,
                valid_until=observed_at + _IDENTITY_LIFETIME,
                evidence={
                    "task_id": task_id,
                    "node_id": node_id,
                    "attempt": attempt,
                    "cursor": cursor,
                },
            )
        )

    bindings: list[dict[str, Any]] = []
    unresolved: dict[str, list[str]] = {}
    for alias in CLAUDE_ALIAS_SELECTORS:
        candidates = observations.get(alias, [])
        if not candidates:
            reasons[alias].add("no_valid_observation")
        if alias not in catalog_aliases:
            reasons[alias].add("catalog_alias_missing")

        if candidates:
            newest_at = max(item.observed_at for item in candidates)
            newest = [item for item in candidates if item.observed_at == newest_at]
            canonical_ids = {item.canonical_model_id for item in newest}
            if len(canonical_ids) > 1:
                reasons[alias].add("conflicting_canonical_ids_at_newest_timestamp")
            elif alias in catalog_aliases:
                # Pick the greatest cursor/evidence tuple when duplicate
                # exact observations share one timestamp, making the source
                # selection deterministic without treating duplicates as a
                # conflict.
                winner = max(
                    newest,
                    key=lambda item: (
                        int(item.evidence["cursor"]),
                        str(item.evidence["task_id"]),
                        str(item.evidence["node_id"]),
                        int(item.evidence["attempt"]),
                    ),
                )
                bindings.append(winner.as_dict())
                continue

        unresolved[alias] = _ordered_reasons(reasons[alias])

    bindings.sort(key=lambda item: CLAUDE_ALIAS_SELECTORS.index(str(item["selection_id"])))
    return {"bindings": bindings, "unresolved": unresolved}


def catalog_with_model_identities(
    catalog: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a copied catalog view with successful alias identities attached.

    Only existing Claude alias records are touched.  The catalog generation
    identity/digest remains unchanged because this is a derived view, not a
    new authoritative catalog generation.
    """

    view = deepcopy(dict(catalog))
    raw_bindings = report.get("bindings") if isinstance(report, Mapping) else None
    if not isinstance(raw_bindings, list):
        return view

    bindings: dict[str, Mapping[str, Any]] = {}
    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            continue
        selection_id = raw.get("selection_id")
        canonical_model_id = raw.get("canonical_model_id")
        evidence = raw.get("evidence")
        if (
            selection_id in _CLAUDE_ALIAS_SET
            and isinstance(canonical_model_id, str)
            and _actual_model_family(canonical_model_id) == selection_id
            and isinstance(evidence, Mapping)
        ):
            bindings[str(selection_id)] = raw

    models = view.get("models")
    if not isinstance(models, list):
        return view
    for record in models:
        if not isinstance(record, dict):
            continue
        if record.get("provider") != "claude":
            continue
        selection_id = record.get("model_id")
        binding = bindings.get(selection_id)
        if binding is None:
            continue
        identity = record.get("identity")
        identity_view = dict(identity) if isinstance(identity, Mapping) else {}
        identity_view["canonical_model_id"] = binding["canonical_model_id"]
        identity_view["evidence"] = deepcopy(dict(binding["evidence"]))
        record["identity"] = identity_view
    return view


def _task_index(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        task_id = _nonempty_text(task.get("task_id"))
        if task_id is not None and task_id not in index:
            index[task_id] = task
    return index


def _node_spec(task: Mapping[str, Any] | None, node_id: str) -> Mapping[str, Any] | None:
    if task is None:
        return None
    nodes = task.get("nodes")
    if not isinstance(nodes, Iterable) or isinstance(nodes, (str, bytes, Mapping)):
        return None
    for node in nodes:
        if isinstance(node, Mapping) and node.get("node_id") == node_id:
            return node
    return None


def _index_start_and_reuse_events(
    events: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, int], tuple[Mapping[str, Any], ...]], set[tuple[str, str]]]:
    starts: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    reused: set[tuple[str, str]] = set()
    for event in events:
        task_id = _nonempty_text(event.get("task_id"))
        node_id = _nonempty_text(event.get("node_id"))
        if task_id is None or node_id is None:
            continue
        event_type = event.get("event_type")
        if event_type == "node.evidence_reused":
            reused.add((task_id, node_id))
        if event_type != "node.started":
            continue
        payload = event.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        attempt = _positive_int(payload_map.get("attempt"))
        if attempt is None:
            attempt = _positive_int(event.get("attempt"))
        if attempt is not None:
            starts[(task_id, node_id, attempt)].append(event)
    return {key: tuple(value) for key, value in starts.items()}, reused


def _trace_aliases(
    spec: Mapping[str, Any],
    starts: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    values: set[str] = set()
    reasons: set[str] = set()
    for start in starts:
        payload = start.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        for field in ("requested_model", "effective_model", "model"):
            value = _start_field(start, payload_map, field)
            if isinstance(value, str) and value in _CLAUDE_ALIAS_SET:
                values.add(value)
    model = spec.get("model")
    if isinstance(model, str) and model in _CLAUDE_ALIAS_SET:
        values.add(model)
    if not values:
        reasons.add("requested_alias_missing")
    return values, reasons


def _same_attempt_executor_is_claude(
    spec: Mapping[str, Any],
    starts: Iterable[Mapping[str, Any]],
    result: Mapping[str, Any],
) -> bool:
    explicit: list[str] = []
    spec_executor = _lower(spec.get("executor"))
    if spec_executor:
        explicit.append(spec_executor)
    for start in starts:
        payload = start.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        for field in ("executor", "effective_executor"):
            value = _lower(_start_field(start, payload_map, field))
            if value:
                explicit.append(value)
    result_executor = _lower(result.get("executor"))
    if result_executor:
        explicit.append(result_executor)
    return bool(explicit) and all(value == "claude" for value in explicit)


def _start_field(
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    field: str,
) -> object:
    value = payload.get(field)
    return event.get(field) if value is None else value


def _start_precedes_terminal(
    start: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> bool:
    start_cursor = start.get("cursor")
    terminal_cursor = terminal.get("cursor")
    if (
        not isinstance(start_cursor, int)
        or isinstance(start_cursor, bool)
        or not isinstance(terminal_cursor, int)
        or isinstance(terminal_cursor, bool)
    ):
        return False
    return start_cursor < terminal_cursor


def _actual_model_family(value: str) -> str | None:
    match = _ACTUAL_MODEL.fullmatch(value)
    return match.group(1) if match is not None else None


def _catalog_aliases(catalog: Mapping[str, Any]) -> set[str]:
    models = catalog.get("models")
    if not isinstance(models, Iterable) or isinstance(models, (str, bytes, Mapping)):
        return set()
    return {
        str(record.get("model_id"))
        for record in models
        if isinstance(record, Mapping)
        and record.get("provider") == "claude"
        and record.get("model_id") in _CLAUDE_ALIAS_SET
    }


def _catalog_claude_cli_version(catalog: Mapping[str, Any]) -> str | None:
    agents = catalog.get("agents")
    if not isinstance(agents, Mapping):
        return None
    claude = agents.get("claude")
    if not isinstance(claude, Mapping):
        return None
    version = claude.get("cli_version")
    return version if isinstance(version, str) and version else None


def _event_timestamp(
    event: Mapping[str, Any],
    reference_now: datetime,
) -> tuple[datetime | None, str | None]:
    raw = event.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None, "timestamp_missing"
    try:
        parsed = _parse_timestamp(raw)
    except (TypeError, ValueError):
        return None, "timestamp_invalid"
    if parsed > reference_now:
        return None, "future_observation"
    if parsed + _IDENTITY_LIFETIME < reference_now:
        return None, "stale_observation"
    return parsed, None


def _coerce_now(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return _ensure_aware(value).astimezone(UTC)
    if isinstance(value, str):
        return _parse_timestamp(value)
    raise TypeError("now must be a datetime, ISO timestamp, or None")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return _ensure_aware(datetime.fromisoformat(normalized)).astimezone(UTC)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(
        timespec="microseconds" if value.microsecond else "seconds"
    )


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return int(value)


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _lower(value: object) -> str:
    return value.lower() if isinstance(value, str) else ""


def _ordered_reasons(reasons: Iterable[str]) -> list[str]:
    return sorted(
        set(reasons),
        key=lambda reason: (_REASON_INDEX.get(reason, len(_REASON_ORDER)), reason),
    )
