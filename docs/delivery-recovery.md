# GitHub delivery recovery

GitHub delivery is a continuation of an already `accepted` task; it never
changes task or node acceptance. The verifier remains the only transition that
can set a task to `accepted`, and a new delivery receipt is created only when
the accepted contract authorizes external GitHub writes.

## Durable identity and ownership

The SQLite delivery receipt freezes the complete request under its
`command_id`, including task, base branch, remote, merge option, and release
tag. Reusing the ID with any changed request is rejected. Once preparation has
completed, the receipt also freezes the integration branch and commit.

One short-lived SQLite `delivery_lease` owns a command while Git/GitHub work is
outside the database transaction. Every receipt advance supplies both that
opaque fence and the expected prior state. A second concurrent invocation is
rejected, and an old owner cannot write after a replacement fence is acquired.
An expired lease may be recovered, but the recovery starts with read-only
reconciliation rather than a replay of an external write.

## Push and PR recovery

Before the first push, delivery probes the exact remote ref
`refs/heads/codex-workbench/integration/<task>`. It pushes only after the ref
is conclusively absent and the verifier worktree `HEAD` still equals the
receipt commit. A matching existing ref is recorded as reconciled; a divergent
ref is a terminal conflict.

`push_started` is persisted before `git push`. If the process times out,
crashes, or loses the result after that point, a retry probes the remote ref:

- matching commit: continue as `pushed`;
- absent, divergent, or unqueryable ref: keep the result indeterminate or fail
  on a proven conflict; do not issue another push.

After a confirmed push, PR lookup has three outcomes: present, confirmed
absent, or unknown. Authentication failures such as GitHub HTTP 401, rate
limits, and network failures are unknown—not absence—and therefore never lead
to `gh pr create`. A later exact retry verifies the same remote branch and
looks up the PR again. If it is absent with a confirmed response, it creates
one PR; if it already exists, it reuses its URL.

`pr_create_started` is likewise persisted before creation. A create with an
unknown outcome is reconciled by lookup and is never blindly repeated. Merge
and release use the same pre-effect intent and read-back pattern.

`merged` is terminal only for a frozen request with no `release_tag`. For a
request that includes a release tag, a durable merged receipt resumes at the
release probe after interruption; it is not allowed to skip the requested
release or create a second one after an unknown release result.

Legacy failed receipts that contain durable successful push evidence can be
reconciled as `pushed`. A receipt that failed before a push has no such proof
and remains terminal; repeating its command does not turn it into a new push.

## Operator behavior

Retry using the exact original delivery command ID and request. Inspect the
receipt and its cursor events when it remains `failed` or `indeterminate`.
Do not change a command ID's request to force a new branch, commit, or PR; use
an explicitly authorized new delivery action only after resolving the recorded
source conflict.
