# Connection recovery without replaying task mutations

The Mac mini Authority remains the only task database and scheduler. MCP is now a protocol adapter to that running service. A local MacBook bridge supervises only the SSH/MCP child it started; it does not launch a second scheduler, create a replacement task, log in to a provider, or restart the Authority.

## Request and reconnect rules

Every mutating service request has a stable `request_id`. The Authority journals it before invoking the existing operation and stores its result after completion. A repeated identical ID returns that receipt; a changed payload or binding under the same ID is rejected. A lost response is not permission to send a new ID. The client queries `workbench_get_service_request` or `service request-status <id>` instead. An interrupted invocation remains `unknown`; it is never automatically replayed.

`workbench_handoff_lockfile` retains its business `request_id` inside the tool arguments. For `apply`, that ID also identifies the Authority write receipt. For `cancel` and `reconcile`, the separate `operation_id` identifies the write receipt while `request_id` continues to select the original handoff. Both the MCP adapter and client bridge use this mapping, including lost-response lookups. `preview` and `status` are read-only operations: they neither consume a write receipt ID nor prevent a later `apply` using the same handoff ID.

Read requests have bounded retries. Reconnection repeats MCP initialize and tools/list, compares server version and catalog fingerprint, and notifies the host when the catalog changes. A changed catalog requires an explicit tools/list refresh before another write. The bridge advertises `listChanged` because it emits that notification; this does not prove every Codex host version will immediately refresh its cached tools. If the host remains stale, restart only that Workbench MCP connection using the host's supported control.

The mixed control, blocked-node validation and acceptance-amendment tools use read-only transport only for the Boolean `dry_run: true`. This includes correcting an invalid preview under the same request ID. See [task control recovery](task-control-recovery.md) for existing sent-record preservation and the distinction between a missing preview receipt and an unknown mutation.

Event continuation uses the Authority cursor. A reconnect continues after the last observed cursor and does not duplicate returned events. A caller's explicit historical `after` is preserved. Events without an authoritative cursor are not silently discarded. Session continuation freezes the active task ID and compares it inside the steering transaction, so a concurrent session rebind cannot redirect the message to a different task.

## Installation and private state

The bridge uses buffered binary pipes for JSONL, so a large valid response does not incur a raw file read for every byte. Responses are bounded to 16 MiB per line. An oversized response is reported explicitly and is not retried as a connection failure; use a smaller event page or a recent explicit cursor. This is a transport bound, not permission to omit events or fabricate a successful response. Mutation outcomes still use their original Authority receipts.

`scripts/install-macbook-client.py` installs `workbench-mcp-bridge.py` and `workbench-connection-diagnose.py` under the existing client `libexec/` directory and registers the bridge through the installation's Python interpreter. Both static SSH and location-aware routing retain their destinations and authentication options in private `mcp-bridge.json`, with `-C` compression enabled for the MCP connection to reduce large JSON transfer time on slow links. Global SSH settings, service preflight, request deadlines, tunnels and the home-presence heartbeat are unchanged.

The installer snapshots and can roll back its managed scripts and configuration. It never replaces or rolls back `mcp-bridge-state.json`: live request IDs and event cursors are data, not installation artifacts. Private config, state and scripts have mode 0600. Stderr evidence is bounded and fingerprinted rather than exposing credentials in diagnostic output.

A missing bridge state file initializes a new record. A present but unreadable or malformed state file is preserved and reported as an error before starting a child; it is never silently reset to empty request or cursor history.

Upgrade the Authority before installing the new client adapter: it requires the authenticated `/api/service/*` endpoints and request journal. Before local installation writes, a 20-second preflight requires `service status` to report `ok: true` and `service_protocol: workbench-authority-service/v1`; an executable old CLI is not sufficient. Updating these repository files alone does not update an already installed MacBook client or a long-lived MCP child.

Authority shutdown stops accepting new service mutations and waits for admitted mutations to settle before releasing its coordinator lease and recording `coordinator.stopped`. A draining service returns an explicit 409 for a new mutation. Read-only status and request receipts remain readable while the HTTP listener is available; a forced process termination still produces an unknown, non-replayed receipt on recovery.

## Bounded diagnosis and fallback

Use the installed Python interpreter to run `libexec/workbench-connection-diagnose.py --config <client-root>/mcp-bridge.json`. `--status-only` reads local state without a remote probe. The normal diagnostic probes only the recognized transport's `service status` endpoint; authentication or host-key failures do not trigger login retries. Missing child exit/stderr evidence is reported as unknown, not guessed from a running tunnel process.

The diagnostic separates configured transport, SSH/local child, Authority HTTP, and MCP child. A 3-second snapshot timeout is not sufficient evidence of a dead Authority: the task snapshot can be much slower than the lightweight service status endpoint. Workbench MCP and Codex's separate remote AppServer channel are different connections.

Where the current task's rules authorize a controlled fallback, the remote `codex-workbench service status`, `service tools`, `service request-status <id>` and `service invoke` commands use the same authenticated Authority endpoint. `service invoke` takes the exact `{request_id, tool, arguments, task_id?, session_id?}` JSON envelope on stdin. These commands do not initialize or open a task database. Legacy administrative CLI commands remain distinct; this change does not migrate every historical command to HTTP or override a task that explicitly requires MCP-only operation.

No fallback authorizes arbitrary shell work on a task, a new worker attempt, model probing, global configuration changes, or restarting unrelated processes. Fixed project validation uses the separately documented [controlled validation entry](controlled-validation.md).

## Evidence coverage

`tests/test_connection_service_e2e.py` exercises the real bridge, CLI MCP child, loopback HTTP Authority and fixture SQLite ledger. It covers reconnecting after an Authority restart, losing a response after a mutation committed while reconnecting without a second POST, and rejecting a session-rebind race. Accepted ancestors, attempt numbers and code artifacts remain unchanged. Unit fixtures cover bounded transport failures, catalog changes, cursor continuation, idempotent receipts, installer rollback and diagnostic redaction. These keyless tests are implementation evidence, not a claim that a deployed host connection has been upgraded.

`tests/test_handoff_service_mcp.py` runs lockfile handoff operations through that same bridge, CLI MCP child and HTTP Authority path. Its isolated Git/SQLite fixture checks read-only preview/status, a single overlay materialization across repeated apply requests, and separate cancel/reconcile receipts. The package-manager materializer is a deterministic fixture; this test covers MCP delivery and idempotency, not a deployed pnpm installation or a production DSH handoff.
