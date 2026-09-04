---
name: codex-radar-provider
description: Consume locally consented or officially authorized Codex Radar observations through the host provider CLI for model-aware scheduling, with SQLite-backed offline last-known-good snapshots and explicit freshness boundaries.
---

# Codex Radar Provider

Use this Skill when Workbench or DSH needs Codex Radar model-quality, cost, runtime,
or other benchmark observations for scheduling and capability calibration.

## Provider CLI

Use the host-provided `codex-radar-provider` command for collection and reads, for example:

```text
codex-radar-provider --state-root <DIR> consent --personal-use
codex-radar-provider --state-root <DIR> status
codex-radar-provider --state-root <DIR> show [--snapshot-id <ID>]
codex-radar-provider --state-root <DIR> refresh --authorization-file <RECEIPT>
codex-radar-provider --state-root <DIR> import --authorization-file <RECEIPT> --payload-dir <DIR>
```

The repository's portable Python package implements this host contract; this Skill does not start
it automatically. The Provider's `<state_root>/radar.sqlite3` is the authoritative store for
snapshots, raw payloads, models, insights, and the active pointer. JSON files under `raw/`,
`generations/`, and `active.json` are compatibility projections only; a valid legacy projection is
automatically migrated into SQLite on first read. Prefer a scheduled refresh at most once per
86400 seconds when online, and use `status`/`show` for offline reads. Preserve the returned
`schema_version`, `snapshot_id`, `digest`, timestamps, authorization/consent, cache state, source
URLs, and model observations in consumer provenance.

`status` exposes the database descriptor `{backend, schema_version, path, row_counts}`. Treat
`radar.sqlite3` as the source of truth even when a JSON projection is present.

## Offline and consent rules

- If refresh cannot reach the source, use the newest complete `last-known-good` snapshot and mark
  it `stale` with the original observation time. Read this LKG from SQLite; do not fabricate current
  values or silently reset freshness.
- For personal use, accept a receipt only when it has `status=consented`,
  `basis=local_operator_consent`, `scope` containing `public-json`, and a valid `accepted_at`.
  This is a local operator consent and must never be described as site or publisher
  `authorized` permission.
- The upstream `current.json` still states that full API/derived integrations require site
  authorization. If such an official receipt is not available, personal-use public JSON remains
  a separate, explicitly attributed local decision; do not imply broader permission.
- If no valid consent/authorization receipt exists, or no valid snapshot exists, fail closed for
  Radar-backed routing. The caller may continue with its built-in capability baseline or defer the
  decision, but must not present unverified Radar data as current.
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

The Provider SQLite database and Workbench task/event SQLite database are different stores. DSH
must consume the stable CLI/JSON contract read-only and must not copy or write Workbench task
state, or couple itself to Provider table names.

Unknown fields may be retained but must not be interpreted as permissions. A consumer that cannot
understand the declared schema or freshness state must reject the snapshot rather than downgrade it
to an apparently current result.

Never convert `iq` into `pass_rate`; only an explicit valid `pass_rate` with a positive
`sample_count` may become a quality prior. DSH may consume this same contract later without any
Workbench dependency; do not modify DSH while installing this plugin for Workbench.
