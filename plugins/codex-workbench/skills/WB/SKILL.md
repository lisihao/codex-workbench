---
name: WB
description: Activate Codex Workbench for the current Codex conversation. Use when the user types wb, invokes $WB, or asks to move a new or existing conversation into the Workbench.
---

# WB

`WB_ACTIVATE_V1`

The plugin hook owns context synchronization. Use only the injected
`WB_SYNC_RECEIPT` as proof that the Workbench accepted the context.

- `active`: route implementation, status, steering, and acceptance through the
  `codex-workbench` MCP tools. Existing conversations continue from their latest
  unfinished request; new conversations wait for a normal user request.
- `degraded`: state clearly that the authority is unreachable and continue only
  in the current MacBook checkout. The hook retries on the next prompt.
- missing receipt: do not claim activation. Tell the user to enable the plugin,
  review its hook with `/hooks`, and type `wb`; `$WB` and `/skills` remain
  supported alternatives.

Do not widen imported scopes, copy secrets, trigger Claude login, spend paid API
quota, or bypass the protected Claude reserve.
