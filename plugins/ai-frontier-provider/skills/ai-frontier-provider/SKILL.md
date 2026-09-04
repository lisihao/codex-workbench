---
name: ai-frontier-provider
description: "Use local, offline AI Frontier observations as a bounded external prior after checking its consent and terms boundary."
metadata:
  codex_workbench_managed: "true"
  provider: "ai-frontier"
---

# AI Frontier Provider

Use this provider only as a local, external observation source. It does not
authorize network use by itself and it does not make model-routing decisions.

## Before refresh

1. Read the local status first; `status` and `show` are offline-only.
2. If there is no valid personal-use consent receipt, keep the provider in
   `disabled_by_policy`; do not create a network workaround.
3. A `consented` receipt records a local operator choice only. It must retain
   `not_official_authorization: true`, and is not evidence of Martian approval
   or an exception to [Martian Terms](https://withmartian.com/terms-of-service).
4. Refresh at most every 72 hours. The provider rejects intervals below 24
   hours, makes no retries, and only requests public aggregate JSON.

## Data contract

- `<state-root>/ai-frontier.sqlite3` is authoritative; use its active,
  content-addressed snapshot or the CLI JSON, not internal tables.
- `Quality` is a cross-benchmark quality observation, not `pass_rate`.
- `Consistency` is a stability observation, not a success rate.
- Cost fields are publisher-defined relative observations, not assumed USD.
- `routing_boundary` prohibits direct use of frontier/oracle observations for
  routing. A consumer must apply its own capability, quality, quota, and
  evidence gates before using these weak priors.
- Model-level benchmark categories are opt-in: pass no more than eight known
  leaderboard model IDs. IDs absent from the current leaderboard are recorded
  as skipped, while aggregate observations remain usable; never collect
  prompts/responses or examples.

## Failure behavior

Network, parse, schema, duplicate, or non-finite data failures preserve the
last-known-good SQLite snapshot. Report the failure; do not invent source
timestamps, versions, model capabilities, or success rates.
