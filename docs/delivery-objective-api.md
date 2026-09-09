# Durable delivery objective API

An authenticated client can attach an immutable delivery objective to an existing task or planning reservation with `POST /api/tasks/{task_id}/delivery-objective`. This records intent; it does not accept a task, queue implementation, grant publication permission, or authorize deployment interruption.

Codex can use `workbench_create_delivery_objective` and `workbench_get_delivery_objective` over the existing trusted MCP transport. These tools use the same ledger as HTTP, so a conversation ending does not remove its objective or wakeup.

The JSON body contains `command_id` and `request`. The request declares `requested_endpoints`, `scope`, and `authority`, plus optional budgets, deadlines and verified identities. A GitHub endpoint names `base_branch`, optional `remote`, and explicit `merge`/`release_tag` policy. A deployment endpoint also names its target, health checks and functional checks. Do not put credentials in the request.

Repeating the same command and request returns the same objective. Reusing a command with different content returns HTTP 409. Invalid bodies return HTTP 400, missing tasks return HTTP 404, and unauthenticated creation returns HTTP 401. Existing `GET /api/tasks/{task_id}/delivery-objective` exposes durable stage, next action, wakeup, waits and evidence.

External actions remain subject to the task's frozen authorization or the existing scope-limited `delivery-authorization` API. Describing an authority in an objective is not itself an authorization grant. Only the verifier can accept implementation; a successful worker, Git push, PR or process listing is not end-to-end completion.

Normal pending observations retain one unsettled dispatch and a durable next wakeup. They release the objective lease without consuming failure retries or incrementing the stage attempt. A restarted owner reconciles the same dispatch rather than replaying its external action; unchanged wait observations do not become new progress evidence.

An unfinished objective retains its task worktrees even after verifier acceptance. Objective creation and the final quarantine claim are mutually exclusive ledger transactions: a stale reclamation candidate cannot move an active delivery's files, and a verifier already claimed for quarantine must be restored before a new objective is created. Completion or cancellation releases this protection; ordinary reclaim policy still applies.

A planning validation failure with no materialized task can retry the same reservation under both its original retry limit and the objective budget. The original request hash and user objective remain unchanged; the recorded validation error is supplied as bounded diagnostic context to the next planning attempt. Concurrent retries use the attempt comparison, and a materialized task cannot be recreated. Other failures retain their explicit recovery or decision requirement rather than triggering blind model retries.

GitHub-only objectives require source and build evidence. A project that also requires Node or pnpm attestation declares `github.required_runtime_identities` as a list containing `node` and/or `pnpm`; pure Python publication does not need unrelated toolchains. Deployment objectives retain the full explicitly checked source, build, deployment, runtime, Node and pnpm identity chain.

Declared nonconflicting scopes remain eligible for parallel scheduling. A private-looking path is not execution-time read-only enforcement and cannot waive a read/write conflict. An observed shared writable entity reached through different scope aliases is an additional blocker; an unavailable filesystem observation is never proof of isolation.

## Local authority deployment

Automatic deployment is opt-in through `config.json`'s `local_deployment` object. Schema version `1` requires `target`, an existing Git `source_root`, executable `installer_python`, `installer_arguments`, positive `installer_timeout_seconds`, `observation_timeout_seconds`, `rollback_probe_timeout_seconds`, and `rollback_probe_interval_seconds`. The `health` and `functional` objects each require `name` and an explicit loopback `url`, respectively ending in `/health` and `/api/snapshot`. Configuration does not grant a task deployment permission. Missing or invalid configuration blocks deployment explicitly.

The accepted commit must already exist in the source repository. A dispatch-specific detached staging worktree supplies that exact commit without changing the primary checkout. The installer refuses dirty sources and installs a Git archive, excluding ignored local files. Before replacing files or taking a database snapshot it asks the authority to stop cooperatively, verifies its matching stopped receipt, and acquires its process lock. Writable sidecars must also stop. Failure to establish this safe point leaves installation blocked rather than forcibly terminating workers.

The durable deployment gate prevents new worker and planner claims while deployment and live verification are in progress. A detached helper survives the authority restart; an unknown helper outcome remains indeterminate and is not blindly replayed. Completion requires matching loaded version, manifest commit, HTTP health, functional snapshot, and a newer active coordinator epoch. Rollback after a started replacement must establish another safe point before restoring the database; inability to do so preserves new writes and reports the unresolved failure.
