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
- `degraded`: report the receipt's actual error and continue in the current
  MacBook checkout. A rejected context bundle is not proof of a network outage.
  Diagnose the named cause; do not keep asking for `wb` while it is unchanged.
- missing receipt: do not claim activation. Tell the user to enable the plugin,
  review its hook in the installed app's plugin/Hook settings, and type `wb`.
  Use `/hooks` only if that interface actually provides it; a chat message is
  not a trust action. Never write trust records or bypass user approval.
  `$WB` and `/skills` are alternatives where the host supports them.

Do not widen imported scopes, copy secrets, trigger Claude login, spend paid API
quota, or bypass the protected Claude reserve.
