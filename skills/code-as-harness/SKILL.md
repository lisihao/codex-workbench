---
name: code-as-harness
description: "Use when choosing proportional tests or executable governance for a code change, investigating repeated workflow friction, or preparing release, deployment, migration, security-sensitive, or governed completion evidence."
metadata:
  codex_workbench_managed: "true"
  profile: "code-as-harness/v1"
  artifact_kind: "workbench-canonical-compatible-skill"
---

# Code as Harness

Choose the smallest executable evidence gate that can falsify the requested completion claim. Keep ordinary changes fast, while making recurring workflow failures and high-risk delivery paths durable and observable.

## Classify once

Use the highest matching tier:

| Tier | Typical scope | Required evidence |
|---|---|---|
| `L0` | read-only analysis; non-executable wording/docs-only change with no contract impact | inspect the relevant source or diff |
| `L1` | localized implementation affecting one owner or behavior path | one focused check that exercises the changed behavior |
| `L2` | multi-file feature; public API/format; shared module; cross-component fix | affected tests plus relevant type/lint/build or quick governance |
| `L3` | release/deploy; migration; security; persistence/schema; governance-engine change; DSH Desktop delivery | project-mandated full gate once the tree is stable, then required attestation/runtime evidence |

Project and higher-priority rules can raise the tier. Never lower an explicit delivery protocol.

## Establish the acceptance boundary

- State the requested behavior and one observable success condition before editing.
- For L2/L3, inspect the actual owner, repository contract, and native commands.
- Diagnose a repeated issue only after evidence shows the same failure pattern. Wording such as “again” or “every time” is a prompt to inspect history, not proof by itself.
- Classify confirmed recurrence as an implementation harness gap, a platform limitation needing research, an execution miss under an existing rule, or a reusable capability.
- Prefer a code-level harness fix such as a gate, hook, workflow, or focused test when it directly prevents the confirmed recurrence.

## Operating contract

- Fill all safe independent work slots when at least two real DAG nodes are ready. Give each worker explicit ownership, inputs, dependencies, and exit evidence.
- Do not parallelize competing writes to the same files, schema, generated artifact, Git state, installed app, release, or deployment target.
- Do not invent speculative work merely to occupy capacity.
- A later user message continues the active objective by default. Preserve its objective and scope unless the user explicitly pauses, cancels, or replaces it.

## Reuse evidence safely

- During development, run a check only when its failure would change the next action.
- Reuse passing evidence while its complete fingerprint is unchanged: source and base identity, configuration, dependency closure, runtime/platform, relevant scopes and steering, governance profile, command, and verification tier.
- After a change, rerun affected checks only. A Git-only commit does not invalidate evidence unless the repository binds evidence to HEAD.
- A matching L3 fingerprint has one full gate. Do not repeat it for reassurance.
- Run final full governance only once after the worktree is stable and the delivery boundary requires it.

## Scope and completion

Workers report changed paths and evidence; dispatch is not completion. The coordinator accepts only when the declared verifier evidence covers the task contract. Do not broaden scopes, silently cancel a durable active task, turn an error into apparent success, or claim external plugin/model execution without observed evidence.

Report tier, observable acceptance, evidence, covered scope, uncovered scope, residual risk, and full-gate status. Partial evidence is `warn` or `pending`, never invented `ok`.

Read [references/aegis-integration.md](references/aegis-integration.md) when deciding whether to add a new harness mechanism rather than reuse an existing one. Read [references/tier-examples.md](references/tier-examples.md) when the verification tier is ambiguous.

## Compatibility boundary

This is the Workbench-managed compatible Code-as-Harness capability. It preserves the proportional Aegis-derived workflow and adds Workbench durable objectives, safe parallelism, evidence fingerprints, and acceptance semantics. It does not claim compatibility with platform-specific third-party UI or thread-routing features that Workbench cannot execute and verify.
