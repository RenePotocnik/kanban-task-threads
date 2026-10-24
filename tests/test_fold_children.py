"""ADR-0016: fold_children — a task that gates an epic publishes its updates
into the epic's thread instead of opening its own. Link direction follows
kanban_db_graph.decompose_triage_task: the child is the parent_id
(prerequisite), the root/epic is the child_id (dependent). Everything runs
against real SQLite fixtures — no network."""

import pytest
from conftest import FakeTransport, add_event, insert_task, link_tasks, make_board

from kanban_task_threads.consumer import DEFAULT_REPLY_ON, Consumer
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import TransportError

NOW = 1_789_700_100


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path / "kanban.db")


def make_parts(tmp_path, board, *, capabilities=None, fold_children=True, reply_on=None):
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
        **kwargs,
    )
    return store, transport, consumer


def epic_with_subtasks(board, *, sub_events=("created",), epic_events=("created", "decomposed")):
    """The decompose shape: subtasks gate the epic (subtask = prerequisite).
    Subtask `created` events are inserted BEFORE the epic's `decomposed`
    event, exactly like decompose_triage_task commits them."""
    insert_task(board, "t_epic", title="the epic", status="todo")
    for kind in epic_events:
        payload = {"child_ids": ["t_s0", "t_s1"]} if kind == "decomposed" else {"status": "ready"}
        add_event(board, "t_epic", kind, payload)
    for i in range(2):
        sub = f"t_s{i}"
        insert_task(board, sub, title=f"subtask {i}", status="done", body=f"plan {i}")
        link_tasks(board, sub, "t_epic")  # parent=subtask (prerequisite), child=epic
        for kind in sub_events:
            payload = {"assignee": "sandbox"} if kind == "created" else {"summary": f"sub {i} done"}
            add_event(board, sub, kind, payload)


# --- routing: the fold target ----------------------------------------------


def test_default_is_one_thread_per_task(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board, fold_children=False)
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)
    assert len(transport.ops("open_thread")) == 3  # epic + both subtasks


def test_child_folds_into_epic_thread(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board)
    epic_with_subtasks(board, sub_events=("created", "completed"))
    report = consumer.run_once(now=NOW)

    assert not report.errors
    # ONE thread: the epic's. The subtasks never open their own.
    opens = transport.ops("open_thread")
    assert len(opens) == 1
    assert opens[0][1].endswith("t_epic")
    epic_thread = "th1"

    # Subtask replies land in the epic's thread, labelled with the child.
    appends = transport.ops("append")
    assert appends and all(a[1].thread_id == epic_thread for a in appends)
    texts = [a[2] for a in appends]
    assert any(t.startswith("▸ subtask 0 · t_s0") for t in texts)
    assert any(t.startswith("▸ subtask 1 · t_s1") for t in texts)
    # the completed summary survived into the labelled reply
    assert any("sub 0 done" in t for t in texts)

    # Folded rows: same thread/card as the epic, owner_task_id = epic.
    for sub in ("t_s0", "t_s1"):
        row = store.get_post("default", sub)
        assert row["thread_id"] == epic_thread
        assert row["message_id"] == "msg1"  # the epic's card, for reply anchors
        assert row["owner_task_id"] == "t_epic"
        assert row["state"] == "live"

    # Epic row is the owner of itself.
    assert store.get_post("default", "t_epic")["owner_task_id"] == "t_epic"

    # The epic's own `decomposed` reply (ADR-0017) refreshes its card in the
    # open pass; a subtask has no card of its own to edit.
    edits = transport.ops("edit_card")
    assert len(edits) == 1
    assert edits[0][1].thread_id == epic_thread
    # The card opened in pass 1 already carries the rollup — the children were
    # done before the thread existed, so it never rendered without them.
    assert "2 done" in opens[0][2].description
    consumer.run_once(now=NOW + 10)
    edits = transport.ops("edit_card")
    assert len(edits) == 2
    assert edits[1][1].thread_id == epic_thread
    assert "2 done" in edits[1][2].description


def test_second_pass_is_silent_when_folded(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board)
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)
    n = len(transport.calls)
    report = consumer.run_once(now=NOW + 10)
    assert len(transport.calls) == n
    assert report.opened == [] and report.replied == 0


def test_child_events_before_hub_hold(board, tmp_path):
    """In a real decompose the children's `created` events carry lower ids
    than the epic's `decomposed` event: the children are processed while the
    hub has no thread yet. They must hold — not publish, not error — and
    publish into the hub's thread once it exists. decompose_triage_task
    inserts the root row BEFORE the children's `created` events, so the
    hub's tasks row is present while its post row is not."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_epic", title="the epic", status="todo")
    insert_task(board, "t_s0", title="subtask 0", status="done")
    link_tasks(board, "t_s0", "t_epic")
    add_event(board, "t_s0", "created", {"assignee": "sandbox"})
    add_event(board, "t_s0", "completed", {"summary": "sub 0 done"})

    # The epic has no thread yet (no post row).
    report = consumer.run_once(now=NOW)
    assert transport.calls == []
    assert report.errors == []
    assert store.get_post("default", "t_s0") is None  # not attached, not lost

    # Now the epic's events land: it opens, its `decomposed` reply goes out
    # (ADR-0017), and the child STILL holds (it is processed first — lower
    # event ids — before the epic's row exists this pass).
    add_event(board, "t_epic", "created", {"status": "ready"})
    add_event(board, "t_epic", "decomposed", {"child_ids": ["t_s0"]})
    report = consumer.run_once(now=NOW + 10)
    assert not report.errors
    assert len(transport.ops("open_thread")) == 1
    assert store.get_post("default", "t_s0") is None
    (append,) = transport.ops("append")
    assert append[1].thread_id == "th1"
    assert append[2].startswith("🧩 decomposed into 1 task:")
    assert "▸ subtask 0 · t_s0" in append[2]

    # Next pass: the child attaches and its held events publish into th1.
    report = consumer.run_once(now=NOW + 20)
    assert not report.errors
    appends = transport.ops("append")
    assert len(appends) == 2
    assert appends[1][1].thread_id == "th1"
    assert appends[1][2].startswith("▸ subtask 0 · t_s0")
    assert "sub 0 done" in appends[1][2]


def test_multiple_topmost_dependents_keep_own_thread(board, tmp_path):
    """A prerequisite gating two epics: folding would duplicate the reply
    into both threads — keep the own thread instead."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_shared", title="shared work", status="done")
    for epic in ("t_e1", "t_e2"):
        insert_task(board, epic, title=f"epic {epic[-1]}", status="todo")
        link_tasks(board, "t_shared", epic)
        add_event(board, epic, "created", {"status": "ready"})
    add_event(board, "t_shared", "created", {"status": "ready"})
    add_event(board, "t_shared", "completed", {"summary": "shared done"})

    consumer.run_once(now=NOW)
    opens = transport.ops("open_thread")
    assert len(opens) == 3  # shared + both epics
    shared_rows = [c for c in opens if c[1].endswith("t_shared")]
    assert shared_rows  # the shared task self-publishes


def test_deleted_hub_falls_back_to_own_thread(board, tmp_path):
    """A fold target that vanished from `tasks` must not hold the task's
    cursor forever — the task publishes its own thread."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_s0", title="subtask 0", status="done")
    link_tasks(board, "t_s0", "t_epic")  # t_epic is NOT inserted
    add_event(board, "t_s0", "created", {"status": "ready"})
    add_event(board, "t_s0", "completed", {"summary": "sub 0 done"})

    report = consumer.run_once(now=NOW)
    assert not report.errors
    (open_call,) = transport.ops("open_thread")
    assert open_call[1].endswith("t_s0")


# --- failure policy stays per-task ------------------------------------------


def test_hub_tombstone_cascades_to_child(board, tmp_path):
    """A child first seen AFTER the hub's post died attaches in the hub's
    terminal state — one operator act (the human deletion) ends the child's
    publishing too."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_epic", title="the epic", status="todo")
    add_event(board, "t_epic", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    store.mark_tombstone("default", "t_epic", "404 at event 5: {}")

    # A NEW subtask appears after the hub's post died.
    insert_task(board, "t_s0", title="subtask 0", status="done")
    link_tasks(board, "t_s0", "t_epic")
    add_event(board, "t_s0", "created", {"assignee": "sandbox"})
    add_event(board, "t_s0", "completed", {"summary": "sub 0 done"})
    report = consumer.run_once(now=NOW + 10)
    row = store.get_post("default", "t_s0")
    assert row["state"] == "tombstone"
    assert "t_epic" in (row["detail"] or "")
    assert any("tombstone" in e for e in report.errors)
    assert transport.ops("append") == []


def test_429_backs_off_the_child_only(board, tmp_path):
    """A 429 on a child's reply backs off that child only — never the hub,
    never a sibling. `decomposed` is dropped from reply_on so this pass's
    appends are the children's own (the epic's announcement is
    test_decompose_reply's concern, not this one)."""
    store, transport, consumer = make_parts(
        tmp_path,
        board,
        reply_on=[kind for kind in DEFAULT_REPLY_ON if kind != "decomposed"],
    )
    epic_with_subtasks(board, sub_events=("created", "completed"))
    transport.queue_error("append", TransportError(429, {"retry_after": 7}))
    consumer.run_once(now=NOW)
    assert store.get_post("default", "t_s0")["backoff_until"] > NOW
    assert store.get_post("default", "t_epic")["backoff_until"] == 0
    assert store.get_post("default", "t_s1")["backoff_until"] == 0


def test_404_tombstones_the_child_not_the_hub(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board)
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)  # everything attached, replies out
    transport.queue_error("append", TransportError(404, {"message": "Unknown Message"}))
    add_event(board, "t_s1", "commented", {"author": "coder", "len": 4})
    consumer.run_once(now=NOW + 10)
    assert store.get_post("default", "t_s1")["state"] == "tombstone"
    assert store.get_post("default", "t_epic")["state"] == "live"


# --- card/maintenance ownership ----------------------------------------------


def test_child_progress_repaints_epic_card(board, tmp_path):
    """The epic's card (with its `depends on:` rollup) repaints when a
    folded child progresses — in the same pass, via the dirty vehicle."""
    store, transport, consumer = make_parts(tmp_path, board)
    insert_task(board, "t_epic", title="the epic", status="todo")
    add_event(board, "t_epic", "created", {"status": "ready"})
    insert_task(board, "t_s0", title="subtask 0", status="done")
    link_tasks(board, "t_s0", "t_epic")
    add_event(board, "t_s0", "created", {"assignee": "sandbox"})

    consumer.run_once(now=NOW)
    assert transport.ops("edit_card") == []  # an open is not an edit

    # The rollup now changes (subtask completes) — the child's reply dirties
    # the hub, and the repaint runs later in the same pass.
    add_event(board, "t_s0", "completed", {"summary": "sub 0 done"})
    consumer.run_once(now=NOW + 10)
    edits = transport.ops("edit_card")
    assert len(edits) == 1
    assert edits[0][1].thread_id == "th1"
    assert "1 done" in edits[0][2].description


def test_no_maintenance_from_folded_child(board, tmp_path):
    """Tag/name/archive follow the epic. A child completing must not PATCH
    the thread's metadata as if it owned it."""
    store, transport, consumer = make_parts(
        tmp_path, board, capabilities={"tags", "title_state", "rich_card"}
    )
    transport.status_tag_ids = {"todo": "tag_todo", "done": "tag_done"}
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)

    # Metadata touches happen only in the epic's own processing (its
    # `decomposed` event), never attributable to a subtask: with two folded
    # children completing in the same pass, maintenance ran for them too in
    # the broken world — it must now run exactly once, for the epic.
    maint_ops = ("set_status_tag", "rename", "set_archived")
    maint_calls = [c for c in transport.calls if c[0] in maint_ops]
    assert maint_calls
    assert all(c[1].thread_id == "th1" for c in maint_calls)


def test_repaint_skips_folded_child(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board)
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)
    store.set_card_dirty("default", "t_s0", True)  # e.g. left by a 429 path
    n = len(transport.calls)
    consumer.run_once(now=NOW + 10)
    assert len(transport.calls) == n  # no repaint: the child has no card
    assert store.get_post("default", "t_s0")["card_dirty"] == 0


def test_audit_map_excludes_folded_rows(board, tmp_path):
    store, transport, consumer = make_parts(tmp_path, board)
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)
    owners = store.owner_posts("default")
    assert [row["task_id"] for row in owners] == ["t_epic"]  # folded rows excluded
    assert len(store.live_posts("default")) == 3  # raw rows still all there


def test_announce_skips_dependent_folded_into_this_thread(board, tmp_path):
    """Opening a thread leaves a 'prerequisite opened' link reply in each
    dependent's thread (ADR-0012). A dependent FOLDED into this same thread
    must not receive a link reply to itself (ADR-0016)."""
    store, transport, consumer = make_parts(tmp_path, board)
    # X opens a thread; D is folded into X's thread; E has its own.
    insert_task(board, "X", title="x", status="running")
    insert_task(board, "D", title="d", status="done")
    insert_task(board, "E", title="e", status="done")
    link_tasks(board, "X", "D")  # X is D's prerequisite
    link_tasks(board, "X", "E")
    now = NOW
    store.begin_create("default", "X", now=now)
    store.complete_create("default", "X", thread_id="thX", message_id="msgX", destination="d")
    store.attach_folded(
        "default",
        "D",
        thread_id="thX",
        message_id="msgX",
        destination="d",
        owner_task_id="X",
    )
    store.begin_create("default", "E", now=now)
    store.complete_create("default", "E", thread_id="thE", message_id="msgE", destination="d")

    import sqlite3

    from kanban_task_threads.transport import ThreadRef

    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.row_factory = sqlite3.Row
    task_row = conn.execute("SELECT * FROM tasks WHERE id = 'X'").fetchone()
    report = consumer.run_once(now=now)  # no events: pure attach state
    consumer._announce_to_dependents(conn, "X", task_row, ThreadRef("thX", "msgX"), report)

    appends = transport.ops("append")
    assert len(appends) == 1  # only E's thread; D is folded into thX itself
    assert appends[0][1].thread_id == "thE"
    assert "prerequisite opened" in appends[0][2]
