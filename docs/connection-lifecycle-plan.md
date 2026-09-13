# Connection lifecycle repair plan

## Objective and acceptance

Extend the existing Workbench maintenance objective without changing DSH source, task CAS, recovery policy or the active C4 execution. Make the four connection layers observable and demonstrate compatible Authority fixes through the same Codex host session. Installed files, loaded processes, connected peers, and host-visible tools are separate facts. Missing observations remain unknown. No claim promises restart-free handling of every upgrade.

The implementation baseline is v1.19.4, commit `d7cdcdfb289e843c668e840f4b23c4d56d01081f`. Existing request journaling, read-only classification, bridge reconnects, event cursors, bounded transport and installer rollback remain the starting point. PR22 continuation responsibilities and the unmerged scoped-steering work are separate changes, not release dependencies or permission to migrate the production schema.

## Phase 1: observations and compatible service updates

1. Extend an existing read-only diagnostic tool with additive, versioned connection evidence. Report Authority service instance, loaded version, protocol and capability digest; remote adapter loaded identity; local bridge loaded identity; and only the host catalog interaction actually observed. Legacy peers produce explicit unknown fields. Do not infer process identity from an installed manifest or from Authority's version in the adapter initialize response.
2. Audit the Authority's authoritative operation classification and client permission ceiling. Unknown operations remain mutating or rejected, never implicitly read-only. Reuse existing request identities and receipt lookup. Document remaining source-request, action-start, external-effect and result evidence gaps instead of manufacturing success receipts.
3. Bind compatible request handling to observed service/version identities without replaying unknown mutations. Preserve existing ledger formats unless a separately authorized migration is necessary. Check MCP, HTTP and CLI semantics at their real owner.
4. Establish a same-host-session baseline and verify a compatible Authority business fix after an authorized safe-point update. A fresh test client is fixture evidence, not acceptance of the user's existing Codex connection. Do not stop C4, invent host menus, bypass MCP-only task controls or force-restart Codex.

The first implementation slice is additive four-layer diagnostics through the existing health tool and actual loaded process receipts. It changes no scheduler, schema, operation authorization or replay behavior. Development uses focused L2 fixtures; release and production installation require the normal L3 gate and explicit release/installation scope.

## Ownership and dependencies

| Slice | Owner | Files and responsibility | Acceptance |
| --- | --- | --- | --- |
| Authority request audit | independent explorer | authority_service.py, service_client.py, service API and tests, read-only | existing guarantees and exact gaps with source locations |
| Diagnostic and prior-work reuse audit | independent explorer | diagnostic script/tests, connection E2E, relevant PR22/P2 code, read-only | reuse map; no duplicate subsystem |
| Connection evidence | coordinator, then bounded worker for isolated helpers/tests | new evidence helper/tests; shared bridge, adapter and API edited serially by coordinator | old peers stay unknown; observations identify the actual responding process |
| Request lifecycle evidence | isolated implementation worker | authority_service.py and its focused tests only | admission identity and action-start/result phases persist once; old-instance facts remain unknown; no schema or fingerprint change |
| Integration and delivery | coordinator only | Git, shared transport, release metadata, installer and runtime | final tree checks, fixed revision, backup, runtime evidence; no concurrent deployment |

Workers do not touch DSH worktrees, current task state, shared Git state, installed scripts or deployment. The original DSH coordinator exclusively owns C4 and later recovery decisions.

## Conditional phase 2

Proceed only if phase 1 demonstrates that compatible service changes still require frequent client replacement. Use a minimal stable stdio front end and replaceable adapter, not another scheduler or Python module hot-loading. Validate protocol, health and permissions before admitting new requests; drain old requests. Preserve a single ledger writer, generation fencing and concurrent-session upgrade exclusion. Clean up only owned processes. Bound backoff, queues and logs; retain stream/event cursors and backpressure. Rollback must not overwrite live request records. State the initial migration and stable-front-end upgrade exceptions explicitly.

Keep common and high-risk named tools. Introduce versioned capability discovery plus governed preview/execute/status only for a demonstrated need; never expose arbitrary shell or URL execution. Bind preview to operation, arguments, scope, permissions, version and CAS, then revalidate before apply. list_changed is a notification, not proof that Codex refreshed its catalog.

## Verification and measurements

Reuse current loss-of-write-response, receipt reconciliation, reconnect, cursor and rollback fixtures. Add affected cases for identity drift, old peers and unavailable host observations. If phase 2 is justified, additionally cover interrupted streams, two-session upgrade contention, incompatible backends, rollback ledger compatibility, pause/cancel during upgrade, stale host catalogs and bounded sleep/wake or network-change recovery. Do not imply unsupported streaming behavior is already implemented.

Record observed restart requests, recovery duration, duplicate effects, orphan processes and data loss with an explicit measurement scope. Unobserved production metrics are unknown, not zero. Run focused checks while editing, reuse unaffected passing evidence, and run the required full gate once at the stable release boundary. Production deployment must not interrupt C4; lack of a safe point is a deployment wait, not permission to cancel work.

## Implementation checkpoint

The first identity slice is committed as `8e0b8bb`. Its 49 focused checks include a real isolated bridge/CLI/HTTP test retaining the bridge and adapter PID and instance across Authority recreation. The current Codex session successfully called the existing health tool before installation of this slice; it returned no connection_evidence. That is a same-session baseline, not an upgrade success claim. Production installation and the same-session after-update observation remain pending.

The classification audit found that the adapter previously OR-ed catalog hints with the shared classifier. The next slice removes that expansion and enforces a recognized bridge ceiling plus the HTTP client's shared classification. Existing instance ownership already fences journal settlement and same-ID receipt recovery. Add admission-version and action-start observations to the existing event stream without changing the immutable request fingerprint or schema. This records the service version that admitted a request; it does not silently add a new client-supplied version CAS precondition. Generic external-effect outcomes remain unknown unless the relevant operation supplies its own measured evidence.
