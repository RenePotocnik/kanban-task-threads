"""ADR-0017: `decomposed` earns a reply in the root task's own thread,
listing the children. The board writes the event on the root (the epic) with
`child_ids` in the payload; the reply needs no fold routing — the root always
self-publishes. Everything runs against real SQLite fixtures — no network."""

import pytest
from conftest import FakeTransport, add_event, insert_task, link_tasks, make_board

from kanban_task_threads.consumer import DEFAULT_REPLY_ON, Consumer
from kanban_task_threads.store import StateStore

NOW = 1_789_700_100


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path / "kanban.db")


def make_parts(
    tmp_path,
    board,
    *,
    capabilities=None,
    fold_children=True,
    reply_on=None,
    reply_templates=None,
):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport(capabilities=capabilities)
    kwargs = {}
    if reply_on is not None:
        kwargs["reply_on"] = tuple(reply_on)
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        guild_id="9",
        fold_children=fold_children,
        reply_templates=reply_templates,
        **kwargs,
    )
    return store, transport, consumer


def decompose_shape(board, *, with_links=True):
    """The board's real decompose shape: the root row first, the children's
    `created` events BEFORE the root's `decomposed` event (that ordering is
    what decompose_triage_task commits), child_ids in the payload."""
    insert_task(board, "t_epic", title="the epic", status="todo")
    for i in range(2):
        insert_task(board, f"t_c{i}", title=f"child {i}", status="done")
        if with_links:
            link_tasks(board, f"t_c{i}", "t_epic")
    add_event(board, "t_c0", "created", {"assignee": "sandbox"})
    add_event(board, "t_c1", "created", {"assignee": "sandbox"})
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(
        board,
        "t_epic",
        "decomposed",
        {"child_ids": ["t_c0", "t_c1"], "root_assignee": "orchestrator"},
    )


# --- the reply itself ---------------------------------------------------------


def test_decomposed_earns_reply_listing_children(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board, fold_children=False)
    decompose_shape(board)
    report = consumer.run_once(now=NOW)
    assert not report.errors

    # Default mode: one thread per task — the children self-publish too.
    # Processing order follows the event ids, so the epic opens last (th3).
    opens = transport.ops("open_thread")
    assert len(opens) == 3
    assert opens[2][1].endswith("t_epic")
    (append,) = transport.ops("append")
    assert append[1].thread_id == "th3"
    text = append[2]
    assert text.startswith("🧩 decomposed into 2 tasks:")
    assert "▸ child 0 · t_c0" in text
    assert "▸ child 1 · t_c1" in text
    assert text.index("t_c0") < text.index("t_c1")  # decompose order kept
    assert append[3] is None  # unsigned: a board mutation, not an actor


def test_decomposed_reply_in_fold_mode(board, tmp_path):
    """fold_children on: the reply lands in the root's thread — which IS the
    hub — before the children attach; their lifecycle then follows it."""
    store, transport, consumer = make_parts(tmp_path, board)
    decompose_shape(board)

    report = consumer.run_once(now=NOW)
    assert not report.errors
    (open,) = transport.ops("open_thread")
    assert open[1].endswith("t_epic")
    (append,) = transport.ops("append")
    assert append[1].thread_id == "th1"
    assert append[2].startswith("🧩 decomposed into 2 tasks:")
    assert "▸ child 0 · t_c0" in append[2]
    assert "▸ child 1 · t_c1" in append[2]

    # Next pass: the children attach and their lifecycle shares the thread.
    add_event(board, "t_c0", "completed", {"summary": "child 0 done"})
    consumer.run_once(now=NOW + 10)
    appends = transport.ops("append")
    assert len(appends) == 2
    assert appends[1][1].thread_id == "th1"
    assert appends[1][2].startswith("▸ child 0 · t_c0")
    assert "child 0 done" in appends[1][2]


def test_decomposed_reply_follows_fold_to_grand_hub(board, tmp_path):
    """Multi-level: the decomposed task is itself a folded child of a grand
    epic — its announcement follows the parent tree into the grand hub,
    labelled with the task's own title, exactly like its other replies."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_top", title="the top", status="todo")
    insert_task(board, "t_mid", title="middle", status="todo")
    insert_task(board, "t_a", title="leaf a", status="done")
    link_tasks(board, "t_mid", "t_top")  # mid is top's prerequisite
    link_tasks(board, "t_a", "t_mid")  # a is mid's prerequisite
    add_event(board, "t_mid", "created", {"status": "ready"})
    add_event(board, "t_top", "created", {"status": "ready"})
    add_event(board, "t_mid", "decomposed", {"child_ids": ["t_a"]})

    consumer.run_once(now=NOW)  # top opens; mid holds (no hub thread yet)
    (open,) = transport.ops("open_thread")
    assert open[1].endswith("t_top")
    assert transport.ops("append") == []

    consumer.run_once(now=NOW + 10)  # mid attaches to top's thread
    (append,) = transport.ops("append")
    assert append[1].thread_id == "th1"
    assert append[2].startswith("▸ middle · t_mid")
    assert "🧩 decomposed into 1 task:" in append[2]
    assert "▸ leaf a · t_a" in append[2]


# --- robustness ---------------------------------------------------------------


def test_decomposed_deleted_child_is_id_only(board, tmp_path):
    """A child already gone from `tasks` degrades to id-only — the reply
    never fails closed on a title lookup (ADR-0009: the id always survives)."""
    store, transport, consumer = make_parts(tmp_path, board, fold_children=False)
    insert_task(board, "t_epic", title="the epic", status="todo")
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(board, "t_epic", "decomposed", {"child_ids": ["t_gone"]})
    consumer.run_once(now=NOW)

    (append,) = transport.ops("append")
    text = append[2]
    assert "🧩 decomposed into 1 task:" in text
    assert "▸ t_gone" in text


def test_decomposed_without_payload_uses_task_links(board, tmp_path):
    """A payload without `child_ids` falls back to `task_links` — children
    sit on the `parent_id` side of the link (ADR-0012)."""
    store, transport, consumer = make_parts(tmp_path, board, fold_children=False)
    insert_task(board, "t_epic", title="the epic", status="todo")
    insert_task(board, "t_c0", title="child 0", status="done")
    insert_task(board, "t_c1", title="child 1", status="done")
    link_tasks(board, "t_c0", "t_epic")
    link_tasks(board, "t_c1", "t_epic")
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(board, "t_epic", "decomposed")  # no payload at all

    consumer.run_once(now=NOW)
    (append,) = transport.ops("append")
    text = append[2]
    assert "🧩 decomposed into 2 tasks:" in text
    assert "▸ child 0 · t_c0" in text
    assert "▸ child 1 · t_c1" in text


def test_decomposed_template_override(board, tmp_path):
    """The existing `reply_templates` setting still wins (ADR-0006); the
    pre-composed fields are available to a custom template."""
    store, transport, consumer = make_parts(
        tmp_path,
        board,
        fold_children=False,
        reply_templates={"decomposed": "split: {children}"},
    )
    insert_task(board, "t_epic", title="the epic", status="todo")
    insert_task(board, "t_c0", title="child 0", status="done")
    link_tasks(board, "t_c0", "t_epic")
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(board, "t_epic", "decomposed", {"child_ids": ["t_c0"]})

    consumer.run_once(now=NOW)
    (append,) = transport.ops("append")
    assert append[2].startswith("split: ▸ child 0 · t_c0")


def test_decomposed_opt_out_via_reply_on(board, tmp_path):
    """`reply_on` remains the operator's dial: drop `decomposed` and the
    split is silent again — the card still refreshes (every kind repaints)."""
    store, transport, consumer = make_parts(
        tmp_path,
        board,
        fold_children=False,
        reply_on=[kind for kind in DEFAULT_REPLY_ON if kind != "decomposed"],
    )
    insert_task(board, "t_epic", title="the epic", status="todo")
    insert_task(board, "t_c0", title="child 0", status="done")
    link_tasks(board, "t_c0", "t_epic")
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(board, "t_epic", "decomposed", {"child_ids": ["t_c0"]})

    consumer.run_once(now=NOW)
    assert transport.ops("append") == []
    (open,) = transport.ops("open_thread")
    assert "1 done" in open[2].description  # rollup on the card, no reply
