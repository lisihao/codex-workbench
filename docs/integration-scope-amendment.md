# Blocked DSH integration recovery

The fixed `dsh-task-template-integration-v1` profile separates a task's source-file scope from the sandbox permissions needed to write translation records and Agent Notes. Scope permission alone does not make protected metadata writable. Neither operation queues a worker, accepts a result, or transfers recovery ownership.

## Source scope amendment

Use `workbench_control_task` with `action: amend_blocked_integration_scope`. The profile applies to the reviewed DSH integration topology: A, B and C are accepted; D is blocked on its current active, unsealed allocation; E is a pending read-only verifier. It does not accept caller-supplied scope lists or change arbitrary task graphs.

Supply `task_id`, `node_id: D`, `expected_revision`, `expected_attempt`, `expected_contract_hash`, `profile_id`, `reason` and `dry_run: true`. A preview binds the contract, node specifications, accepted ancestor results, allocation and source delta to the proposed additions. It does not write task state, events, artifacts or source files.

The fixed write additions are exactly eight files: the Cordis generator plus its API catalog, the generated client slot catalog, the generated session event inventory, the ACP scenario registration and its two composition files, and the task-template RPC package manifest. README and translation sidecars are not pre-authorized without a concrete changed-file or pairing failure; existing broader D scopes continue to cover documentation already owned by the task. Read-only inputs are separate exact files for the two other generators, affected template/UI/event declarations, and the existing ACP suite/launcher/config. The profile grants no Typert source directory and no translation Git helper. Task-level scope includes only uncovered exact read paths because the planner validates node read and write scopes against the task scope. E receives the same read coverage, not write access. Existing scopes remain intact.

Apply the reviewed preview with `dry_run: false`, its `expected_fingerprint`, and a stable Authority `request_id`. A short transaction checks the durable identities again, records the old and new contract and specifications, and advances the task revision. It preserves attempts, allocation identities, accepted results, model selection, quotas, external-write permissions and acceptance commands. Concurrent validation, a stale preview or a changed task topology rejects the amendment.

This operation does not modify repository files, execute generators or grant filesystem privileges to a Worker. It does not make an earlier out-of-scope change retrospectively compliant. The actual coordinator remains responsible for approving the exact scope preview and subsequent recovery.

A pending verifier can retain the allocation of an earlier rejected attempt. Allocation state is not proof that an executor is running: native verifier settlement clears E's execution fields while preserving its worktree record. A retained same-attempt allocation is eligible only when its allocation, failed-verifier and repair-scheduled events agree, no later E execution invalidates that history, and the native read-only process check proves its source idle. The preview and transaction bind the unchanged allocation and historical evidence; they never delete, reclassify or rewrite them. Unknown activity, conflicting history and quarantine-in-progress remain blocking conditions.

## Restricted metadata writes

The separate [controlled validation entry](controlled-validation.md) owns metadata execution. Its D pairing profile receives an explicit bounded list of Markdown pairs; it does not run `--all`. Its Agent Note profile receives reviewed English and Chinese text and creates only their exact approved paths. Neither profile exposes shell commands, arbitrary environment variables or general writable directories.

Metadata writes change the source fingerprint. After a successful write, read the current task revision and obtain a fresh source-only recovery preview. Do not seal the source allocation before these writes, reuse an earlier source digest, or add an extra queue/resume operation after the authorized recovery. Historical Worker results remain historical evidence; separate project checks and the verifier still own acceptance.

## Delivery boundary

These are Workbench capabilities, not DSH implementation or acceptance evidence. An open PR, a local permission fixture or an installed file does not prove that the existing host can call the new catalog. Deployment acceptance must check the loaded Authority and the actual host tool surface before the original coordinator applies a production preview. This repair does not require the separate connection-lifecycle candidate.
