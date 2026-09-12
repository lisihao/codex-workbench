# One-time workspace lockfile handoff

This operation repairs a prerequisite of an existing blocked task. It does not create a replacement task, accept a worker result, enable automatic recovery, or grant the blocked worker additional write scope.

The task must already authorize `pnpm-lock.yaml`. A preview identifies the existing lockfile owner, the temporary handoff owner, the blocked node and attempt, accepted dependency inputs, and the manifest and lockfile fingerprints. Ambiguous ownership, active conflicting work, or changed inputs reject the operation.

The preview lists up to 32 changed importer paths and reports the complete count and fingerprint. If `changed_importers_truncated` is true, the displayed list is incomplete; inspect the complete source plan before authorizing application.

`workbench_handoff_lockfile` is available through the existing authenticated Authority service. `preview` and `status` are read-only. `apply` requires the preview fingerprint and a stable request ID matching the Authority journal request ID. After an uncertain transport response, query that journal receipt and the handoff status; do not invent a new ID to retry.

Apply reserves temporary ownership and reconstructs a separate repair worktree from accepted inputs and the scope-checked existing source delta. Original worktrees and historical acceptance receipts are not modified. The importer editor supports the repository's generated pnpm lockfile format and adds missing supported workspace links only. Unsupported formats and third-party dependency changes are rejected rather than normalized or upgraded. The fixed offline frozen-lockfile check remains mandatory.

Successful verification atomically associates the new lockfile content artifact with the original task input and returns temporary ownership. It does not queue the node or advance its attempt. The task coordinator separately reviews the receipt and performs the normal continuation. The derived input receipt records the handoff so retry, downstream, and final-verifier worktrees reproduce the same input without attributing the lockfile change to the original worker.

Failure, cancellation, and interrupted-operation reconciliation never publish an unverified input. Separate journal IDs identify reconciliation or cancellation operations targeting the original handoff ID. Releasing a reservation fences a late original runner from committing a ready receipt; it does not claim to terminate a subprocess that may still be writing only its isolated worktree.

The final verifier remains the only task-acceptance authority. A verified lockfile handoff proves that prerequisite repair, not completion of the original task or a new application release.

Ledger schema 15 fences older coordinators that do not recognize temporary lockfile ownership or derived dependency-input receipts. Deployment must use the existing cooperative drain and consistent backup protocol. Rolling back code alone against the new ledger is unsupported; restore only a verified matching backup at the repository's safe point, without discarding subsequent task writes.
