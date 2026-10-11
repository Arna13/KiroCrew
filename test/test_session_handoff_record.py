"""A handoff leaves a record in the handed-over session's own transcript.

A handoff is visible in the sidebar, in the crew log, on the slot and in the giver's
tool reply. It is visible in none of those places to someone reading the handed-over
session's conversation, who sees it change hands with nothing accounting for it.
``session_control.record_handoff_in_transcript`` closes that for the reader.

The row's ROLE is what these tests are mostly about. ``notice`` is display-only: the
model running in the session never sees the text, so the honest wording is a
third-person statement rather than an instruction, and the first test pins the fact
the wording depends on. Nothing the agent must act on rides on the row -- a worker
reaches its item through its binding, and a handoff does not move the binding.

The harness is imported from ``test_session_control_tree`` for the reason
``test_session_handoff`` imports it: these tests drive the same ``_reparent_target``
body through the same projection and emitter stub, and a second copy of that setup
would drift from the one the adoption tests exercise.

WHICH TESTS FAIL WITHOUT WHICH GUARD, so a reader can re-run the evidence rather than
take it on trust. Each line is a one-token edit to
``session_control.record_handoff_in_transcript`` or its caller, and the failure it
produces:

* ``async with target._handoff_record_lock:`` -> ``if True:`` --
  ``test_two_handoffs_of_one_session_record_in_the_order_they_committed`` fails with the
  second row landing first, and
  ``test_two_records_started_before_either_runs_still_append_in_order`` fails on two
  loads in flight at once.
* the second ``_target_replaced`` read, after the window load, deleted --
  ``test_a_replacement_that_arrives_during_the_window_load_gets_no_row`` fails.
* ``await load_window_off_loop(target)`` deleted --
  ``test_the_transcript_window_is_read_off_the_loop_before_the_row_is_appended`` fails.
* the ``if previous_manager`` arm of the row's text made unconditional --
  ``test_a_tab_with_no_manager_is_recorded_without_the_empty_clause`` fails on the
  rendered ``in place of .``.
* ``_named_in_a_row`` replaced by the bare keys --
  ``test_the_row_names_a_titled_manager_by_title_and_key`` fails.
* ``_start_handoff_record`` moved from the end of ``_commit`` to after
  ``_reparent_target`` returns --
  ``test_a_handoff_that_commits_after_the_caller_left_is_still_recorded`` fails with no
  row at all; a SECOND call added beside it fails
  ``test_one_commit_starts_one_record``.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from test_session_control_tree import (  # noqa: F401 -- imported FIXTURES apply here
    _emitted,
    _enabled,
    _hold,
    _key,
    _live,
    _run,
    _slot,
    _tree_on,
)

from kiro_crew.crew_log import emit
from kiro_crew.dashboard import session_control as sc

GIVER = "chat-lead"
RECEIVER = "chat-conductor"
WORKER = "chat-worker"


@pytest.fixture(autouse=True)
def _saves(_floor_monkeypatch):
    """Report every slot save as landed, and name the slots saved.

    The handoff writes no slot line of its own -- the crew-log entry is the handoff --
    so what this floor covers is every OTHER save a test's path may reach, which must
    not touch a real file. The list it returns is also the evidence a test can use to
    say the handoff saved nothing. The same floor the sibling handoff suites install.

    Patched through ``_floor_monkeypatch``, which is the rule for an AUTOUSE fixture:
    the shared ``monkeypatch`` stack is the one a test unwinds when it overrides this
    stub, and a floor patch sits under that stack rather than in it.
    """
    saved: list[str] = []

    async def _save(state, slot, **kwargs):
        saved.append(slot.key)
        return True

    _floor_monkeypatch.setattr(sc, "save_slot_off_loop", _save)
    return saved


def _team(state, *, handed: bool = False):
    """A lead, a conductor and a worker the LEAD created. See ``test_session_handoff``."""
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    lead = _live(state, _slot(state, GIVER), "sid-lead")
    conductor = _live(state, _slot(state, RECEIVER), "sid-conductor")
    worker = _live(state, _slot(state, WORKER), "sid-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    if handed:
        worker._manager = conductor.key
    return lead, conductor, worker


def _handoff(state, *, caller: str, target: str, to: str):
    """Hand *target* over and then let the record task the commit started finish.

    The verb starts the row's task and does not wait for it, which is the point -- the
    row's window load must not spend the caller's bound or hold the slot's tree fence.
    A test reading the transcript does have to wait for it, so this is where that wait
    lives rather than in each test.
    """

    async def _drive():
        answer = await sc.handoff_target(state, caller_session_key=caller, target=target, to=to)
        await _settled(state)
        return answer

    return _run(_drive())


async def _settled(state, *, timeout: float = 5.0) -> None:
    """Await the background tasks still running, so the transcript can be read.

    The row's task is held in ``state._background_tasks``, the dashboard's own door for
    work that outlives its caller. Tasks already finished are skipped rather than awaited
    again: their done callbacks clear them from the set on a later loop iteration, and
    waiting for the set itself to empty would spin without ever yielding to the iteration
    that empties it.

    BOUNDED, and the expiry is an assertion naming what was still running. A row that
    never finishes -- a lock never released, a window load that never returns -- is a
    failure of the thing under test, and an unbounded wait would turn it into a hung
    worker that takes the rest of the run with it instead of a red test.
    """
    while True:
        running = [task for task in state._background_tasks if not task.done()]
        if not running:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*running, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            stuck = ", ".join(
                sorted(getattr(task.get_coro(), "__qualname__", repr(task)) for task in running)
            )
            raise AssertionError(
                f"the handoff record did not finish within {timeout}s: {stuck}"
            ) from None


def _notices(slot) -> list[str]:
    """Every notice row in *slot*'s transcript window, oldest first."""
    return [
        str(row.get("content") or "")
        for row in getattr(slot, "messages", [])
        if row.get("role") == "notice"
    ]


def test_the_row_reaches_a_reader_and_never_a_prompt():
    """The fact the wording depends on, pinned where a reader of the verb can find it.

    ``notice`` is in ``history_projection.DISPLAY_ONLY_ROLES`` and
    ``history_consolidation._prompt_rows`` drops those rows from every prompt it
    builds. So an imperative in this row would be an instruction nobody receives,
    and nothing load-bearing may be delivered through it. Neither fact is visible at
    the append site, which is why it is asserted here.
    """
    from kiro_crew.history_consolidation import _prompt_rows
    from kiro_crew.history_projection import DISPLAY_ONLY_ROLES

    assert "notice" in DISPLAY_ONLY_ROLES
    assert _prompt_rows([{"role": "notice", "content": "x"}]) == []


def test_a_handoff_records_itself_in_the_handed_over_sessions_transcript(
    tmp_path, monkeypatch, _saves
):
    """One row, on the right session, naming both managers."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    rows = _notices(worker)
    assert len(rows) == 1, "one row, on the transcript of the session that was handed over"
    assert RECEIVER in rows[0], "the manager it is under now"
    assert GIVER in rows[0], "and the one it was under before"
    assert _notices(conductor) == [], "the record goes to the report, not to the manager"
    assert _notices(lead) == []


def test_the_row_names_a_titled_manager_by_title_and_key(tmp_path, monkeypatch, _saves):
    """A key answers "which session" and nothing a reader recognises, so the title rides.

    ``chat-1760...-3`` is exact and unreadable. The sidebar title is what the person
    named it, so a named session appears as ``title (key)`` -- the title to be
    recognised, the key because two sessions may share one title. A session still on its
    untouched default has no title worth printing (``display_title`` answers
    ``NEW_SESSION_TITLE`` for it), and is named by its key alone.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    conductor.title = "Pipeline conductor"
    conductor._titled = True

    _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    row = _notices(worker)[0]
    assert f"Pipeline conductor ({RECEIVER})" in row, "the new manager, named and exact"
    assert f"in place of {GIVER}." in row, "the giver has no title, so it is the key alone"


def test_two_records_started_before_either_runs_still_append_in_order(
    tmp_path, monkeypatch, _saves
):
    """The order holds from the START of each task, not from one of them already holding.

    The ordering test above releases the first row while it is INSIDE the lock, which
    answers what happens to a row that has already taken it. This answers the state
    before that: both tasks exist and NEITHER has run, so neither is in the lock's wait
    queue yet, and the order rests on the tasks being started in commit order and the
    loop running them in the order they were scheduled.

    Driven straight at :func:`_start_handoff_record` rather than through the verb,
    because the verb cannot produce this state -- its second commit is separated from
    the first by its own crew-log settle, so the first task has always run by then.
    Calling the starter twice with no await between is the only way to hold two unrun
    tasks at once, which is exactly the case the question is about.

    Both tasks are then given the loop ONCE, with the first row's load held open. That is
    what makes this a test rather than a coincidence: the first task parks inside its
    load, and the second must queue on the lock behind it instead of running its own load,
    which the ``loads`` assertion below states directly. Without the lock the second load
    returns at once and the second row lands first.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    second = _live(state, _slot(state, "chat-second"), "sid-second")
    held = asyncio.Event()
    loads: list[str] = []

    async def _first_load_is_the_slow_one(slot):
        loads.append(slot.key)
        if len(loads) == 1:
            await held.wait()

    monkeypatch.setattr(sc, "load_window_off_loop", _first_load_is_the_slow_one)

    async def _drive():
        # Started back to back, with nothing awaited between them, so both tasks are
        # scheduled and unrun when the second one is created.
        sc._start_handoff_record(
            state, target=worker, manager_key=conductor.key, previous_manager=lead.key
        )
        sc._start_handoff_record(
            state, target=worker, manager_key=second.key, previous_manager=conductor.key
        )
        assert _notices(worker) == [], "neither has run yet"
        # One pass of the loop, so both tasks reach their first await.
        await asyncio.sleep(0)
        assert loads == [worker.key], (
            "only the first row is loading: the second is queued on the lock, not racing "
            "its own load"
        )
        held.set()
        await _settled(state)

    _run(_drive())

    rows = _notices(worker)
    assert len(rows) == 2, "one row per start"
    assert RECEIVER in rows[0], "the first start is the first row"
    assert "chat-second" in rows[1], "and the second start is the second row"


def test_one_commit_starts_one_record(tmp_path, monkeypatch, _saves):
    """One handoff, one task. Guards the count, so a retry added later cannot duplicate.

    The row is started from inside the commit, and the commit is called once by
    ``_reparent_target``'s window -- there is no retry loop around it today. That is a
    fact about code that could change: a retry added to the window, or a second start
    added beside this one, would append the same handoff twice to one transcript and no
    other test would notice, because every other assertion here reads row CONTENT.
    Counted at the start rather than at the row, so a duplicate is caught even when the
    two rows would be identical.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    started: list[str] = []
    real_start = sc._start_handoff_record

    def _counted(st, *, target, manager_key, previous_manager):
        started.append(target.key)
        return real_start(
            st, target=target, manager_key=manager_key, previous_manager=previous_manager
        )

    monkeypatch.setattr(sc, "_start_handoff_record", _counted)

    _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    assert started == [worker.key], "the commit starts the record exactly once"
    assert len(_notices(worker)) == 1, "and exactly one row lands"


def test_the_row_states_what_happened_without_instructing(tmp_path, monkeypatch, _saves):
    """Third person, no imperative: the agent never reads this row."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    row = _notices(worker)[0]
    for imperative in ("Report to", "you hold", "report to the", "from here on", "your "):
        assert imperative not in row, f"{imperative!r} addresses an agent that never reads it"


def test_the_row_names_the_targets_own_previous_manager(tmp_path, monkeypatch, _saves):
    """Not the caller: an owner may hand over a worker it did not create.

    The owner's tab is unfenced and may hand over any session in the workspace, so
    the caller and the manager being replaced are different sessions here. Naming the
    caller would put a session that never managed this one into its transcript.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    _handoff(
        state,
        caller=_key(_slot(state, "chat-owner-tab")),
        target=worker.key,
        to=conductor.key,
    )

    row = _notices(worker)[0]
    assert GIVER in row, "the manager the handoff replaced"
    assert "chat-owner-tab" not in row, "and not the caller, which never managed it"
    _ = lead


def test_a_tab_with_no_manager_is_recorded_without_the_empty_clause(tmp_path, monkeypatch, _saves):
    """A tab the owner opened has no previous manager, so the row names none.

    ``manager_of`` answers ``""`` for a slot carrying neither ``_manager`` nor
    ``_created_by``, which is the shape of a tab the user opened rather than one an
    agent created. The owner's dashboard is unfenced and may hand such a tab over, so
    that empty value reaches the row -- where a clause naming it renders as "in place
    of ." and leaves a reader with a sentence that has a hole in it.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    conductor = _live(state, _slot(state, RECEIVER), "sid-conductor")
    tab = _live(state, _slot(state, "chat-user-tab"), "sid-user-tab")
    _hold(state, tab.key, owner.key, sid="sid-user-tab")

    assert sc.manager_of(tab) == "", "the case this test is about: nobody managed it"

    _handoff(state, caller=_key(owner), target=tab.key, to=conductor.key)

    row = _notices(tab)[0]
    assert RECEIVER in row, "the manager it is under now"
    assert "in place of" not in row, "there was no previous manager to put in place of"
    assert row.endswith("manages it now."), "and the sentence still ends like one"


def test_a_handoff_that_commits_after_the_caller_left_is_still_recorded(
    tmp_path, monkeypatch, _saves
):
    """The row rides on the COMMIT and not on the caller's return.

    The caller's wait is bounded at ``_TREE_APPEND_TIMEOUT`` while the commit window
    runs on to ``_TREE_SETTLE_TIMEOUT``, and the window is shielded, so an append that
    settles slowly answers the caller ``tree_write_pending`` -- or takes its
    cancellation -- and then publishes the new manager anyway. A row written where the
    verb returns would be missing from exactly those handoffs: the session changes
    hands and its transcript says nothing, which is the gap the row exists to close.

    Staged the way the cancellation tests stage it: the manager entry's settlement is
    withheld, the caller is cancelled while parked on it, and the entry then lands.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    settles: list = []
    monkeypatch.setattr(emit, "on_manager_set", lambda sid, **kw: settles.append(kw["on_settled"]))

    async def _drive() -> None:
        call = asyncio.ensure_future(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
        # Against a DEADLINE rather than a count of zero-length sleeps, for the reason
        # the compensation suite waits that way: the verb hops a thread on the way here
        # and a thread hop needs wall time.
        deadline = asyncio.get_running_loop().time() + 10.0
        while not settles and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert settles, "the commit never reached the manager entry"
        window = next(iter(sc._commit_windows))

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert _notices(worker) == [], "nothing is recorded while the entry is unsettled"

        settles[0](True)
        await asyncio.wait_for(window, timeout=5)
        await _settled(state)

    _run(_drive())

    assert worker._manager == conductor.key, "the window published after the caller left"
    row = _notices(worker)[0]
    assert RECEIVER in row, "the manager it is under now"
    assert GIVER in row, "and the one it was under before"


def test_an_adoption_records_nothing(tmp_path, monkeypatch, _saves):
    """The control: an adoption moves the sidebar edge and changes no manager.

    Adoption and handoff share ``_reparent_target``, so a record written into that
    shared body rather than into the handoff would appear here too -- and an adopter
    does not become the adopted session's manager.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, "chat-owner-tab", sid="sid-worker")

    moved = _run(sc.adopt_target(state, caller_session_key=_key(lead), target=worker.key))

    assert moved["parent"] == GIVER, "the edge moved, which is all an adoption does"
    assert worker._manager == "", "an adoption does not change who manages the session"
    assert _notices(worker) == [], "so there is no new manager to record"
    _ = conductor


def test_a_closed_session_cannot_withhold_the_handoff(tmp_path, monkeypatch):
    """Best-effort: the record must never be able to undo a committed transfer."""
    state = _make_state(tmp_path)
    gone = _slot(state, "chat-gone")
    state._slots.pop(gone.key, None)

    assert (
        _run(
            sc.record_handoff_in_transcript(
                state, target=gone, manager_key=RECEIVER, previous_manager=GIVER
            )
        )
        is False
    )


def test_two_handoffs_of_one_session_record_in_the_order_they_committed(
    tmp_path, monkeypatch, _saves
):
    """Rows appear in handoff order even when the FIRST row's window load is the slow one.

    The recorder runs as its own task that nothing awaits, so two handoffs of the same
    session have two tasks in flight and the loop decides which appends first. The order
    that reverses is not hypothetical: a lazy window is loaded by the FIRST task, and the
    second task finds it in memory and returns from its load at once -- so the slow load
    belongs to the row that has to come first.

    Staged exactly that way: the first task is held inside its load until the second
    handoff has committed and its own task has been given the loop, and only then is the
    load released. A reader of that transcript must still see "lead to conductor" before
    "conductor to second", because a transcript is read top to bottom and the reverse
    order states that the session is under a manager it has already left.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    second = _live(state, _slot(state, "chat-second"), "sid-second")
    # Created by the CONDUCTOR, because the second handoff's caller is the conductor and
    # a handoff names a recipient the caller itself manages.
    second._created_by = conductor.key
    _hold(state, worker.key, lead.key, sid="sid-worker")

    held = asyncio.Event()
    loads: list[str] = []

    async def _first_load_is_the_slow_one(slot):
        loads.append(slot.key)
        if len(loads) == 1:
            await held.wait()

    monkeypatch.setattr(sc, "load_window_off_loop", _first_load_is_the_slow_one)

    async def _drive():
        await sc.handoff_target(
            state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
        )
        # The second handoff is made by the manager the first one installed, which is
        # what makes this a sequence a person can actually drive.
        await sc.handoff_target(
            state, caller_session_key=_key(conductor), target=worker.key, to=second.key
        )
        assert _notices(worker) == [], "both rows are still owed at this point"
        held.set()
        await _settled(state)

    _run(_drive())

    rows = _notices(worker)
    assert len(rows) == 2, "one row per handoff"
    assert RECEIVER in rows[0] and GIVER in rows[0], "the first handoff is the first row"
    assert "chat-second" in rows[1] and RECEIVER in rows[1], "and the second is the second"


@pytest.mark.parametrize(
    "refusal",
    ["tree_not_ready", "managers_not_restored", "deputy_refused", "manager_unresolved"],
)
def test_a_refused_handoff_writes_no_row(tmp_path, monkeypatch, _saves, refusal):
    """A refusal before the commit records nothing, because the row records the commit.

    The row is started from the END of the committed path, which is what makes "no
    handoff, no row" a property of WHERE it is started rather than a check it performs.
    A reader must never find a notice for a handoff that was refused, so each of the
    verb's refusal points before the crew-log append is driven here:

    * ``tree_not_ready`` -- the cycle check cannot read the queued tree edges, inside the
      reparent helper and after the gate.
    * ``managers_not_restored`` -- the fence itself refuses while the manager
      reconstruction is still running, which is earlier still: before the gate.
    * ``deputy_refused`` -- the recipient is ownership-fenced and the target is not, so
      the handoff would hand a fenced session reach it does not have. Refused at the
      recipient resolution, which is earlier than both.
    * ``manager_unresolved`` -- this one slot's own authority read has not succeeded, so
      who manages it is undecided rather than empty. Refused at the fence, per slot.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    caller, handed = _key(lead), worker

    if refusal == "tree_not_ready":

        def _unreadable(_nodes):
            raise RuntimeError("the queued edges could not be folded in")

        monkeypatch.setattr(sc, "_tree_with_pending_edges", _unreadable)
    elif refusal == "managers_not_restored":
        sc.arm_manager_restore()
    elif refusal == "manager_unresolved":
        # The slot's own flag, which is the state a slot built from history after boot
        # carries until its authority read succeeds. Set on the TARGET, because that is
        # the slot whose manager the fence has no decided answer for.
        worker._manager_unresolved = True
    else:
        # A tab the person opened, which is NOT ownership-fenced, handed to the
        # agent-created conductor, which is. The caller is the owner's own tab, the one
        # caller unfenced enough to attempt it.
        owner = _slot(state, "chat-owner-tab")
        handed = _live(state, _slot(state, "chat-user-tab"), "sid-user-tab")
        _hold(state, handed.key, owner.key, sid="sid-user-tab")
        caller = _key(owner)

    try:
        with pytest.raises(sc.SessionControlError) as exc:
            _handoff(state, caller=caller, target=handed.key, to=conductor.key)
    finally:
        # Cleared however the assertion goes: the gate is a module-level flag, and one
        # left armed would refuse every widened verb for the rest of the session.
        if refusal == "managers_not_restored":
            _run(sc.rebuild_managers_from_log(state))

    assert exc.value.code == refusal, "refused before anything was written"
    assert handed._manager == "", "so no manager was published"
    assert _notices(handed) == [], "and the transcript says nothing happened"
    assert _notices(conductor) == []
    assert not [task for task in state._background_tasks if not task.done()]


def test_a_handoff_whose_edge_is_taken_back_leaves_no_row(tmp_path, monkeypatch, _saves):
    """A row implies the edge STANDS. The compensated handoff is the case that asks it.

    The row is started from the end of the commit, and the sidebar edge is appended
    before the commit runs -- so a reader could in principle meet "handed over" in a
    transcript whose edge was put back afterwards. The window where that could happen is
    the one this drives: the recipient is closed from the manager entry's own settle
    callback, so the entry is on disk and the edge is durable when the verb refuses, and
    the refusal compensates the edge on the log.

    What makes the row safe is WHERE it starts: after the publish, as the commit's last
    statement, and the checks that can still refuse all run before it. So a refusal in
    this window takes the edge back and no row was ever started -- the two cannot
    disagree.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    settled: list[str] = []
    original = emit.on_manager_set

    def _close_the_recipient_then_settle(sid, **kw):
        # From the manager entry's settle, which is a later suspension than the check
        # before the append can cover -- the entry is durable by the time this runs.
        if not settled:
            settled.append(sid)
            state._slots.pop(conductor.key, None)
        original(sid, **kw)

    monkeypatch.setattr(emit, "on_manager_set", _close_the_recipient_then_settle)

    with pytest.raises(sc.SessionControlError) as exc:
        _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    assert exc.value.code == "recipient_replaced", "refused after its own entry landed"
    assert settled, "the manager entry did land, so the edge was durable"
    assert worker._manager == "", "nothing was published"
    assert _notices(worker) == [], "and no row claims a handoff the log took back"
    assert not [task for task in state._background_tasks if not task.done()]


def test_a_same_key_replacement_is_told_nothing_about_a_handoff(tmp_path, monkeypatch, _saves):
    """A key is not an identity, so the row goes to an OBJECT and never to a key.

    The transfer suspends on its crew-log append while it holds the tree lock, and the
    only replacement check runs before it. So the target can close and a NEW session can
    open under the same slot key inside that await. A row looked up by key would then
    land on that session's transcript, telling a reader its manager changed when it never
    did, and nothing removes the row afterwards.

    Driven through that append: the replacement arrives while the manager entry is being
    written, and the entry then lands. The handoff itself still has to stand -- the record
    is on disk, and this row is a record of it rather than a part of it.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    impostors: list = []

    def _replace_then_settle(sid, **kw):
        # Closed and reopened under the same key, with a log of its own mapped, exactly
        # as the adoption tests stage it on the final gate's hop.
        state._slots.pop(worker.key, None)
        impostors.append(_live(state, _slot(state, worker.key), "sid-impostor"))
        kw["on_settled"](True)

    monkeypatch.setattr(emit, "on_manager_set", _replace_then_settle)

    reply = _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    assert reply["manager"] == RECEIVER, "the transfer itself landed and is not undone"
    assert impostors and impostors[0] is not worker, "a different session under the same key"
    assert _notices(impostors[0]) == [], "which was never handed over, so it is told nothing"
    assert _notices(worker) == [], "and no row is written to a transcript that has left"


def test_the_transcript_window_is_read_off_the_loop_before_the_row_is_appended(
    tmp_path, monkeypatch, _saves
):
    """A lazy window must not be pulled off disk on the gateway loop.

    A tab nobody has opened since the last start carries its transcript as a pending
    window, and the first read of ``messages`` loads it -- synchronously, inside
    ``append_and_surface``. On the loop that is a large file read with no yield, which
    stalls every other chat. ``load_window_off_loop`` is the existing door for it, so
    what this pins is that the door is used, on the TARGET, and before the row exists.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    loaded: list = []

    async def _load(slot):
        loaded.append((slot, _notices(worker)))

    monkeypatch.setattr(sc, "load_window_off_loop", _load)

    _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    assert loaded, "the window is loaded through the off-loop door"
    assert loaded[0][0] is worker, "the handed-over session's own window"
    assert loaded[0][1] == [], "and before the row, which is what the append needs it for"
    assert len(_notices(worker)) == 1, "the row still lands"


def test_a_replacement_that_arrives_during_the_window_load_gets_no_row(
    tmp_path, monkeypatch, _saves
):
    """The window load suspends, so the identity read happens again after it.

    The check made before the load is a verdict formed before the suspension. A session
    that closes and reopens under the same key inside the load would otherwise be told
    its manager changed, on the strength of a read that was true a moment earlier.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    impostors: list = []

    async def _replace_during_load(slot):
        state._slots.pop(worker.key, None)
        impostors.append(_live(state, _slot(state, worker.key), "sid-impostor"))

    monkeypatch.setattr(sc, "load_window_off_loop", _replace_during_load)

    reply = _handoff(state, caller=_key(lead), target=worker.key, to=conductor.key)

    assert reply["manager"] == RECEIVER, "the transfer itself landed and is not undone"
    assert _notices(impostors[0]) == [], "the new session was never handed over"
    assert _notices(worker) == [], "and the departed one gets no row either"


def test_a_slow_window_load_does_not_reach_the_callers_answer_or_the_fence(
    tmp_path, monkeypatch, _saves
):
    """The row is started, not awaited, so its load is on nobody's critical path.

    The commit that starts the row runs with the slot's tree fence held and inside the
    caller's ``_TREE_APPEND_TIMEOUT`` bound. A lazy window can be a large file read, so
    awaiting the row there would answer a handoff that has already landed with
    ``tree_write_pending`` and keep every other tree verb on this slot waiting out the
    load. Staged with a load that takes longer than the caller's whole bound.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.2)

    async def _drive():
        # The load is HELD rather than slowed: the test releases it only after it has
        # read the caller's answer, so what is pinned is the order and not a bet on how
        # long a file read takes. A row awaited inside the commit would park the caller
        # here until its own bound ran out, which is the failure this guards.
        held = asyncio.Event()

        async def _held_load(slot):
            await held.wait()

        monkeypatch.setattr(sc, "load_window_off_loop", _held_load)
        try:
            answer = await asyncio.wait_for(
                sc.handoff_target(
                    state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
                ),
                timeout=5,
            )
            # Read BEFORE the row's task is let go: this is the state the caller is
            # answered in, and the fence is what another tree verb would meet.
            assert worker.key not in sc._tree_pending_slots, "the slot is unfenced already"
            assert _notices(worker) == [], "the row has not been written yet, by design"
        finally:
            held.set()
        await _settled(state)
        return answer

    reply = _run(_drive())

    assert reply["manager"] == RECEIVER, "the caller is answered, not told to retry"
    assert len(_notices(worker)) == 1, "and the row lands on its own time"
