# Empty Claude quota-observation recovery

`quota_snapshots` is the single append-only SQLite ledger for Claude quota
evidence. Every collector row remains raw. An effective observation is a
read-time projection only: it never writes a balance, timestamp, reset window,
or replacement authority row.

When the current raw observation is authenticated with the native subscription
but has no quota values, `WorkbenchStore.latest_quota()` may use an earlier
complete row only when all of these are true:

- the caller supplied its normal freshness limit and both rows remain fresh;
- producer, source, schema, and CLI-version identity match;
- reset bindings match and both known reset deadlines are still ahead;
- every intervening row is a compatible empty observation; and
- append order agrees with observation-time order for the recovered run.

A producer-confirmed collection failure with no parsed reset payload can use a
complete row only before that row's known reset deadlines. An ordinary empty
row with unknown bindings fails closed. Repeated empty rows never refresh the
complete row's age. Authentication loss, incompatible provenance, changed or
unknown reset binding, reset expiry, stale timestamps, partial rows, and
out-of-order appends all fail closed. A partial protected or red pool remains
authoritative for refusal.

The selected complete row retains its immutable row ID, original `observed_at`,
and reset windows. The `effective_observation` receipt exposes the raw and
effective row IDs, authentication state, collection state, selection reason,
and nonsecret provenance. Planner receipts surface it as `quota_observation`.
Claude execution also re-resolves the selected durable row immediately before
the provider boundary; an unresolved reference falls back to Codex.

Existing policy is unchanged: absent model-specific Sonnet/Fable pools use the
shared weekly pool, the 20% reserve and 25% stop line remain fail-closed, and
shared weighted capacity is unchanged.
