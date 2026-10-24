# ADR-0017 — announce decompositions in the hub thread

Status: accepted (2026-10-24) · follow-up to ADR-0016

## Context

ADR-0016 (`fold_children`) verified live on board `prometna-urnik` (hub
`t_8ee1feb8`, children `t_bba2dfbb`, `t_d5bdb6b0`): the fold plumbing works —
both children's `posts` rows point at the hub's thread with
`owner_task_id` set, and a child's `completed` reply landed in the hub thread
with the `▸` prefix. What the hub thread *lacked* was any record of the
decomposition itself. The thread contained only the card, re-rendered with its
`depends on: 2 ready` rollup.

Three independent reasons the decomposition is currently silent:

1. The board writes a `decomposed` event on the **root** task at decompose
   time (payload `{"child_ids": [...], "root_assignee": ...}` — verified in
   `task_events`), but `decomposed` is not in `DEFAULT_REPLY_ON`, so it earns
   a card refresh only.
2. `render_reply` has no built-in template for it — even if an operator adds
   it to `reply_on`, the reply falls back to the generic line
   `• decomposed`.
3. With `fold_children` on, the per-child "🧵 prerequisite opened" link-reply
   (ADR-0012's `_announce_to_dependents`) never fires either: it posts when a
   child's **own thread opens**, and a folded child never opens a thread.
   With the flag off it partially compensates (one link reply per child
   thread), but it is a link to a thread, not a summary of the split.

An operator reading the hub thread wants it to answer *"what happened to this
task?"* — including "it split into these N tasks". The card's `depends on:`
line is a live snapshot rewritten in place (ADR-0001); the append-only log
is where the topology change belongs.

## Decision

`decomposed` becomes a reply-earning kind, and its reply lists the children.

- **Trigger:** the root task's own `decomposed` event — the one the board
  already writes, with `child_ids` in the payload. No new board event, no
  hook, no change to the decompose path. Log-only consumption (ADR-0002)
  already sees it.
- **Routing:** none needed. The event belongs to the root task, which always
  self-publishes into its own thread (the hub). Identical outcome with
  `fold_children` on or off; with the flag on this reply is the *only*
  decomposition record in the thread, with the flag off it complements the
  per-child link replies.
- **Child list is pre-composed by the consumer**, not templated from the raw
  payload: the formatter is flat-field-only (ADR-0006) and `child_ids` is a
  list, not a scalar. For each id the consumer looks up the task row and
  renders one line `▸ {title} · {id}` — title truncated to fit, id always
  kept (the stable-key invariant of ADR-0009), a deleted child degrades to
  id-only — then sets `payload["children"]` and `payload["child_count"]`
  before `render_reply`. The lookup pattern mirrors `commented`'s excerpt
  recovery: the event payload is not the only source of truth.
- **Legacy/absent payload:** if the event carries no `child_ids` (older rows
  or a future writer), the consumer falls back to
  `SELECT parent_id FROM task_links WHERE child_id = ?` — children sit on the
  `parent_id` side of the link (ADR-0012's arrow direction).
- **Template** (built-in, operator-overridable via the existing
  `reply_templates` setting):

  ```
  "decomposed": "🧩 decomposed into {child_count}:\n{children}"
  ```

  with `child_count` carrying the plural (`"2 tasks"` / `"1 task"`) so the
  line reads correctly in the singular too. A custom template referencing a
  field the consumer did not pre-set falls back to the generic line
  (ADR-0006's rule) — never a 400.
- **Default set:** `decomposed` is added to `DEFAULT_REPLY_ON`. Rationale:
  it is a topology change, not a status flap — single-digit occurrences per
  month on a live board versus thousands of heartbeats — and high-signal by
  definition (it names every new task). `reply_on` remains the operator's
  dial; dropping `decomposed` from it restores today's silence.
- **Signature:** unsigned (board voice), like `crashed`/`timed_out` —
  decomposition is a board mutation, and `root_assignee` in the payload is
  the root's assignee, not the decomposer. `reply_username` unchanged.
- **Avatar:** `message_type="decomposed"` is an unknown kind to the avatar
  catalog → `default` (the existing fallback). A dedicated glyph is a v2
  catalog concern (ADR-0015), out of scope here.
- **Anchor:** the trailing `[#]` deep-links to the root task's page — the
  consumer already passes the owning task's id, and the event's task is the
  root.
- **Truncation:** the `▸` block flows through the existing
  `_with_anchor`/`truncate` path; a long decomposition list keeps the head,
  loses the tail, marks the cut — deterministic (ADR-0007).

## Consequences

- The hub thread gains exactly one reply per decomposition, before the
  children's lifecycle replies (`▸ … completed` and friends, ADR-0016) —
  the thread reads as: card → "decomposed into 2 tasks: ▸ …, ▸ …" →
  per-child updates. That is the intended story order.
- No new setting, no new plugin.yaml key, no store/schema change.
- No backfill: events already past the durable cursor (ADR-0002) are not
  re-published. Threads for tasks decomposed before the change — including
  `t_8ee1feb8` — stay silent retroactively, consistent with ADR-0016's
  "the mapping is decided once" posture.

## Rejected

- **Announce per child, on the child's `created` event** (its payload carries
  `from_decompose_of`): N replies instead of one, each needing the same
  title/id composition, and none of them sees the sibling set. The single
  root-side event already carries the complete list.
- **Fold-mode special case** (announce only when `fold_children` is on):
  makes the same event mean two things depending on a flag — a reader of one
  board cannot predict the other's threads. One behavior, one rule.
- **Put the child list in the card:** the card is a snapshot rewritten in
  place; an append-only record belongs in the log (the ADR-0001 split).
  The card already carries the `depends on:` rollup, which covers state.
- **A hook from the decompose code:** a transport the design deliberately
  avoids (ADR-0002); the event log already carries the fact.
- **A 13th avatar in v1:** the catalog is a versioned asset bundle; the
  `default` fallback renders fine, and a glyph ships with the next catalog
  move, not with this.
- **Announcing `linked`** (manual `kanban_link`): an incremental topology
  edit, not a decomposition; a "prerequisite added" reply is a separate,
  later decision if the noise turns out to be wanted.

## Wiring (implementation)

- `kanban_task_threads/consumer.py`: `DEFAULT_REPLY_ON += ("decomposed",)`;
  in the reply loop, when `kind == "decomposed"`, pre-compose
  `children`/`child_count` (payload `child_ids` → `tasks` lookup;
  `task_links` fallback) before `render_reply`.
- `kanban_task_threads/render.py`:
  `DEFAULT_REPLY_TEMPLATES["decomposed"]` as above.
- `tests/test_decompose_reply.py` (new): root with a live thread +
  `decomposed` event → exactly one reply, containing every child id in
  payload order; fold mode → same single reply (it is the root's own
  thread); a deleted child → id-only line; payload without `child_ids` →
  `task_links` fallback; operator `reply_templates["decomposed"]` override
  honored; `decomposed` dropped from `reply_on` → no reply, card still
  refreshes.
- `docs/mapping.md`: `decomposed` moves from "card refresh only" (it is
  currently unlisted there) into the reply-earning table with the template.
- `docs/adr/README.md`: index row for 0017.
- `plugin.yaml`: no change (no new setting).
