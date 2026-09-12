# Durable responsibility ledger

`codex_workbench.responsibility.ResponsibilityLedger` records which existing session is accountable for one `goal_id` on one task. It is deliberately not a scheduler, task-control lease, execution claim, session-route creator, or access grant.

Each mutation appends one complete snapshot to the existing `events` journal inside `WorkbenchStore.transaction()`. No responsibility table, generic `command_receipts` write, schema migration, or background takeover loop is introduced.

## Snapshot identity

Every responsibility event contains these durable fields:

- `goal_id`, `task_id`, `node_id`, `attempt`, and `task_revision`
- `responsibility_revision`
- `original_owner`, `current_owner`, and `proposed_owner`
- `next_action` and a timezone-normalized UTC `deadline`
- a typed `wait` record or `null`

The current snapshot is the newest responsibility event for exactly `(task_id, goal_id)`. Reads use that bounded projection; they do not replay an unbounded history across tasks.

## Operations

The public methods are `open`, `propose_handoff`, `claim_handoff`, `defer`, and `inspect`.

`open` verifies that its `owner` is already the task contract's `source_thread_id` or an existing permanent task-to-session route. It records that source owner as both `original_owner` and `current_owner`.

`propose_handoff` requires the current responsibility revision and current owner. It only writes `proposed_owner`; the current owner remains accountable.

`claim_handoff` requires the exact proposed recipient, responsibility revision, node attempt, and task revision. The recipient must already be the task contract source session or have an existing permanent task-to-session route. A successful claim changes only `current_owner`; it does not grant task data, a node lease, or permission to execute.

`defer` keeps `current_owner` unchanged and may retain a pending `proposed_owner`. It records why the owner is waiting and what to do next. It never selects or claims a recipient.

For a new proposal or deferral, the caller supplies the current task/node/attempt/revision identity and it is validated against SQLite before being written. This is an explicit new snapshot, not an implicit rebase. A claim cannot rebase: its supplied identity must exactly match both the proposed snapshot and the current task/node rows.

## Typed waits

`defer` accepts an explicit object with `wait_kind`, `detail`, and `release_condition`. The supported kinds are `resource`, `dependency`, `environment`, `indeterminate`, `approval`, and `user_pause`.

Non-`user_pause` waits require a future `next_recheck_at`, and that recheck must not be after the responsibility deadline. A `user_pause` wait requires `requires_user_action: true`, no automatic recheck, and an actually paused task. A deadline expiring changes no owner and starts no work.

## Stable commands and replay

Callers provide a stable `command_id`. The ledger stores it under the task-scoped namespace `responsibility:<task_id>:<caller-command-id>` and hashes the normalized operation request. Before validating current task state, a mutation checks its original task-scoped event receipt:

- An identical retry returns that original receipt, even if later responsibility or task events exist.
- Reusing the same task-scoped command for a changed payload or another operation raises a command conflict.

This supports response-loss retry without overwriting generic command receipts or turning the latest snapshot into a false receipt for an earlier command.

## Pause, cancellation, and expiry

Paused and cancelled tasks cannot be claimed through this ledger. The ledger does not alter task state, so the existing user task-control transition remains authoritative. An unclaimed proposal and an expired deadline both retain the old current owner. An expired proposal must be replaced with a fresh proposal before its recipient can claim.

## Authority terminal projection

`reconcile_terminal(coordinator_epoch, limit=100)` runs from the existing Authority background control turn after delivery reconciliation. It first validates the active coordinator epoch inside the same SQLite transaction. It does not create a scheduler or execute `next_action`. Distinct failures produce `responsibility.reconcile_failed` events without converting a failed projection into completion.

The bounded query selects only latest nonterminal goal snapshots whose real task is already terminal, before applying its limit. This prevents earlier nonterminal goals from starving a later eligible goal. It appends:

- `responsibility.fulfilled` only when the task state is `accepted` and its optional delivery objective is absent or currently `complete`.
- `responsibility.cancelled` when the task state is `cancelled`, regardless of delivery state. It is never a success event.

The terminal snapshot keeps the source snapshot's owners, pending recipient, next action, deadline, and wait. It records the current task revision plus immutable terminal-source metadata. It does not modify tasks, nodes, leases, delivery objectives, policy, or session routes. A restart sees the terminal snapshot and emits nothing again.

`list_for_task(task_id, limit=100, cursor=0)` returns a bounded cursor page of latest snapshots for that task's goals. The cursor is an event cursor, and the method does not scan another task's history.

## Supported MCP path and notifications

`workbench_responsibility` exposes `list`, `inspect`, `open`, `propose_handoff`, `claim_handoff` and `defer`. Reads require no journal ID. Mutations preserve the same `request_id` through the MCP bridge, Authority journal and responsibility command namespace. `source_thread_id` supplies the current actor; caller-supplied alternative owner fields are not accepted. Use `list` with a task from the session objective inventory to rediscover its goal IDs after a context reset.

Proposals, claims, waits and terminal responsibility events enter the existing session notification outbox only for existing task routes. An acknowledgement remains an outbox acknowledgement: it neither claims responsibility nor accepts a task. Proactive display or waking of a Codex chat is a host capability, not implied by outbox delivery.

The background regression starts the real Authority loop with two fixture goals in one session, switches the active pointer, waits for independent acceptance and responsibility fulfilment, then restarts the coordinator and checks that no accepted node executes twice. This is keyless implementation evidence, not a measured production speed improvement or a claim that every recovery policy has been enabled.
