# Task-scoped blocked recovery

Version 1.18.0 adds a recovery projection to the existing Workbench Authority and SQLite database. It is not a second scheduler, a chat automation, or an acceptance shortcut. Policies are disabled unless explicitly enabled for a task. The [ADR](autonomous-blocked-recovery-adr.md) records the source findings and intended completion criteria.

## Available behavior

The existing Coordinator control pool consumes bounded event metadata and sweeps enabled policies at a low frequency. A task/node/attempt/failure fingerprint identifies an episode. Each action has an intent, original request ID, current revision and attempt, Authority epoch, lease, stage-local budget, next wakeup and receipt. Unknown writes are reconciled from their original receipts, never replayed. A lost successful validation receipt retains the same check-reuse facts as a direct receipt.

| Action | Current adapter | Authority |
|---|---|---|
| `observe_readiness` | Existing bounded readiness assessor; no install or retry | Explicit task policy; captures current node and Authority identity |
| `narrow_validation` | Existing fixed controlled-validation profiles | Explicit profile allowlist, fresh source/runtime preview and stable request ID |
| `source_only_recovery` | Existing source-preserving blocked recovery | Fresh preview and source digest; does not assert unknown historical effects |
| `request_repair` | Existing asynchronous planning queue | Explicit repair repository/scopes; one stable repair request per failure; no publication authority |
| `materialize_dependencies` | Existing offline materializer, cached template required, at most 60 seconds | Explicit task policy and current source/runtime binding; unknown effects are not replayed |
| `resume_node` | Existing blocked-node retry CAS | Proven pre-execution dependency failure, fresh readiness and clean source with matching revision/attempt/allocation; never changes historical acceptance |
| `repair_source` | Existing failed-attempt preparation followed by the ordinary worker executor | Explicit action grant, ready blocked non-verifier, fresh source/revision/attempt/policy binding and bounded repair count; never accepts the task |

The fixed validation profiles currently cover the bounded DSH B IPC and pairing checks documented in [controlled validation](controlled-validation.md). This does not provide arbitrary acceptance commands, arbitrary Unix/Git grants or generic project compatibility. Existing readiness checks and those fixed profile plans do not yet constitute a general pre-dispatch capability proof for every project's acceptance surface.

## Configuration and observation

`workbench_list_tasks` includes a compact `current_status` read in the same database snapshot as each summary: current revision/state, active scheduler phases, latest material-event metadata and UTC `observed_at`. Recovery preparation, verification and ordinary execution can appear together when independent nodes run concurrently. These are scheduler phases, not proof of CPU activity or a particular compiler step. The projection reads no result bodies, steering, event payloads or artifact content; detailed history remains in the existing paginated event and artifact tools. A database read failure returns `observation_unavailable` and changes no task state.

Use `workbench_configure_node_recovery` through the same authenticated Authority service with `task_id`, `expected_revision` and a strict `policy` object. Mutations require a stable service `request_id`. `workbench_get_node_recovery` is read-only and reports the policy, actually available actions, compact episodes and measured ledger counts. Policy installation does not transfer recovery ownership from another operator or grant release/deployment permissions.

```json
{
  "enabled": true,
  "allowed_actions": ["observe_readiness"],
  "validation_profiles": [],
  "max_action_attempts": 3,
  "time_budget_seconds": 900,
  "backoff_seconds": 30,
  "max_backoff_seconds": 300
}
```

This example only observes readiness. It cannot install dependencies, requeue a clean node or publish a repair. Add a fixed validation profile only when the task owns its required paths. Repair planning additionally requires explicit `repair_repository` and `repair_allowed_scopes`; it inherits the parent's Claude policy and retains the existing native-auth/quota gates. No API-key fallback is added.

An exhausted stage with only known failed action receipts may enter the authorized `request_repair` stage once for its existing failure identity. The repair adapter verifies the durable episode, policy revision and action ledger before planning. It retains the observed failure category: repeated environment failure is not proof of a tooling bug. Unknown receipts, a pending repair, expired time budget, pause, cancellation or missing repair scope cannot trigger another repair request.

## Session notifications

The Authority control turn projects selected failure and recovery events into a durable session outbox without waking a model. Task-to-session routes survive a later change of the session's active task. Events without a route remain available for late binding; repeated projection and restart do not create a second notification. Notifications contain fixed event metadata rather than diagnostic bodies or source paths.

`workbench_read_session_notifications` reads pending entries for one originating session. `workbench_ack_session_notification` records a session-bound acknowledgement through the existing Authority request journal. Reading does not acknowledge, and an acknowledgement does not prove that Codex displayed a message. This candidate does not yet provide a verified daemon-to-Codex conversation push adapter. Durable outbox acceptance and actual host delivery must be reported separately.

## Preservation and repair

An environment-blocked node no longer prevents an independent legal node from being claimed. Dependency acceptance and read/write scope gates still apply. Pause and cancellation override recovery at claim and settlement; unknown effects retain their original evidence and require a decision. One bounded investigation may report missing durable progress for a running attempt, but it neither labels a live PID as progress nor declares a deadlock or kills the process.

`repair_source` separates source preparation from business acceptance. The `blocked_source_repair` mode restores the retained source and dependencies before invoking the ordinary worker; it does not require the failing business checks to pass before the worker can repair them. The original blocked result remains unchanged. Preparation failure restores that blocked attempt and records separate rollback evidence. The existing final verifier remains mandatory, and the existing strict source-only recovery path still runs its acceptance checks. Repair dispatches are counted across attempts for the current task/node policy revision, so creating another attempt does not reset the repair limit. A repeated request ID reads its original receipt rather than queuing another attempt.

The verifier now returns `repair_node_ids` (empty when the source owner is unknown). A failed verifier does not reset every worker. Only explicit, evidenced accepted owners enter `accepted-source-repair-v1`: their original successful result, patch, dependency input and allocation remain the source for a fresh attempt. Unrelated accepted ancestors remain accepted. Invalid owner lists, missing source evidence and uncovered accepted descendants are rejected without discarding implementation.

Only a bounded internal error before executor start, with its deepest frame in Workbench package code, can produce the new typed `tooling_bug` receipt. Execution-started or external-frame failures remain indeterminate. Free-form summaries are not recovery authority. A ready environment is not proof that code is complete.

Failed clean-target preparation emits a `recovery-preparation-failure` artifact with source and recovery attempts, the failed phase, an explicit code and existing evidence refs. `acceptance-command-failed` identifies a failed check, not its cause: it does not establish a missing dependency, a business-code defect or permission to change acceptance. Rollback keeps the original source result while retaining this separate failed-attempt evidence.

A repair request fingerprint and a verified deployment fingerprint are separate values. The parent waits for the repair task's exact deploy/live-verify receipt and dispatch chain, current installation identity and fresh readiness. An accepted repair is not a deployed repair. If the Authority restarted, historical live verification remains historical; a current, content-addressed readiness proof may confirm the same installation for parent validation, without inventing a new HTTP probe. Missing publication authority is one explicit user-action wait, not an automatic deployment.

## Evidence and release boundary

The focused fixtures exercise original-node continuation, independent-node dispatch, pause/cancellation, bounded repeated faults, lost-response/restart deduplication, accepted-source owner repair, actual metadata-only repair enqueue, controlled validation isolation and deployment receipt validation. Local-adapter fixtures use isolated Git/SQLite and a controlled materializer; they do not prove that an arbitrary production project's dependency cache is complete.

Runtime delivery requires an identified version/commit, authorized installation and separate activation evidence. No DSH task acceptance or production speedup follows from these fixtures. Ledger metrics distinguish episode/action counts and deduplicated user-action notices; model wakeups remain unknown unless actual provider evidence measures them. Workbench and DSH code, releases, deployments and acceptance remain separate.
