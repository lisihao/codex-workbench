# Codex Workbench development contract

- This repository is independent from DSH, Solar, and AI4Research. Do not copy their state or create a runtime dependency on them.
- The Mac mini SQLite database is the single task-state authority. Clients consume snapshot plus cursor events.
- A worker result is never acceptance. Only the verifier transition may set a task to `accepted`.
- Claude execution is fail-closed when subscription auth or quota is unknown, or any protected pool is at or below 25% remaining.
- API-key fallback is forbidden for subscription-backed Codex and Claude executors.
- One worker owns one Git worktree and one branch. Base SHA and allowed scopes are part of the durable contract.
- Development uses fixture executors. Run at most one minimal real subscription acceptance after the committed release candidate is stable.
- Reuse passing evidence while source, configuration, runtime version, and affected dependency closure are unchanged.
- Apply Adaptive Verification: focused tests while editing; full tests once before release/deployment.

