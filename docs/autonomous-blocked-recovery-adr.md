# ADR: deterministic blocked recovery inside the existing Authority

Status: approved implementation objective; this document separates verified baseline from the implementation plan. Production activation and publication are separate authorization steps. The DSH coordinator retains recovery CAS ownership until explicitly transferred.

## Source-grounded diagnosis

Baseline: the independent 1.17.0 connection/controlled-validation candidate, PR #13. No replacement task scheduler or database is needed.

| Existing owner | Verified behavior | Missing connection |
|---|---|---|
| `service.py: Coordinator.run_forever` | One Authority drives dispatch, quota refresh, delivery reconciliation and bounded worker pools without a chat client. | No durable recovery objective consumes ordinary blocked-node results. The worktree recovery companion is recycling/orphan recovery, not a general blocked-task repair loop. |
| `store.py: settle_node / claim_ready_node` | Revision/attempt/epoch and scope gates protect dispatch and settlement; retryable results have a bounded existing retry path. | An ordinary blocked result makes the whole task blocked, which the ready-node query excludes. An independent eligible node can consequently stop too. |
| `store.py: settle_node` | Verifier failure can schedule implementation repair. | The legacy branch resets all non-verifier workers. It does not identify whether only the validation environment failed or which source owner actually needs repair. |
| `execution_readiness.py` and `service.py: _assess_readiness` | Worktree, pinned pnpm, linker, source resolution and bounded tool checks exist; readiness failure does not call the worker. | Actual acceptance IPC/Git requirements are not selected before dispatch. Structured supplied failure origin is not consistently consumed, and summary text heuristics are not sufficient authority for recovery. |
| `delivery_lifecycle.py`, `authority_delivery.py`, delivery Store methods | Typed waits, bounded attempts/time/cost, next wakeup, epochs, leases, intent-before-effect, receipt idempotency, observation dedup and deployed identity already exist. | There is no typed parent-blocker/repair-request/deployed-fingerprint relation that resumes the original node's validation. |
| 1.17.0 Authority request journal and MCP bridge | Stable write IDs, non-replayed unknown receipts, cursor continuation, connection diagnostics and bounded node validation exist. | These are execution/observation primitives, not an autonomous repair policy. |

## Decisions

1. Keep the existing task and node state vocabulary. Add a durable recovery projection in the same Authority SQLite database, owned by one small Store component. An accepted implementation plus environment-blocked verifier is exposed as `verification_wait` in that projection; it is not a new claim that the task is accepted.
2. Classify recovery from validated current-attempt attribution, readiness/validation artifacts, explicit failure codes and authoritative state. Keep missing or ambiguous evidence unclassified. A summary keyword is diagnostic context, not permission to retry, relabel a code fault or expand privileges.
3. Store one bounded episode per task/node/attempt/failure fingerprint: origin/category, source evidence/cursor, action owner, permission requirement, retry/time budget, next wakeup, last material progress, deduplicated notification, and optional repair relation. Separate the failure fingerprint from the fresh revision/worktree/source fingerprint required for an individual action.
4. Reconcile from the existing Coordinator turn and append-only events, with a persisted cursor and low-frequency program sweep for missed events. Ready recovery work uses the existing bounded execution pool; there is no model polling loop and no second scheduling authority. Observation-only waits do not consume action retries.
5. Reuse the existing offline dependency materializer, readiness assessor, source-only recovery and fixed validation profiles. Record intent before invoking an action and reconcile uncertain outcomes from existing receipts/bindings rather than replaying them. Never change old worker results or turn blocked directly into accepted; successful validation proceeds through the original fenced verification/recovery lifecycle.
6. Recovery policy is explicit and task-scoped. Existing allowed local actions can proceed after enabling that policy. Auth renewal, new permission, destructive actions and unknown external effects create one durable user-action requirement; pause, cancel and rejection take precedence at both claim and commit. Installing the mechanism does not silently take DSH recovery ownership.
7. A persistent tooling defect may create at most one deduplicated bounded repair request for the current failure fingerprint and authorized repair scope. A repair being accepted is not deployment. The parent remains in a typed deployment wait until a matching verified deployment identity and fresh readiness evidence exist. Publication must already be authorized or request authorization once.
8. Progress means source/evidence cursor, patch, check result or phase advancement. Process presence and heartbeat alone are not progress. A deadline or lack of material progress permits one bounded investigation, not an assumption of deadlock or repeated model wakeups.
9. Preserve independent dispatch and dependency gates: an environment-blocked node cannot hold an unrelated legal node, but its dependents cannot pass until acceptance. Code failure returns to the evidenced source owner; unrelated accepted ancestors stay accepted.

## Implementation sequence and file ownership

| Slice | Owned files | Acceptance |
|---|---|---|
| Pure policy and classification | `src/codex_workbench/node_recovery_policy.py`, its focused tests | Explicit evidence maps to bounded actions; missing evidence/auth/unknown effects/pause never become implicit permission. |
| Durable recovery projection | `src/codex_workbench/node_recovery_store.py`, its tests | Same SQLite/transaction/event helpers, idempotent episode and action IDs, revision/epoch fences, budgets, typed waits, repair linkage and notification dedup survive restart. |
| Action adapter and Coordinator wiring | `src/codex_workbench/node_recovery.py`, targeted `service.py`/`store.py`/MCP/config wiring | Reuse actual existing helpers, preserve source/accepted ancestors, allow independent legal work, and never replay an unknown action. Coordinator owns all integration and shared-file edits. |
| Independent closed-loop fixtures | dedicated recovery integration tests plus existing connection/source-only/deployment fixtures | No chat turns or model polling are needed for the scenarios below; publication and activation remain separately reported. |

This worktree is separate from the 1.17.0 PR. Shared schema, Git, generated output, release and deployment operations are serialized by the coordinator; workers own only named new modules and their tests.

## Required observable scenarios

1. Dependency/resource unavailable, then recovered: original node continues without repeated development.
2. IPC/Git validation permission mismatch: use the selected narrow profile, retain implementation and scope restrictions.
3. Generated receipt contamination: preserve actual source and unknown ignored data through existing source-only recovery.
4. Observer/MCP disconnect and client exit: Authority continues; reconnect resumes cursor evidence.
5. Lost write receipt: query the same idempotent request; one effect.
6. Repair accepted but not deployed, then matching deployment verified: parent validation resumes only after the latter.
7. Restart around capture, assignment and settlement: existing fencing/receipts prevent duplicate effects.
8. Persistent identical fault: bounded attempts and one notification per unchanged actionable condition.
9. User pause, cancellation or denied approval: recovery cannot pass the control decision.
10. Blocked node with independent and dependent siblings: independent work can run, dependent work cannot cross acceptance.

Measurements will report fixture-observed elapsed delivery time, environment retries, repeated checks, model wakeups and human-action counts. They are not production speedup claims. Reuse unchanged passing evidence; run the required full gate only at the stable delivery boundary.
