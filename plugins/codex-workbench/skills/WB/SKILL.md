---
name: WB
description: Activate Codex Workbench for the current Codex conversation. Use when the user types wb, invokes $WB, or asks to move a new or existing conversation into the Workbench.
---

# WB

`WB_ACTIVATE_V1`

The plugin hook owns context synchronization. Use only the injected
`WB_SYNC_RECEIPT` as proof that the Workbench accepted the context.

- `active`: use the same controlled Workbench Authority service for task changes;
  prefer `codex-workbench` MCP tools as the model adapter. Preserve the bound task,
  accepted nodes, revision, scopes and approvals. Existing conversations continue
  their unfinished request; do not recreate work because a connection failed.
- `degraded`: report the actual component error. A rejected context bundle or
  `Transport closed` does not prove a network outage, expired login or dead Authority.
  Use the installed `workbench-connection-diagnose.py` with the existing private
  connection config for bounded SSH/read-only diagnosis. Missing child exit/stderr
  is unknown evidence, not proof of any cause. Do not poll with scheduled model turns.
- missing receipt: do not claim activation. Tell the user to enable the plugin,
  review its hook in the installed app's plugin/Hook settings, and type `wb`.
  Use `/hooks` only if that interface actually provides it; a chat message is
  not a trust action. Never write trust records or bypass user approval.
  `$WB` and `/skills` are alternatives where the host supports them.

If MCP is unavailable, an already-authorized operator may use the existing SSH
transport or protected tunnel and `codex-workbench service invoke` to call the
same authenticated Authority endpoint. Preserve the original task/session binding,
`expected_revision` and stable `request_id`; this is not a second scheduler or a
direct-SQLite fallback. After a lost write receipt, query `service request-status`
with the same ID. Unknown outcomes prohibit automatic resend or a new ID.

The deterministic MCP bridge owns bounded reconnect, initialization, tool/version
checks and event cursor replay. A `tools/list_changed` notification is only a hint;
if the host retains an old catalog, refresh/restart that MCP connection using its
supported controls. Do not default to restarting the whole app or Authority.
Diagnostic failure alone grants no production restart, deployment or worker kill.

These are Workbench-owned workflow rules, not overrides of Codex/Claude host
policy or higher-priority instructions. Local implementation fallback requires an
explicit decision after checking the bound remote objective; do not duplicate it.

Do not widen imported scopes, copy secrets, trigger Claude login, spend paid API
quota, or bypass the protected Claude reserve.
