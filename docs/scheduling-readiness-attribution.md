# WB-SCHED-1 P0 readiness and execution-attribution contract

This document records the P0-A/P0-B delivery boundary only. It does not
describe a learned graph optimizer, controlled effectiveness result, or DSH
integration. Workbench remains independent from DSH; its SQLite event ledger
is the task-state authority.

## Acceptance boundary

Before a worker or verifier can consume a costly model turn, the coordinator
checks the exact allocated worktree with the bounded readiness contract. A
failed check is an `environment` outcome with structured remediation, never a
model-quality failure. The check does not install packages, log in, run a model
prompt, or invoke project lifecycle scripts merely to probe readiness.

Each settled node attempt may carry a typed `execution_attribution` value in
the existing `NodeResult` and terminal event envelope. The record separates:

- The requested provider/model route from an observed model identity.
- Attested, unattested, and unknown observations. A requested model never
  becomes an observed model by inference.
- Selected and rejected candidates with bounded reasons and durable pointers.
- `environment`, `auth`, `quota`, `transport`, `model`, `verification`,
  `scope`, `cancel`, and `unknown` failure origins.
- Queue, prepare, execute, and verify timing boundaries. Missing boundaries
  remain explicitly `unknown`.
- Dependency, scope, provider-quota, readiness, CPU, memory, and I/O
  conditions. CPU/memory/I/O are not fabricated when unmeasured.

The only persisted payloads are bounded values and references to existing
events, quota rows, scope contracts, and content-addressed artifacts. No
second database, scheduler, full source snapshot, or provider transcript is
introduced.

## API, storage, and event contracts

| Surface | P0 contract |
| --- | --- |
| `execution_readiness.py` | `ExecutionReadinessRequest` describes the exact worktree plus explicit tools, paths, source roots, and optional pnpm policy. `ExecutionReadinessReport` exposes `ready`, structured checks/failures, `environment` origin, and elapsed probe time. |
| `execution_attribution.py` | `ExecutionAttribution` schema version 1 serializes state reference, requested/observed identity, physical-call identity, candidates, failure, conditions, timings, and bounded references. |
| `NodeResult` | Adds optional `execution_attribution` while retaining `actual_model` as a legacy compatibility field. With typed attribution, model-quality consumers must use an attested `observed_model`; an unattested or unknown identity cannot enter an attested-quality bucket. |
| Coordinator | Stores readiness JSON in the existing artifact store under `execution-readiness`, blocks before executor dispatch on failure, attaches coordinator-observed timing/route/scope context, and settles through the existing store path. |
| SQLite store | No P0 table or scheduler was added. `nodes.result_json` and `node.accepted`/`node.failed`/`node.blocked` event payloads carry the result. Settlement validates that attribution references bind to this node attempt and existing artifacts/events/quota rows. |
| Performance and scheduler metrics | Replay terminal events. Infrastructure failures stay in end-to-end outcomes but are excluded from model-quality denominators. Physical-call keys deduplicate only attested provider/call IDs; API dollars and subscription tokens stay independently unknown unless directly measured. |

`actual_model` is not an independent attestation. If a typed record has an
attested observed model, the compatibility field must match it; otherwise a
missing legacy field is allowed and the observation remains unknown or
unattested.

## Source-to-requirement matrix

| Requirement | Implementing source | Assembled evidence |
| --- | --- | --- |
| Bounded exact-worktree readiness; non-Node usability; pnpm/tool/source failures are environmental | `src/codex_workbench/execution_readiness.py`, `src/codex_workbench/service.py` | `tests/test_execution_pipeline_attribution.py` blocks a pnpm-shaped target before any keyless executor dispatch. |
| Source resolution is local to each allocated worktree | `src/codex_workbench/execution_readiness.py`, `src/codex_workbench/service.py` | The integration test allocates two repositories with the same package name and verifies distinct target-local origins. |
| Preserve accepted worker changes when verifier readiness blocks | `src/codex_workbench/service.py`, `src/codex_workbench/dirty_worktree_recovery.py`, `src/codex_workbench/store.py` | The integration test accepts a worker patch, blocks verifier readiness, verifies the patch artifact remains, and proves the worker stub ran only once. |
| Bounded requested/observed identity, candidates, taxonomy, conditions, timings, and references | `src/codex_workbench/execution_attribution.py`, `src/codex_workbench/model.py`, `src/codex_workbench/service.py`, `src/codex_workbench/store.py` | Terminal event attribution is checked against its matching `node.started` cursor; an unavailable fixture identity stays unknown. |
| Fallback/retry/verifier correlation and physical-call accounting | `src/codex_workbench/service.py`, `src/codex_workbench/store.py`, `src/codex_workbench/scheduler_metrics.py`, `src/codex_workbench/performance.py` | The integration test drives a keyless Claude-to-Codex fallback, durable retry, and verifier, then requires one shared attested physical-call usage key. The coordinator retains only validated, current-attempt direct receipt evidence while adding its own state, conditions, and timings. |
| Do not mix public benchmarks into local outcomes | `src/codex_workbench/performance.py`, `src/codex_workbench/performance_report.py`, `src/codex_workbench/routing_v3.py` | Metrics retain source-separated public evidence and local-outcome ledgers; this integration test exercises only local event evidence. |

## Verification procedure

This node runs only its bounded, keyless integration test:

```sh
PYTHONPATH=src scripts/python-runtime -m unittest discover -s tests -p 'test_execution_pipeline_attribution.py'
```

It does not call Codex or Claude, use API keys, change quota policy, install
dependencies, or run the whole suite. The independent final verifier must run
the project-mandated full gate once after the integrated worktree is stable;
this node does not claim that gate has run or that its worker result is
accepted.

## Covered scope, residual risk, and pending work

Covered P0 evidence is readiness-before-execution, retained accepted diffs
across a verifier readiness block, target-local source resolution,
event-cursor correlation, unavailable identity remaining unknown, and bounded
storage/metrics envelopes.

The keyless integration test deliberately supplies an artifact-backed,
attested physical call across a fallback/retry pair. It is an acceptance guard
for the runtime adapter: if the coordinator discards that direct evidence while
constructing its own attribution, the test must fail. A passing test proves the
local wiring only; it does not substitute for a real subscription acceptance or
prove provider-receipt behavior under production transport failures.

Repair lineage: this worktree reconstructed the accepted integration input
tree `f0710eca6a0ca15a5c2664dbeaf12b23292496e5` from base
`7e881005ab6078c5a625fad090a4a73a09854a52` using dependency-input receipt
`sha256:17d4b875122ed7cea8126cb54a32146270db896d99396c9a2365697b8132908d:dependency-input.json`
and the ordered attribution, environment, runtime, and performance patch
receipts. The P0 repair keeps a typed executor observation and physical call
only when its state points to the current task/node/attempt, its provider and
observed model agree with the effective result boundary, and its direct
native/provider provenance references an artifact already in that result.
Coordinator state, routing, conditions, timing, and failure fields are then
added without relabeling the executor source. Legacy `actual_model`, requested
model, malformed flags, and a prior fallback/retry attempt remain unattested
or unknown; no physical-call key is synthesized or reused for them.

Focused fixture evidence now proves direct receipt preservation and one
physical-call key across the fallback/retry pair. This is implementation
evidence only, not an L3 acceptance claim: the independent verifier still
owns the single full release gate and any minimal real subscription acceptance.

Other residual limits are intentional: no provider GPU state, quota cost,
token count, CPU/memory/I/O wait, absent timestamp, or observed model is
invented. The test also checks that a source sentinel and the word `transcript`
do not appear in its durable pipeline events, but it is not a general proof
about every possible artifact producer.

Explicitly pending beyond this package:

- P1-C bounded graph-level shadow optimizer.
- Controlled effectiveness evaluation, including calibrated outcomes under
  real production transport and provider-receipt failure modes.
- A separate DSH plugin port; it remains out of this repository and must not
  become a WB runtime dependency.

None of those objectives is completed or claimed by WB-SCHED-1 P0.
