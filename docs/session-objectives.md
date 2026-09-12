# Session objective inventory

`workbench_get_session` preserves its existing durable session-binding response and adds a bounded, read-only `objectives` page. The page makes earlier unfinished work visible after a Codex thread has bound a newer active task.

The request still requires `source_thread_id`. Optional `objective_limit` is an integer from 1 through 50 and defaults to 20. Optional `objective_cursor` is opaque: omit it for the first page, then pass `next_objective_cursor` unchanged. A decimal value continues after a permanent route ledger row. The value `active` only continues after a current active task that has not yet received a permanent route.

Each entry contains only `task_id`, `state`, `state_revision`, `active`, and a nullable `delivery_objective` with `objective_id`, `stage`, and `state`. It never returns prompts, task steering, context excerpts, imported transcript text, worker results, artifact references, or notification payloads.

The inventory reads permanent `session_notification_routes` plus the current session binding. Routed entries keep their route order if the active pointer changes between pages; the active flag is a projection, not a sort key. The active task is included even before a route event is projected. Reading does not bind a task, backfill a route or notification, mutate a task or delivery objective, wake a coordinator, or create a scheduler.

A task is omitted only when its actual task state is `accepted` or `cancelled` and it has no delivery objective still outside `complete` or `cancelled`. Therefore an accepted implementation task remains visible while its attached delivery objective is `active`, `waiting`, or `needs_decision`. A blocked task remains visible because it is not completed or cancelled.

This is an inventory surface only. It does not transfer durable responsibility, replace delivery lifecycle ownership, or claim full P0 completion.
