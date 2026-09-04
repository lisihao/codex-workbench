---
name: codex-radar-provider
description: Consume authorized Codex Radar observations through the host provider CLI for model-aware scheduling, with offline last-known-good snapshots and explicit freshness boundaries.
---

# Codex Radar Provider

Use this Skill when Workbench or DSH needs Codex Radar model-quality, cost, runtime,
or other benchmark observations for scheduling and capability calibration.

## Provider CLI

Use the host-provided `codex-radar-provider` command for collection and reads, for example:

```text
codex-radar-provider --state-root <DIR> status
codex-radar-provider --state-root <DIR> show [--snapshot-id <ID>]
codex-radar-provider --state-root <DIR> refresh --authorization-file <RECEIPT>
codex-radar-provider --state-root <DIR> import --authorization-file <RECEIPT> --payload-dir <DIR>
```

The repository's portable Python package implements this host contract; this Skill does not start
it automatically. Prefer a normal scheduled refresh when online, and use `status`/`show` for
offline reads. Preserve the returned `schema_version`, `snapshot_id`, `digest`, timestamps,
authorization, cache state, source URLs, and model observations in consumer provenance.

## Offline and authorization rules

- If refresh cannot reach the source, use the newest complete `last-known-good` snapshot and mark
  it `stale` with the original observation time. Do not fabricate current values or silently reset
  freshness.
- If no valid snapshot exists, or the source requires authorization that cannot be proven, fail
  closed for Radar-backed routing. The caller may continue with its built-in capability baseline or
  defer the decision, but must not present unverified Radar data as current.
- Keep collection low-frequency and cache-aware. Do not trigger a model call, login flow, paid API
  request, or credential export merely to refresh Radar data.

## Quota boundary

Radar is a quality/benchmark prior, not a live entitlement or quota ledger. Never infer remaining
Codex/Claude quota, reservation availability, protected-pool thresholds, or admission permission
from Radar fields. Use the host's real quota collector for those decisions.

## Workbench and DSH consumer contract

Both consumers should accept the same provider JSON contract:

- envelope: `schema_version`, `snapshot_id`, `digest`, `upstream`, `source_urls`, `fetched_at`,
  `source_updated_at`, `authorization`, `cache`, `models`, and `insights`;
- model observation: exact `provider`, `model`, `reasoning_effort`, `routing_eligible`,
  `pass_rate`, `iq`, `sample_count`, `avg_cost_usd`, `avg_runtime_seconds`, and `metric_sources`;
- routing use: treat observations as bounded priors behind local quality gates, capability checks,
  quota checks, and deterministic tie-breaking;
- pin `snapshot_id` (and the observation digest when the host exposes one) to each task/evidence
  contract so later refreshes do not re-route active work.

Unknown fields may be retained but must not be interpreted as permissions. A consumer that cannot
understand the declared schema or freshness state must reject the snapshot rather than downgrade it
to an apparently current result.

Never convert `iq` into `pass_rate`; only an explicit valid `pass_rate` with a positive
`sample_count` may become a quality prior. DSH may consume this same contract later without any
Workbench dependency; do not modify DSH while installing this plugin for Workbench.
