# Codex Workbench plugin

This plugin adds an explicit `wb` activation to Codex.  When a conversation is
bound, it sends a bounded, redacted context bundle to the `codex-workbench` MCP
server already configured on this computer.  It does not create a remote server
or silently enable synchronization for unrelated conversations.

## Prerequisites

- Codex CLI with plugin support.
- A configured `codex-workbench` MCP server, normally created by
  `scripts/install-macbook-client.py` after the persistent authority is
  installed.
- A trusted Workbench authority: activation can transmit the current
  conversation's normalized text, Git diff, untracked repository files, and
  explicitly referenced non-secret files to that endpoint.

## Install from a repository checkout

```sh
codex plugin marketplace add /absolute/path/to/codex-workbench
codex plugin add codex-workbench@codex-workbench
```

For the public repository, replace the first command with:

```sh
codex plugin marketplace add lisihao/codex-workbench --ref main
codex plugin add codex-workbench@codex-workbench
```

Codex asks you to review and approve the `UserPromptSubmit` hook.  Do that in
`/hooks` before activating a session.

## Upgrade without invalidating older Hook paths

Do not upgrade an installed copy with bare `codex plugin remove` or `codex
plugin add`. From a checkout pinned to the intended release, first run the
read-only inspection:

```sh
./scripts/upgrade-codex-plugin.py \
  --marketplace codex-workbench \
  --plugin codex-workbench
```

Save other work and fully stop Codex App and interactive Codex hosts using the
plugin. Then run the same checked-out script from a normal terminal:

```sh
./scripts/upgrade-codex-plugin.py \
  --marketplace codex-workbench \
  --plugin codex-workbench \
  --apply \
  --confirm-host-stopped \
  --confirm-active-hook-cache-retention
```

The installer snapshots and synchronizes every old cache version before native
installation, restores any deleted old paths, and independently verifies the
new installed version. It never runs the Hook, edits trust state, or prunes old
caches. Native installation still creates a short delete-to-restore window, so
this is an offline maintenance flow, not a zero-downtime hot upgrade. After
reopening Codex, a new message must traverse the new Hook and create a new
remote event; an older `active` receipt is not acceptance. Full recovery and
trust instructions are in `docs/AI_INSTALL.md` in the same pinned checkout.
The automated gate covers a killed upgrade process, not a physical power-loss
test or a long-lived host hot-reload claim. Pre-publish staging residue is
reported and blocks another upgrade; it is never silently deleted.

## What the v1.8.2 authority adds

The plugin remains a thin session-binding entry point; scheduling and evidence
stay on the configured Mac mini authority. The authority now provides:

- a benchmark-backed, domain-specific cold-start prior plus a long-lived
  SQLite runtime ledger and pinned performance snapshots;
- quality-gated routing that uses conservative benchmark/runtime posterior
  lower bounds only after role, tool, evidence, quota, and capacity gates;
- a dedicated logical Spark lane with planner-side decomposition checks and
  observable queue/utilization metrics;
- a recoverable worktree lifecycle that quarantines first, requires a fully
  verified NAS recovery capsule before deletion, and forces remote capsule
  transfers through the installed Tailscale profile.

Public benchmark results are transfer priors, not a unified leaderboard or a
claim about local success rates. The current calibration interface is
advisory-only and reports `cold-start` or `ok`; it does not yet implement a
`baseline`/`shadow`/`calibrated` promotion lifecycle. Codex/Spark remaining
quota is `N/A` when no provider balance is observable.

## Use

Type `wb` in a Codex conversation to activate the Workbench. You may continue
the request after whitespace or common punctuation, for example `wb, status`
or `wb，检查状态`. `$WB` and the `WB` entry in `/skills` are also supported.
Existing conversations import their
normalized history, current Git patch, untracked files, and explicitly mentioned
files once before planning begins.

The hook is inert until activation.  An active binding reuses its durable receipt
on later prompts.  If the authority cannot be reached, it records a degraded
receipt and keeps execution in the current MacBook checkout; the next prompt
retries synchronization.

Some Codex versions reject custom top-level slash commands before hooks run, so
`wb` is the portable shortcut; `/WB` is not required.

The same activation works in a Codex conversation reached through the native
mobile Remote page when Remote Control is enabled on the Mac mini. Run
`codex-workbench mobile status`, inspect `mobile enable --dry-run`, then use
`mobile pair` to obtain the attended native pairing command. The plugin never
captures or stores the short-lived pairing code.
