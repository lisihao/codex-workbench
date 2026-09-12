# Durable responsibility and deterministic continuation

Status: implementation authorized; preparation-failure recovery is the first delivery slice. Later slices are not implemented or deployed by this plan. Workbench is independent of the applications it develops. Production migration, new takeover authority and deployment remain separately authorized operations.

## Delivery order and ownership

| Slice | Implementation owner and files | Dependencies | Observable acceptance |
| --- | --- | --- | --- |
| Immediate preparation failure | Coordinator: `dependency_inputs.py`, `lockfile_handoff.py`, handoff tests; isolated worker: `service.py`, `test_preparation_input_receipt.py` | Existing accepted patches, recovery and request journal | Failed package installation retains completed input evidence; historical missing receipt is reconstructed only against verified evidence and matching staged input; source and task CAS remain unchanged |
| P0 durable responsibility | Coordinator serializes `store.py`, schema and API integration after owner inventory | Existing delivery objectives, session associations, attempt records | Handoff retains original responsibility until an identified recipient claims it; one session's new active objective does not erase previous unfinished objectives |
| P1 deterministic continuation | Authority service owner, sharing the existing coordinator lease and request journal | P0 ownership and explicit authorized next action | Reconciliation progresses without a new chat turn; lost response is reconciled before retry; notification acknowledgement is never execution acceptance |
| P2 bounded recovery | Existing recovery policy/observation/local recovery owners; shared service changes serialized | P0/P1; manifest and lockfile closure before downstream preparation | Environment repair, revalidation and source continuation have bounded attempts/time/quota; no identical retry without new evidence; accepted ancestors remain preserved |
| P3 current-entry acceptance | MCP adapter/bridge and session notification owners; deployment remains coordinator-owned | Existing catalogue notifications, P1 | The actual requesting host discovers and calls the tool; a fresh diagnostic connection alone is insufficient |

All paths above are relative to `src/codex_workbench/` unless named as tests. Independent tests may run in parallel; shared store/service/schema, Git integration and production release operations are serialized. No second scheduler or second production writer is introduced. Existing recovery, delivery objectives, notification outbox and request journal are extended only after their current behavior is inspected.

## Required durable records

Chat turns, execution attempts, objectives and delivery states remain separate. A responsibility handoff identifies objective/task/node/attempt/revision, previous and proposed owner, claim receipt, deadline and next action. A wait records one of resource, dependency, environment, indeterminate, approval or user pause, with release condition, responsible owner and recheck time. Unknown results require reconciliation. Explicit pause/cancel wins over automatic continuation.

## Fault acceptance matrix

Required scenarios are delivered-but-unclaimed work, crash after claim, success with lost receipt, multiple objectives in one session, repair followed by original-objective continuation, stale current-client catalogue, competing coordinators, pause racing recovery, background restart, stopped supervisor and repeated-fault backoff. Each scenario must show action/claim/acceptance evidence separately, zero duplicate side effects and preserved accepted source/evidence. Supervisor detection must not itself acquire a second production write authority.

Record unattended duration, recovery result, repeated work and user nudges from actual events. Do not claim speed or autonomy gains from design or fixture consensus. Platform restrictions on UI automation and unsolicited chat delivery remain explicit limitations, not reasons to stop an otherwise authorized background action.

## Verification and release boundaries

Use focused regressions while changing each slice, then affected integrations. Run a full gate once for each stable release fingerprint. The immediate preparation repair must ship independently of P0–P3 completion. Plans, commits, passing fixtures, releases, installed runtime and application-task acceptance are distinct milestones. Never queue or resume another coordinator's application task as part of a Workbench repair.
