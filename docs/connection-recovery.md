# Connection recovery without replaying task mutations

The Mac mini Authority remains the only task database and scheduler. MCP is now a protocol adapter to that running service. A local MacBook bridge supervises only the SSH/MCP child it started; it does not launch a second scheduler, create a replacement task, log in to a provider, or restart the Authority.

## Request and reconnect rules

Every mutating service request has a stable `request_id`. The Authority journals it before invoking the existing operation and stores its result after completion. A repeated identical ID returns that receipt; a changed payload or binding under the same ID is rejected. A lost response is not permission to send a new ID. The client queries `workbench_get_service_request` or `service request-status <id>` instead. An interrupted invocation remains `unknown`; it is never automatically replayed.

`workbench_handoff_lockfile` retains its business `request_id` inside the tool arguments. For `apply`, that ID also identifies the Authority write receipt. For `cancel` and `reconcile`, the separate `operation_id` identifies the write receipt while `request_id` continues to select the original handoff. Both the MCP adapter and client bridge use this mapping, including lost-response lookups. `preview` and `status` are read-only operations: they neither consume a write receipt ID nor prevent a later `apply` using the same handoff ID.

Read requests have bounded retries. Reconnection repeats MCP initialize and tools/list, compares server version and catalog fingerprint, and notifies the host when the catalog changes. A changed catalog requires an explicit tools/list refresh before another write. The bridge advertises `listChanged` because it emits that notification; this does not prove every Codex host version will immediately refresh its cached tools. If the host remains stale, restart only that Workbench MCP connection using the host's supported control.

The mixed control, blocked-node validation and acceptance-amendment tools use read-only transport only for the Boolean `dry_run: true`. This includes correcting an invalid preview under the same request ID. See [task control recovery](task-control-recovery.md) for existing sent-record preservation and the distinction between a missing preview receipt and an unknown mutation.

The Authority classifies business operations. The adapter uses that shared classifier, while the HTTP client intersects a caller's read-only hint with the classifier before enabling retries. The standalone bridge also requires a recognized local read-tool ceiling and the catalog annotation; a catalog cannot silently promote an unknown operation to read-only. Mixed preview operations retain their explicit Boolean/operation checks. New unsupported tools may require a compatible client update; this is a permission ceiling, not permission for the client to authorize server-side effects.

Event continuation uses the Authority cursor. A reconnect continues after the last observed cursor and does not duplicate returned events. A caller's explicit historical `after` is preserved. Events without an authoritative cursor are not silently discarded. Session continuation freezes the active task ID and compares it inside the steering transaction, so a concurrent session rebind cannot redirect the message to a different task.

## Installation and private state

The bridge uses buffered binary pipes for JSONL, so a large valid response does not incur a raw file read for every byte. Responses are bounded to 16 MiB per line. An oversized response is reported explicitly and is not retried as a connection failure; use a smaller event page or a recent explicit cursor. This is a transport bound, not permission to omit events or fabricate a successful response. Mutation outcomes still use their original Authority receipts.

`scripts/install-macbook-client.py` installs `workbench-mcp-bridge.py` and `workbench-connection-diagnose.py` under the existing client `libexec/` directory and registers the bridge through the installation's Python interpreter. Both static SSH and location-aware routing retain their destinations and authentication options in private `mcp-bridge.json`, with `-C` compression enabled for the MCP connection to reduce large JSON transfer time on slow links. Global SSH settings, service preflight, request deadlines, tunnels and the home-presence heartbeat are unchanged.

The installer snapshots and can roll back its managed scripts and configuration. It never replaces or rolls back `mcp-bridge-state.json`: live request IDs and event cursors are data, not installation artifacts. Private config, state and scripts have mode 0600. Stderr evidence is bounded and fingerprinted rather than exposing credentials in diagnostic output.

A missing bridge state file initializes a new record. A present but unreadable or malformed state file is preserved and reported as an error before starting a child; it is never silently reset to empty request or cursor history.

Upgrade the Authority before installing the new client adapter: it requires the authenticated `/api/service/*` endpoints and request journal. Before local installation writes, a 20-second preflight requires `service status` to report `ok: true` and `service_protocol: workbench-authority-service/v1`; an executable old CLI is not sufficient. Updating these repository files alone does not update an already installed MacBook client or a long-lived MCP child.

Authority shutdown stops accepting new service mutations and waits for admitted mutations to settle before releasing its coordinator lease and recording `coordinator.stopped`. A draining service returns an explicit 409 for a new mutation. Read-only status and request receipts remain readable while the HTTP listener is available; a forced process termination still produces an unknown, non-replayed receipt on recovery.

## Bounded diagnosis and fallback

`workbench_harness_health` adds `connection_evidence` using `workbench-connection-evidence/v1`. The Authority reports its responding service instance, loaded package version, protocol and capability digest. A supporting adapter adds its own process instance, PID and loaded version; a supporting bridge adds its process instance, PID, implementation protocol and source digest captured once at import. The bridge does not read or hash its installed source on every call. An installed release receipt remains separate evidence, and a legacy component is unknown rather than inferred from another component's version.

Host evidence records receipt of the existing health tool call and, separately, the last tools/list request made by that host. Internal reconnection catalog fetches are not host refreshes. UI catalog availability remains unknown: neither a health call nor list_changed proves that the host exposes every current tool. Missing restart, recovery-duration, duplicate-effect, orphan-process and data-loss measurements remain null. Diagnostics do not clear or rewrite write identities, authorize operations, or turn a failed health response into success. See the staged [connection lifecycle repair plan](connection-lifecycle-plan.md).

Use the installed Python interpreter to run `libexec/workbench-connection-diagnose.py --config <client-root>/mcp-bridge.json`. `--status-only` reads local state without a remote probe. The normal diagnostic probes only the recognized transport's `service status` endpoint; authentication or host-key failures do not trigger login retries. Missing child exit/stderr evidence is reported as unknown, not guessed from a running tunnel process.

The diagnostic separates configured transport, SSH/local child, Authority HTTP, and MCP child. A 3-second snapshot timeout is not sufficient evidence of a dead Authority: the task snapshot can be much slower than the lightweight service status endpoint. Workbench MCP and Codex's separate remote AppServer channel are different connections.

Where the current task's rules authorize a controlled fallback, the remote `codex-workbench service status`, `service tools`, `service request-status <id>` and `service invoke` commands use the same authenticated Authority endpoint. `service invoke` takes the exact `{request_id, tool, arguments, task_id?, session_id?}` JSON envelope on stdin. These commands do not initialize or open a task database. Legacy administrative CLI commands remain distinct; this change does not migrate every historical command to HTTP or override a task that explicitly requires MCP-only operation.

No fallback authorizes arbitrary shell work on a task, a new worker attempt, model probing, global configuration changes, or restarting unrelated processes. Fixed project validation uses the separately documented [controlled validation entry](controlled-validation.md).

## Evidence coverage

`tests/test_connection_service_e2e.py` exercises the real bridge, CLI MCP child, loopback HTTP Authority and fixture SQLite ledger. It covers reconnecting after an Authority restart, losing a response after a mutation committed while reconnecting without a second POST, and rejecting a session-rebind race. Accepted ancestors, attempt numbers and code artifacts remain unchanged. Unit fixtures cover bounded transport failures, catalog changes, cursor continuation, idempotent receipts, installer rollback and diagnostic redaction. These keyless tests are implementation evidence, not a claim that a deployed host connection has been upgraded.

The compatible-service-change fixture additionally holds the bridge and adapter process instances and PIDs constant while recreating the Authority and reading a changed business result through the existing health tool. It verifies a changed Authority instance and unchanged durable task. This is an isolated compatibility demonstration, not a measurement of the user's Codex UI, a production restart count, or a guarantee for changed tool schemas and incompatible upgrades.

`tests/test_handoff_service_mcp.py` runs lockfile handoff operations through that same bridge, CLI MCP child and HTTP Authority path. Its isolated Git/SQLite fixture checks read-only preview/status, a single overlay materialization across repeated apply requests, and separate cancel/reconcile receipts. The package-manager materializer is a deterministic fixture; this test covers MCP delivery and idempotency, not a deployed pnpm installation or a production DSH handoff.
