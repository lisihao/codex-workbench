"""Deterministic, non-calibrating comparison of public model evidence.

Public benchmark records are not local Workbench outcomes.  This module keeps
them out of Beta/posterior math and only turns exact, comparable same-cohort
records into an explainable secondary preference among already-admitted cold
start candidates.  It intentionally returns a partial order rather than a
pairwise sort comparator: incompatible records, source conflicts, and cycles
all abstain instead of creating an unstable ranking.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
import json
import math
from typing import Any


_COHORT_FIELDS = (
    "benchmark",
    "benchmark_version",
    "metric_kind",
    "harness",
    "reasoning_effort",
    "task_type",
)
_HIGHER_IS_BETTER = frozenset(
    {"acceptance_rate", "accuracy", "pass_rate", "quality", "resolved_rate", "success_rate"}
)
_LOWER_IS_BETTER = frozenset(
    {"cost", "cost_units", "cost_usd", "latency", "latency_ms", "runtime_ms"}
)
_RATE_METRICS = frozenset({"acceptance_rate", "pass_rate", "resolved_rate", "success_rate"})
_RATE_UNITS = frozenset({"percent", "percentage", "proportion", "ratio"})
_LATENCY_UNITS = frozenset({"milliseconds", "ms", "s", "seconds"})
_COST_UNITS = frozenset({"cost-units", "credits", "tokens", "usd"})


def canonical_cohort_key(record: Mapping[str, Any]) -> str | None:
    """Return the contract cohort key only when every condition is explicit."""

    cohort: dict[str, str] = {}
    for field in _COHORT_FIELDS:
        value = _cohort_text(record.get(field))
        if value is None:
            return None
        cohort[field] = value
    return json.dumps(cohort, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rank_comparable_public_evidence(
    candidates: Iterable[Mapping[str, Any]],
    *,
    task_type: str,
) -> dict[str, Any]:
    """Return a deterministic secondary preference plus a full abstention receipt.

    ``candidates`` must already be hard-gated and in the quality-equivalence
    band.  A candidate needs ``candidate_id``, execution ``provider``, exact
    ``model``, ``reasoning_effort``, and optional ``public_evidence`` rows.
    No numeric source value leaves this function; its only ranking output is a
    topological preference tier derived from unanimous pairwise source verdicts.
    """

    normalized_task_type = _text(task_type) or ""
    items = [
        _candidate_state(candidate, task_type=normalized_task_type)
        for candidate in candidates
    ]
    items.sort(key=lambda item: item["candidate_id"])
    summaries = {
        item["candidate_id"]: _empty_candidate_summary(item["candidate_id"])
        for item in items
    }
    records_by_candidate = {
        item["candidate_id"]: item["records"]
        for item in items
    }
    for item in items:
        summaries[item["candidate_id"]]["abstained_sources"].extend(item["abstentions"])

    edges: dict[tuple[str, str], list[dict[str, str]]] = {}
    conflicts: list[dict[str, Any]] = []
    incomplete_comparisons: list[dict[str, Any]] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            left_id = left["candidate_id"]
            right_id = right["candidate_id"]
            shared = sorted(
                set(records_by_candidate[left_id]) & set(records_by_candidate[right_id])
            )
            votes: list[dict[str, str]] = []
            if not shared:
                incomplete_comparisons.append(
                    {
                        "candidates": [left_id, right_id],
                        "sources": [],
                        "reason": (
                            "incomplete-comparison: no shared valid comparable public "
                            "measurement cohort"
                        ),
                    }
                )
                continue
            shared_valid_measurement = False
            for source, cohort_key in shared:
                comparison = _compare_records(
                    records_by_candidate[left_id][(source, cohort_key)],
                    records_by_candidate[right_id][(source, cohort_key)],
                )
                if comparison["direction"] != 0 or comparison["reason"] in {
                    None,
                    "pass-rate uncertainty intervals overlap",
                }:
                    shared_valid_measurement = True
                if comparison["direction"] == 0:
                    if comparison["reason"] is not None:
                        receipt = {
                            "source": source,
                            "cohort_key": cohort_key,
                            "reason": comparison["reason"],
                        }
                        summaries[left_id]["abstained_sources"].append(receipt)
                        summaries[right_id]["abstained_sources"].append(receipt)
                    continue
                votes.append(
                    {
                        "source": source,
                        "cohort_key": cohort_key,
                        "winner": left_id if comparison["direction"] > 0 else right_id,
                        "loser": right_id if comparison["direction"] > 0 else left_id,
                    }
                )
            directions = {(vote["winner"], vote["loser"]) for vote in votes}
            if len(directions) > 1:
                conflict = {
                    "candidates": [left_id, right_id],
                    "sources": _unique_references(votes),
                    "reason": "comparable public sources disagree; preference abstained",
                }
                conflicts.append(conflict)
                summaries[left_id]["conflicting_sources"].extend(conflict["sources"])
                summaries[right_id]["conflicting_sources"].extend(conflict["sources"])
                continue
            if directions:
                winner, loser = next(iter(directions))
                edges[(winner, loser)] = _unique_references(votes)
            if not shared_valid_measurement:
                incomplete_comparisons.append(
                    {
                        "candidates": [left_id, right_id],
                        "sources": _unique_references(
                            {"source": source, "cohort_key": cohort_key}
                            for source, cohort_key in shared
                        ),
                        "reason": (
                            "incomplete-comparison: no shared valid comparable public "
                            "measurement cohort"
                        ),
                    }
                )

    if incomplete_comparisons:
        for candidate_id, summary in summaries.items():
            wins = [
                references
                for (winner, _), references in edges.items()
                if winner == candidate_id
            ]
            summary["supporting_sources"].extend(
                reference for values in wins for reference in values
            )
        for comparison in incomplete_comparisons:
            references = comparison["sources"] or [
                {"source": "unknown", "cohort_key": ""}
            ]
            for candidate_id in comparison["candidates"]:
                summaries[candidate_id]["abstained_sources"].extend(
                    {
                        "source": reference["source"],
                        "cohort_key": reference["cohort_key"],
                        "reason": comparison["reason"],
                    }
                    for reference in references
                )
        reason = (
            "incomplete-comparison: not every in-band candidate pair shares a "
            "valid comparable public measurement cohort; baseline deterministic "
            "ordering retained"
        )
        for summary in summaries.values():
            summary["status"] = "abstained"
            summary["reason"] = reason
            summary["preference_rank"] = 0
            _normalise_summary_receipt(summary)
        return _result(
            items,
            summaries,
            status="abstained",
            reason=reason,
            conflicts=conflicts,
            incomplete_comparisons=incomplete_comparisons,
        )

    if _has_cycle(items, edges):
        cycle_references = _unique_references(
            reference for values in edges.values() for reference in values
        )
        for summary in summaries.values():
            summary["abstained_sources"].extend(
                {
                    "source": reference["source"],
                    "cohort_key": reference["cohort_key"],
                    "reason": "public evidence preference cycle; all public preferences abstained",
                }
                for reference in cycle_references
            )
            summary["reason"] = "public evidence cycle; baseline deterministic ordering retained"
            _normalise_summary_receipt(summary)
        return _result(
            items,
            summaries,
            status="abstained",
            reason="public evidence cycle; baseline deterministic ordering retained",
            conflicts=conflicts,
        )

    preference_rank = _preference_ranks(items, edges)
    for candidate_id, summary in summaries.items():
        wins = [references for (winner, _), references in edges.items() if winner == candidate_id]
        losses = [references for (_, loser), references in edges.items() if loser == candidate_id]
        summary["supporting_sources"].extend(
            reference for values in wins for reference in values
        )
        if wins:
            summary["status"] = "used"
            summary["reason"] = "same-cohort public evidence supports a secondary cold-start preference"
        elif losses:
            summary["status"] = "used"
            summary["reason"] = "same-cohort public evidence supports another cold-start candidate"
        elif summary["conflicting_sources"]:
            summary["reason"] = "conflicting public evidence; baseline deterministic ordering retained"
        elif summary["abstained_sources"]:
            summary["reason"] = "no comparable public evidence; baseline deterministic ordering retained"
        else:
            summary["reason"] = "no public evidence supplied"
        summary["preference_rank"] = preference_rank[candidate_id]
        _normalise_summary_receipt(summary)

    return _result(
        items,
        summaries,
        status="used" if edges else "abstained",
        reason=(
            "same-cohort public evidence applied as a secondary cold-start preference"
            if edges
            else "no unanimous comparable public evidence; baseline deterministic ordering retained"
        ),
        conflicts=conflicts,
    )


def _candidate_state(candidate: Mapping[str, Any], *, task_type: str) -> dict[str, Any]:
    candidate_id = _text(candidate.get("candidate_id"))
    provider = _text(candidate.get("provider"))
    model = _text(candidate.get("model"))
    effort = _text(candidate.get("reasoning_effort"))
    if candidate_id is None or provider is None or model is None:
        raise ValueError("public evidence candidate requires candidate_id, provider, and model")
    records: dict[tuple[str, str], dict[str, Any]] = {}
    abstentions: list[dict[str, str]] = []
    raw_records = candidate.get("public_evidence")
    if raw_records is None:
        raw_records = ()
    if not isinstance(raw_records, Iterable) or isinstance(raw_records, (str, bytes, Mapping)):
        abstentions.append(
            {
                "source": "unknown",
                "cohort_key": "",
                "reason": "public_evidence is not a sequence",
            }
        )
        raw_records = ()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in raw_records:
        parsed, abstention = _parse_record(
            raw,
            provider=provider,
            model=model,
            effort=effort,
            task_type=task_type,
        )
        if abstention is not None:
            abstentions.append(abstention)
        elif parsed is not None:
            grouped[(parsed["source"], parsed["cohort_key"])].append(parsed)
    for key, group in grouped.items():
        selected, abstention = _deduplicate_group(group)
        if selected is None:
            assert abstention is not None
            abstentions.append(abstention)
        else:
            records[key] = selected
    return {
        "candidate_id": candidate_id,
        "records": records,
        "abstentions": abstentions,
    }


def _parse_record(
    raw: Any,
    *,
    provider: str,
    model: str,
    effort: str | None,
    task_type: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if not isinstance(raw, Mapping):
        return None, _abstention(raw, "public evidence record is not an object")
    source = _text(raw.get("source"))
    if effort is None:
        return None, _abstention(raw, "candidate reasoning effort is missing")
    cohort_key = _cohort_text(raw.get("cohort_key"))
    if source is None:
        return None, _abstention(raw, "missing public evidence source")
    if raw.get("comparability") is None or not isinstance(raw.get("comparability"), Mapping):
        return None, _abstention(raw, "missing public evidence comparability")
    comparability = raw["comparability"]
    if _text(comparability.get("status")) != "comparable":
        return None, _abstention(
            raw,
            "public evidence is reference_only or lacks declared comparability",
            source=source,
        )
    expected_cohort = canonical_cohort_key(raw)
    if expected_cohort is None or cohort_key != expected_cohort:
        return None, _abstention(
            raw,
            "public evidence cohort_key is incomplete or does not match its conditions",
            source=source,
        )
    if _text(raw.get("provider")) != provider or _text(raw.get("canonical_model_id")) != model:
        return None, _abstention(
            raw,
            "public evidence does not match the exact execution provider/model",
            source=source,
        )
    if _text(raw.get("reasoning_effort")) != effort or _text(raw.get("task_type")) != task_type:
        return None, _abstention(
            raw,
            "public evidence does not match the exact reasoning effort/task type",
            source=source,
        )
    metric_kind = _text(raw.get("metric_kind"))
    score_kind = _text(raw.get("score_kind"))
    unit = _text(raw.get("unit"))
    if metric_kind is None or score_kind is None or unit is None:
        return None, _abstention(raw, "public evidence metric, score kind, or unit is missing", source=source)
    direction = _metric_direction(metric_kind, unit)
    if direction is None:
        return None, _abstention(raw, "public evidence metric direction or unit is unknown", source=source)
    value = _number(raw.get("value"))
    if value is None:
        return None, _abstention(raw, "public evidence value is not finite", source=source)
    if metric_kind in _RATE_METRICS:
        value = _normalise_rate(value, unit)
        if value is None:
            return None, _abstention(raw, "public pass-rate value is outside its declared unit", source=source)
    lineage_id = _text(raw.get("lineage_id"))
    correlation_group = _text(raw.get("correlation_group"))
    if lineage_id is None or correlation_group is None:
        return None, _abstention(raw, "public evidence lineage_id or correlation_group is missing", source=source)
    sample_count = _positive_int(raw.get("sample_count"))
    return {
        "source": source,
        "cohort_key": expected_cohort,
        "metric_kind": metric_kind,
        "score_kind": score_kind,
        "unit": unit,
        "direction": direction,
        "value": value,
        "sample_count": sample_count,
        "lineage_id": lineage_id,
        "correlation_group": correlation_group,
    }, None


def _deduplicate_group(group: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    first = group[0]
    comparable_fields = (
        "metric_kind",
        "score_kind",
        "unit",
        "direction",
        "value",
        "sample_count",
    )
    same_lineage = all(item["lineage_id"] == first["lineage_id"] for item in group)
    equivalent = all(
        all(item[field] == first[field] for field in comparable_fields)
        for item in group
    )
    if same_lineage and equivalent:
        return first, None
    return None, {
        "source": first["source"],
        "cohort_key": first["cohort_key"],
        "reason": "multiple public records share a source/cohort without one unambiguous lineage",
    }


def _compare_records(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    for field in ("metric_kind", "score_kind", "unit", "direction"):
        if left[field] != right[field]:
            return {"direction": 0, "reason": "same source/cohort has incompatible metric conditions"}
    left_value = float(left["value"])
    right_value = float(right["value"])
    if left["metric_kind"] in _RATE_METRICS:
        left_n = left["sample_count"]
        right_n = right["sample_count"]
        if left_n is None or right_n is None:
            return {"direction": 0, "reason": "pass-rate denominator is missing; confidence is not fabricated"}
        left_low, left_high = _wilson_interval(left_value, left_n)
        right_low, right_high = _wilson_interval(right_value, right_n)
        if left_low <= right_high and right_low <= left_high:
            return {"direction": 0, "reason": "pass-rate uncertainty intervals overlap"}
    if math.isclose(left_value, right_value, rel_tol=0.0, abs_tol=1e-12):
        return {"direction": 0, "reason": None}
    higher_is_better = left["direction"] == "higher"
    left_wins = left_value > right_value if higher_is_better else left_value < right_value
    return {"direction": 1 if left_wins else -1, "reason": None}


def _has_cycle(items: list[dict[str, Any]], edges: Mapping[tuple[str, str], Any]) -> bool:
    adjacency: dict[str, set[str]] = {item["candidate_id"]: set() for item in items}
    for winner, loser in edges:
        adjacency[winner].add(loser)
    state: dict[str, int] = {candidate_id: 0 for candidate_id in adjacency}

    def visit(candidate_id: str) -> bool:
        state[candidate_id] = 1
        for neighbor in sorted(adjacency[candidate_id]):
            if state[neighbor] == 1:
                return True
            if state[neighbor] == 0 and visit(neighbor):
                return True
        state[candidate_id] = 2
        return False

    return any(state[candidate_id] == 0 and visit(candidate_id) for candidate_id in sorted(adjacency))


def _preference_ranks(
    items: list[dict[str, Any]],
    edges: Mapping[tuple[str, str], Any],
) -> dict[str, int]:
    candidate_ids = [item["candidate_id"] for item in items]
    adjacency: dict[str, set[str]] = {candidate_id: set() for candidate_id in candidate_ids}
    indegree = {candidate_id: 0 for candidate_id in candidate_ids}
    for winner, loser in edges:
        if loser not in adjacency[winner]:
            adjacency[winner].add(loser)
            indegree[loser] += 1
    queue = deque(sorted(candidate_id for candidate_id, value in indegree.items() if value == 0))
    ranks = {candidate_id: 0 for candidate_id in candidate_ids}
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency[current]):
            ranks[neighbor] = max(ranks[neighbor], ranks[current] + 1)
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)
    return ranks


def _result(
    items: list[dict[str, Any]],
    summaries: Mapping[str, dict[str, Any]],
    *,
    status: str,
    reason: str,
    conflicts: list[dict[str, Any]],
    incomplete_comparisons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ordered_summaries = {
        item["candidate_id"]: summaries[item["candidate_id"]]
        for item in items
    }
    ranks = {
        candidate_id: int(summary.get("preference_rank", 0))
        for candidate_id, summary in ordered_summaries.items()
    }
    all_cohorts = sorted(
        {
            cohort
            for summary in ordered_summaries.values()
            for cohort in summary.get("cohorts", ())
        }
    )
    return {
        "status": status,
        "reason": reason,
        "preference_ranks": ranks,
        "candidate_summaries": ordered_summaries,
        "conflicts": conflicts,
        "cohorts": all_cohorts,
        "incomplete_comparisons": incomplete_comparisons or [],
    }


def _empty_candidate_summary(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "status": "abstained",
        "reason": "no public evidence supplied",
        "preference_rank": 0,
        "supporting_sources": [],
        "conflicting_sources": [],
        "abstained_sources": [],
        "cohorts": [],
    }


def _normalise_summary_receipt(summary: dict[str, Any]) -> None:
    summary["supporting_sources"] = _unique_references(summary["supporting_sources"])
    summary["conflicting_sources"] = _unique_references(summary["conflicting_sources"])
    summary["abstained_sources"] = _unique_abstentions(summary["abstained_sources"])
    summary["cohorts"] = sorted(
        {
            item["cohort_key"]
            for item in [
                *summary["supporting_sources"],
                *summary["conflicting_sources"],
                *summary["abstained_sources"],
            ]
            if item.get("cohort_key")
        }
    )


def _metric_direction(metric_kind: str, unit: str) -> str | None:
    if metric_kind in _HIGHER_IS_BETTER and unit in _RATE_UNITS:
        return "higher"
    if metric_kind in {"latency", "latency_ms", "runtime_ms"} and unit in _LATENCY_UNITS:
        return "lower"
    if metric_kind in {"cost", "cost_units", "cost_usd"} and unit in _COST_UNITS:
        return "lower"
    return None


def _normalise_rate(value: float, unit: str) -> float | None:
    normalized = value / 100 if unit in {"percent", "percentage"} else value
    return normalized if 0 <= normalized <= 1 else None


def _wilson_interval(rate: float, sample_count: int) -> tuple[float, float]:
    z = 1.96
    denominator = 1 + z * z / sample_count
    center = (rate + z * z / (2 * sample_count)) / denominator
    spread = z * math.sqrt(
        rate * (1 - rate) / sample_count + z * z / (4 * sample_count * sample_count)
    ) / denominator
    return max(0.0, center - spread), min(1.0, center + spread)


def _abstention(raw: Any, reason: str, *, source: str | None = None) -> dict[str, str]:
    mapping = raw if isinstance(raw, Mapping) else {}
    return {
        "source": source or _text(mapping.get("source")) or "unknown",
        "cohort_key": _cohort_text(mapping.get("cohort_key")) or "",
        "reason": reason,
    }


def _unique_references(references: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    unique = {
        (
            str(reference.get("source", "unknown")),
            str(reference.get("cohort_key", "")),
        ): {
            "source": str(reference.get("source", "unknown")),
            "cohort_key": str(reference.get("cohort_key", "")),
        }
        for reference in references
    }
    return [unique[key] for key in sorted(unique)]


def _unique_abstentions(references: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    unique = {
        (
            str(reference.get("source", "unknown")),
            str(reference.get("cohort_key", "")),
            str(reference.get("reason", "")),
        ): {
            "source": str(reference.get("source", "unknown")),
            "cohort_key": str(reference.get("cohort_key", "")),
            "reason": str(reference.get("reason", "")),
        }
        for reference in references
    }
    return [unique[key] for key in sorted(unique)]


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cohort_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _text(value: Any) -> str | None:
    """Return an exact contract identifier without case folding."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


__all__ = ["canonical_cohort_key", "rank_comparable_public_evidence"]
