# ADR-0016 — fold children into the hub thread

Status: accepted (2026-10-22) · opt-in via `fold_children`; default preserves ADR-0005/0012 behavior

## Context

The default is one thread per task. When a task is decomposed, every child
gets its own thread, and the epic's thread only receives a `depends on:`
rollup plus a best-effort "prerequisite opened" link-reply (ADR-0012). An
operator watching a decomposition wants every child's update in the epic's
thread — the hub the design already treats as the single source of the
story.

Link direction (verified against `kanban_db_graph.decompose_triage_task`):
each child is the `parent_id` (prerequisite), the root is the `child_id`
(dependent). The root waits on all children.

## Decision

Opt-in setting `fold_children` (default `false`). When on, a task that is a
prerequisite publishes its events into the thread of its **single topmost
dependent** (the epic) instead of opening its own thread.

Routing rule (`Consumer._fold_target`):

- `fold_children` off, or the task gates nothing → own thread (no change).
- BFS over `task_links` dependents (`child_id`) collecting the topmost nodes
  (those gating nothing); exactly one topmost T that is not the task itself
  → fold into T. Transitive: multi-level decompositions land in the grand
  epic's thread.
- Multiple topmost dependents (shared fan-in) or a cycle → own thread.
  Duplicating one reply into several threads is worse than a split hub.
- The topmost must still exist in `tasks`; a deleted hub falls back to the
  task's own thread (otherwise the child would hold the board cursor
  forever — the existing stuck-task failure mode).

State shape (ADR-0005, `posts` table):

- New column `owner_task_id TEXT` (NULL = self-owned, legacy rows unchanged;
  added in place via the existing `_migrate()` pattern).
- A folded child keeps its own `posts` row — the deduping watermark
  (`last_event_id`), per-task 429 backoff, and tombstone/dead-letter
  semantics all stay per task. The row points at the hub: `thread_id` and
  `destination` copied from the hub row, `message_id` = the hub's card
  (replies anchor on it; the child never edits it), `owner_task_id` = hub.
- The folded row never gets a `last_status_key`, so the stale sweep and the
  metadata audit are naturally hub-owned only. The audit's thread-keyed map
  (`_audit_discord_metadata`) is built from owner rows only
  (`store.owner_posts`) — a folded row sharing the hub's `thread_id` would
  otherwise collide in that dict.

Consumer behavior, per folded task:

- **Attach** (`_process_task` create branch): hub row live with a thread →
  insert the folded row, `ref` = hub's thread/card, skip card rendering and
  `open_thread` entirely. Hub not published yet (or its create outcome
  unknown) → **hold**: return the current position so the events re-scan next
  pass. Real case: in a decompose, the children's `created` events have lower
  ids than the root's `decomposed` event, so a child can be processed first.
  Hub tombstoned/dead-lettered → the child attaches in the hub's terminal
  state and stops publishing (one human/operator act, one verdict).
- **Replies**: posted to the hub thread, prefixed with the child's label
  (`▸ title · id`) so several children in one thread stay attributable. The
  `[#]` anchor already deep-links to the child's own task page
  (`_with_anchor` runs on the child's id).
- **Card/maintenance**: the hub's card is the only card in the thread; tag,
  name, and archive follow the hub's status, not the children's. A child
  completing does not re-tag or archive the epic's thread. Progress on a
  child dirties the hub's card through the existing dependent-dirty path,
  and the dirty-repaint vehicle refreshes the `depends on:` rollup — in the
  same pass when the hub had no events of its own, deferred one pass when it
  did (the vehicle skips in-batch tasks; the card opened in that pass was
  rendered from the current state anyway).
- **Failure policy** (ADR-0007) is unchanged per task: 429 backs off the
  child, 404/10003 tombstones the child (the hub row tombstones separately
  when its own publish 404s), 5xx retries with the card-dirty vehicle
  redirected to the owner.
- `_announce_to_dependents` skips dependents folded into this thread (a link
  reply to itself) and keeps announcing into genuinely separate dependent
  threads.
- `_repaint` clears and skips folded rows (they have no card).

Consequences:

- Already-opened child threads stay as-is: existing `posts` rows persist
  (terminal is not final), and a task published before the flag was enabled
  keeps its own thread. Links changed after a publish do not re-route it —
  the mapping is decided once, at attach time.
- The hub thread's status tag reflects the epic's own state (it sits in
  `todo` while children run). A composite tag ("all children done") is a
  deliberately separate, later step — it needs a new status-key input, not a
  routing change.
- Wiring: `plugin.yaml` `config_schema.fold_children` →
  `ctx.get_config("fold_children")` in `_build_consumer` →
  `Consumer(fold_children=…)`; stored under
  `plugins.entries.kanban-task-threads.settings`.

## Rejected

- Folding by default: a behavior change in a published plugin; the
  one-thread-per-task invariant is load-bearing for operators who rely on
  per-task search in the forum.
- Posting a folded child's reply into every dependent's thread (fan-out):
  shared prerequisites would multiply the log N-fold.
- Reusing the hub row for the child (no child row at all): loses per-child
  watermark/backoff and makes the child's events invisible to the per-task
  low-water mark arithmetic.
