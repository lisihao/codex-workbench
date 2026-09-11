# Correcting a missing acceptance prerequisite

`workbench_amend_task_acceptance` adds the fixed `dsh-host-types-before-client-v1` prerequisite to a blocked task. It is an operator-controlled amendment through the existing Authority request journal, not another scheduler or an automatic retry. It changes only the acceptance command list and its contract digest, advances the task revision, and records both old and new contracts. It does not queue work, change a node attempt or allocation, alter an accepted ancestor, or change scope, model, quota, or external-write permissions.

The profile is for DSH repositories whose `build:lib:host` script declares `tsc -b tsconfig.host.json && tsdown --env.DSH_BUILD_FACE host`. Host bundling generates Typert remote declarations consumed by Client compilation. Running a partial Client `tsc -b` without that prerequisite can fail even when source-level Vitest checks pass. Do not fabricate declarations or map Host source into Client compiler paths to hide this error.

The amendment inserts two fixed commands before the original Client `tsc -b`: the Authority-pinned Node runs the installed TypeScript entry with `-b tsconfig.host.json`, then the installed tsdown entry with `--env.DSH_BUILD_FACE host`. Every original acceptance command remains in its original order. The tool does not accept arbitrary replacement commands, remove failed checks, or run the build during amendment.

## Operator sequence

Read the current blocked task and node. Supply `task_id`, `node_id`, `expected_revision`, `expected_attempt`, `expected_contract_hash`, `profile_id`, `reason`, and `dry_run: true`. The preview is read-only and binds the current allocation, source prerequisite files, installed entrypoints, runtime and old contract to its returned fingerprint.

The MCP catalog requires `request_id` for both preview and apply. Use distinct stable IDs for the two different payloads. The Authority recognizes the preview as read-only even though the catalog marks the tool as potentially mutating.

Apply with the same fields, `dry_run: false`, the preview's `expected_fingerprint`, and a stable Authority `request_id`. The transaction repeats the task/node/revision/hash/allocation and concurrent-validation checks. A paused, cancelled, running, stale or sealed recovery target is rejected. A lost response is resolved by querying that same request ID, never by resending with a new ID.

Permanent source-only retention prevents reclaiming or quarantining the original worktree; it does not freeze the task's acceptance commands. After preparation rolls back to the original blocked attempt, that retained allocation may be amended while the source files and retention events remain unchanged. A current recovery record or an active later-attempt allocation still prevents amendment, as do concurrent validation, approval and normal revision/hash fences.

After a successful amendment the task is still blocked. Its authorized coordinator reads the new revision and contract digest before choosing the existing recovery operation. The amendment does not transfer recovery ownership or authorize parallel CAS writers. Old validation logs and patches remain evidence of the earlier attempt.

## Evidence reuse

A prior passing log is not automatically proof that every input is unchanged. Legacy recovery logs that lack complete command/source/runtime bindings remain historical evidence, not silently reusable acceptance. One necessary revalidation after correcting the prerequisite is distinct from retrying an unchanged failed plan. New recovery command logs record available input identities and fingerprint coverage; incomplete coverage is explicit. Coverage refers only to recorded recovery inputs, not the complete inherited environment or dependency closure, and `reuse_authorized` remains false. This maintenance change does not implement cross-attempt evidence migration or automatically skip checks.

Recovery currently assigns the target worktree only after preparation and acceptance finish. During preparation, a null allocated worktree is not evidence of a hang. Use the terminal recovery event and its stored preparation result to distinguish successful preparation, command failure and rollback. Rollback preserves the original authoritative source; it does not by itself deliver the original objective.
