"""A handoff whose commit refuses puts the parent back, and the window settles alone.

The two halves of a handoff are the tree edge and the manager, and this file is about
what happens when only the first of them lands. ``_reparent_target`` appends the edge and
waits for it to be on disk BEFORE the commit runs, which is the right order -- a field
written first and an append that then failed would leave the gateway acting on a move no
record carries. The cost of that order is this file's subject: between the two there is a
window where the edge is durable and the manager has not moved.

A refusal inside that window is not a tidy no-op if it is left alone. The target hangs
under the recipient while the giver is still its manager, so the recipient cannot drive
it and the giver's own ``session_release`` answers ``not_parent`` -- the move is neither
done nor undoable by either session, and only an operator editing state could separate
them.

So the refusal is compensated: the previous parent is restored, durably, before the
caller is told. The log is append-only, so "restored" is another edge stated on top and
the fold prefers the newest -- both moves stay readable.

The second half of the file is the cancellation rule. The window runs in its own task
behind a shield, because a request that goes away must not take a half-finished handoff
with it: the entry is already queued and the commit may already have published. Cancelling
the caller cancels the WAIT and not the work.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from test_session_control_tree import (  # noqa: F401 -- imported FIXTURES apply here
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
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture(autouse=True)
def _saves(_floor_monkeypatch):
    """Report every slot save as landed and record which slots were saved.

    The same floor the handoff suite uses, for the same reason: what these tests are
    about is the ORDER of the durable writes, so the cache write is stubbed and observed.
    """
    saved: list[str] = []

    async def _save(state, slot, **kwargs):
        saved.append(slot.key)
        return True

    _floor_monkeypatch.setattr(sc, "save_slot_off_loop", _save)
    return saved


def _team(state):
    """A lead and a conductor, plus a worker the lead created. The dispatch shape."""
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    lead = _live(state, _slot(state, "chat-lead"), "sid-lead")
    conductor = _live(state, _slot(state, "chat-conductor"), "sid-conductor")
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    return lead, conductor, worker


def _entries(monkeypatch, *, manager_lands: bool = True) -> list[dict]:
    """Record every tree and manager entry the verb emits, IN ORDER.

    All three emitters, which is the point: the compensation is itself an entry, so a
    recorder that watched only the manager write could not tell a restored parent from an
    abandoned one. The order is what the assertions read -- the edge, then the manager,
    then (only on a refusal) the edge that puts the parent back.

    Each stub settles its own append, because the verb waits on it. ``manager_lands``
    stands in for a manager entry the writer gave up on, which is the second way a commit
    refuses after the edge is already durable.
    """
    calls: list[dict] = []

    def _record(op: str, sid: str, kw: dict, landed: bool) -> None:
        settle = kw.pop("on_settled", None)
        calls.append({"op": op, "sid": sid, **kw})
        if settle is not None:
            settle(landed)

    monkeypatch.setattr(
        emit, "on_session_adopted", lambda sid, **kw: _record("adopt", sid, kw, True)
    )
    monkeypatch.setattr(
        emit, "on_session_released", lambda sid, **kw: _record("release", sid, kw, True)
    )
    monkeypatch.setattr(
        emit, "on_manager_set", lambda sid, **kw: _record("manager", sid, kw, manager_lands)
    )
    return calls


def _refuse_recipient_once(state, conductor, calls: list[dict]) -> None:
    """Close the recipient from the EDGE's settle callback.

    That callback is the suspension between the last gate and the commit, which is where
    a recipient really can go: its own routes take its slot lock and not this verb's. It
    is the cheapest honest way to reach the commit's refusal with the edge already down.
    """
    original = emit.on_session_adopted

    def _close_then_settle(sid, **kw):
        if not calls:
            state._slots.pop(conductor.key, None)
        original(sid, **kw)

    emit.on_session_adopted = _close_then_settle


def _refuse_recipient_after_the_manager_append(state, conductor, calls: list[dict]) -> None:
    """Close the recipient from the MANAGER entry's settle callback.

    A later suspension than :func:`_refuse_recipient_once` reaches, and the one the
    check before the append cannot cover: the manager entry is on disk by the time this
    runs, so the only thing standing between a replaced recipient and the reach is a
    check made AFTER the append. The recipient's own routes take its slot lock and not
    this verb's, which is what makes this window real rather than contrived.
    """
    original = emit.on_manager_set

    def _close_then_settle(sid, **kw):
        if not any(call["op"] == "manager" for call in calls):
            state._slots.pop(conductor.key, None)
        original(sid, **kw)

    emit.on_manager_set = _close_then_settle


def _undecided_manager_append(monkeypatch) -> None:
    """Make the handoff's own manager append answer UNDECIDED, once.

    Stubbed at :func:`~kiro_crew.crew_log.emit.awaiting_settlement` rather than by
    withholding a settle for the real bound, because the bound is seconds and what is
    under test here is not how long the wait is. The entry is still QUEUED through the
    recorder, with nothing settling it, which is exactly the state the undecided answer
    describes: an append that may land at any time afterwards.

    Only the first call is answered this way. The correction that follows goes through
    the real wait, so what the assertions read is the correction's own outcome.
    """
    real = emit.awaiting_settlement
    answered: list[str] = []

    async def _settlement(emit_one, *, what, **kwargs):
        if not answered:
            answered.append(what)
            emit_one(lambda _wrote: None)
            return None
        return await real(emit_one, what=what, **kwargs)

    monkeypatch.setattr(emit, "awaiting_settlement", _settlement)


# -- a refused commit puts the parent back ------------------------------------


def test_a_refused_commit_restores_the_previous_parent(tmp_path, monkeypatch):
    """The finding itself. The edge is durable, the commit refuses, and the parent the
    target had is put back by a further entry -- so the tree and the manager agree again
    and the giver's own release still works.

    Without the compensation the last entry on disk would name the conductor as the
    parent while the lead was still the manager, and neither session could undo it.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_once(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == ["adopt", "adopt"], "the edge, then the undo"
    undo = calls[-1]
    assert undo["slot"] == worker.key
    assert undo["parent_slot"] == lead.key, "back under the parent it had"
    assert undo["previous_parent_slot"] == conductor.key, "and it says what it came from"
    assert worker._manager == "", "the manager never moved, so nothing to take back"


def test_a_refused_commit_releases_a_target_that_had_no_parent(tmp_path, monkeypatch):
    """Putting a root target back is a RELEASE, and that is not a detail.

    An adoption naming an empty parent is not an edge at all and the emitter refuses to
    write one, so a compensation that only ever adopted would have left the root case
    silently uncompensated -- the one case where the undo matters most, because there is
    no earlier parent to notice the target is missing.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    # No ``_hold``: the worker is a root, which is what a dispatched session is until
    # something adopts it.
    _refuse_recipient_once(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == ["adopt", "release"], "the edge, then the undo"
    undo = calls[-1]
    assert undo["slot"] == worker.key
    assert undo["previous_parent_slot"] == conductor.key, "released from where it was put"
    assert worker._manager == ""


def test_a_manager_entry_that_never_landed_also_restores_the_parent(tmp_path, monkeypatch):
    """The second way the commit refuses after the edge is down: the manager entry is the
    commit point, so a writer that gave up on it means the handoff did not happen -- and
    the edge that was ordered before it has to come back out."""
    calls = _entries(monkeypatch, manager_lands=False)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target="chat-worker", to="chat-conductor"
            )
        )

    assert exc.value.code == "handoff_not_recorded"
    assert [call["op"] for call in calls] == ["adopt", "manager", "adopt"]
    assert calls[-1]["parent_slot"] == lead.key
    assert worker._manager == "", "nothing is published on the strength of a dropped append"


def test_a_committed_handoff_leaves_the_parent_where_it_moved(tmp_path, monkeypatch, _saves):
    """Control. With nothing refusing there is NO undo entry -- a compensation that fired
    on the success path would hand every session straight back and the verb would do
    nothing at all."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    assert [call["op"] for call in calls] == ["adopt", "manager"], "no undo on success"
    assert result["manager"] == conductor.key
    assert result["previous_manager"] == lead.key
    assert worker._manager == conductor.key
    assert _saves == [], "the two entries are the whole of it; no slot line is written"


def test_a_compensation_that_cannot_be_written_still_reports_the_original_refusal(
    tmp_path, monkeypatch
):
    """The undo is bounded and non-raising. A caller that asked for a handoff must be told
    why the HANDOFF failed, not why the undo did -- the undo's own failure is an operator
    problem and goes to the log, because no retry by this caller fixes it."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_once(state, conductor, calls)

    def _undo_explodes(sid, **kw):
        if calls:
            raise OSError("the compensating append could not be queued")
        raise AssertionError("the first edge must still be emitted")

    original = emit.on_session_adopted

    def _dispatch(sid, **kw):
        if calls:
            _undo_explodes(sid, **kw)
        original(sid, **kw)

    monkeypatch.setattr(emit, "on_session_adopted", _dispatch)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced", "the original refusal, not the undo's"
    assert worker._manager == ""


# -- the commit window settles independently of the caller --------------------


def test_the_slot_stays_fenced_across_the_commit(tmp_path, monkeypatch):
    """No other verb may decide about the slot inside the window.

    The append's own fence clears when the append settles, which is exactly when the
    window OPENS -- so without a hold of its own the slot would be unfenced for the one
    stretch where its recorded parent and its manager disagree. Read from inside the
    commit, which is the only place the question can be asked.
    """
    _entries(monkeypatch)
    fenced: list[tuple[str, bool]] = []
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    original = emit.on_manager_set

    def _held(key: str) -> bool:
        pending = sc._tree_pending_slots.get(key)
        return pending is not None and not pending.done()

    def _check_then_emit(sid, **kw):
        # Observed at the commit point, which is the moment that matters: the edge is on
        # disk, the manager has not moved yet, and the two disagree until this lands.
        fenced.append(("target", _held(worker.key)))
        fenced.append(("recipient", _held(conductor.key)))
        original(sid, **kw)

    monkeypatch.setattr(emit, "on_manager_set", _check_then_emit)

    _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    assert fenced == [("target", True), ("recipient", True)], "BOTH endpoints, at the commit"
    assert worker.key not in sc._tree_pending_slots, "and released when the window closes"
    assert conductor.key not in sc._tree_pending_slots


def test_a_cancelled_caller_does_not_abandon_the_commit(tmp_path, monkeypatch):
    """The cancellation rule. The caller goes away mid-commit -- a disconnect, a tool
    timeout -- and the window finishes anyway: the entry is queued and the manager is
    published, so stopping halfway is what would leave the two halves apart.

    Driven at the manager entry, which is the commit point: the settle is withheld, the
    caller is cancelled while waiting on it, and only then does the entry land.
    """
    pushes: list[int] = []
    settles: list = []
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    monkeypatch.setattr(state, "push_slots_update", lambda: pushes.append(1))

    monkeypatch.setattr(
        emit,
        "on_session_adopted",
        lambda sid, **kw: (kw.get("on_settled") or (lambda _ok: None))(True),
    )

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_manager_set", _withhold)

    async def _drive() -> None:
        call = asyncio.ensure_future(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
        # Until the commit has reached the withheld entry, which is where the caller is
        # parked when it is cancelled. Waited on the STATE the verb publishes rather than
        # a count of zero-length sleeps: the verb hops a thread more than once on the way
        # here (the gate's warm, the id resolution), and a thread hop needs wall time, so
        # a fixed number of ``sleep(0)`` passes runs out on a loaded machine and the test
        # fails for the one reason it is not about. The helper's bound is half the test's
        # timeout and only names a hang.
        await async_wait_until(
            lambda: bool(settles), describe=lambda: "the commit never reached the manager entry"
        )
        window = next(iter(sc._commit_windows))

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not window.done(), "the cancellation must not have taken the window with it"

        settles[0](True)
        await await_until(window, what="the commit window the cancelled caller left behind")

    _run(_drive())

    assert worker._manager == conductor.key, "the window published after the caller left"
    assert pushes == [1], "and the sidebar work it owed still ran"
    assert not sc._commit_windows, "the window is not held after it finishes"
    assert worker.key not in sc._tree_pending_slots, "and the slot is unfenced"
    assert conductor.key not in sc._tree_pending_slots


# -- a handoff refused AFTER its own entry landed is taken back on the log ----


def test_a_recipient_replaced_after_the_append_is_refused(tmp_path, monkeypatch):
    """The check that decides runs AFTER the manager entry, not before it.

    The append suspends, and the recipient can be closed, rebound or reopened inside that
    suspension by its own routes, which take its slot lock and not this verb's. A verdict
    formed before the append and acted on after it would move the reach to whatever sits
    at the key by then.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert worker._manager == "", "nothing is published to a recipient that changed"


def test_a_handoff_refused_after_its_entry_landed_is_taken_back_on_the_log(tmp_path, monkeypatch):
    """The finding. The entry is the AUTHORITY, so a refusal that only declines to
    publish the field leaves the log still granting the reach -- and the next start
    publishes it, the gateway's refusal having lived in memory alone.

    The correction is another entry on top, because the log is append-only and the fold
    takes the newest statement per slot. This target had no manager before, so the
    correction is the revert: a correction naming the creator would instead record a
    handoff TO the creator, leaving the field set where it was empty.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == [
        "adopt",
        "manager",
        "manager",
        "adopt",
    ], "the edge, the manager entry, the manager taken back, then the edge put back"
    taken_back = calls[2]
    assert taken_back["slot"] == worker.key
    assert taken_back["manager"] == "", "no manager is named"
    assert taken_back["to_creator"] is True, "and the revert is what says so"
    assert taken_back["previous"] == conductor.key, "it records the handoff it undoes"


def test_the_correction_names_the_manager_the_handoff_replaced(tmp_path, monkeypatch):
    """A target already handed over once reverts to the manager it HAD, not to its
    creator. The two are different sessions, so a correction that always reverted to the
    creator would quietly take the earlier handoff away as well -- an undo of something
    nobody refused.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    # Already handed over once, so the FIRST manager is the session that may hand it on
    # again: ``handoff`` is one of the verbs a handoff widens, and the creator's own
    # reach went with the first transfer. The recipient is authorized on its own terms
    # too, so the giver has to be able to name it -- hence the conductor is this
    # session's own.
    first = _live(state, _slot(state, "chat-first-manager"), "sid-first-manager")
    first._created_by = lead.key
    conductor._created_by = first.key
    worker._manager = first.key
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(first), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    taken_back = calls[2]
    assert taken_back["manager"] == first.key, "back to the manager it had"
    assert taken_back["to_creator"] is False, "which is not a revert to the creator"
    assert worker._manager == first.key, "and the field never moved off it"


def test_an_undecided_manager_append_is_corrected_rather_than_abandoned(tmp_path, monkeypatch):
    """An append still QUEUED when the wait gives up is the case a two-valued answer
    cannot handle. It is not a refusal: it may land at any time afterwards, and it would
    then be the newest statement on the log -- so a start would publish a manager this
    verb refused.

    The correction is queued behind it rather than skipped. The writer is FIFO per unit,
    so whichever order the two land in, the newest statement is the correction.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _undecided_manager_append(monkeypatch)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "handoff_not_recorded"
    assert [call["op"] for call in calls] == ["adopt", "manager", "manager", "adopt"]
    assert calls[2]["to_creator"] is True, "the queued handoff is taken back"
    assert calls[2]["previous"] == conductor.key
    assert worker._manager == "", "and nothing is published on an undecided append"


# -- the settlement answer the correction is decided from ---------------------


def test_a_settled_append_answers_true_and_a_dropped_one_answers_false(tmp_path):
    """The two decided answers, which need no correction between them: one landed and one
    never will."""

    async def _both() -> tuple[bool | None, bool | None]:
        landed = await emit.awaiting_settlement(lambda s: s(True), what="a landed append")
        dropped = await emit.awaiting_settlement(lambda s: s(False), what="a dropped append")
        return landed, dropped

    assert _run(_both()) == (True, False)


def test_an_append_that_never_settles_answers_undecided(tmp_path):
    """The third answer, and the only one that obliges a caller to correct the log.

    Kept apart from False because the two differ in exactly what matters to a caller that
    has to undo: a dropped append left nothing behind, and this one may still land.
    """

    async def _never() -> bool | None:
        return await emit.awaiting_settlement(
            lambda _settle: None, what="an append nothing settles", timeout=0.05
        )

    assert _run(_never()) is None


def test_the_two_valued_wait_reads_undecided_as_not_recorded(tmp_path):
    """``awaiting_commit`` is the same wait for callers with nothing to take back, and it
    folds the two ways of not being on disk together -- which is safe only because those
    callers publish on the append rather than undo on its refusal."""

    async def _folded() -> bool:
        return await emit.awaiting_commit(
            lambda _settle: None, what="an append nothing settles", timeout=0.05
        )

    assert _run(_folded()) is False


# -- BOTH endpoints are fenced while the tree append is in flight -------------


def _withheld_tree_append(monkeypatch) -> "tuple[list[dict], list]":
    """Record the emitted entries and WITHHOLD the FIRST tree append's settle.

    Leaves the first move parked exactly where a slow writer parks it: the edge handed
    over, nothing on disk yet, and the projection still showing the tree as it was. That
    is the state every decision made meanwhile is made against, so it is the state the
    fence and the cycle check have to cover.

    Only the first, so a LATER move can still complete inside that window. A helper that
    withheld every append would make every second move fail for the one reason these
    tests are not about, and a guard that refused everything while anything was pending
    would pass just as well as one that reads the edges.
    """
    calls: list[dict] = []
    settles: list = []

    def _record(op: str, sid: str, kw: dict, landed: bool | None) -> None:
        settle = kw.pop("on_settled", None)
        calls.append({"op": op, "sid": sid, **kw})
        if landed is None:
            settles.append(settle)
        elif settle is not None:
            settle(landed)

    def _adopt(sid, **kw):
        # The first is held; every one after it settles, so the window stays open around
        # moves that are meant to be decided inside it.
        _record("adopt", sid, kw, None if not settles else True)

    monkeypatch.setattr(emit, "on_session_adopted", _adopt)
    monkeypatch.setattr(
        emit, "on_session_released", lambda sid, **kw: _record("release", sid, kw, True)
    )
    monkeypatch.setattr(emit, "on_manager_set", lambda sid, **kw: _record("manager", sid, kw, True))
    return calls, settles


def test_the_reverse_handoff_is_refused_while_the_first_append_is_in_flight(tmp_path, monkeypatch):
    """The cycle a target-only fence admits. A handoff's commit window outlives the
    request, so the edge can still be queued when the next verb is decided -- and the
    REVERSE handoff names the other endpoint as its target, so a fence on this one never
    sees it. ``is_ancestor`` then reads a tree the pending edge has not reached, admits
    the opposite move, and both entries land: a loop in an append-only log that no fold
    can present and no verb can undo.

    Fencing both endpoints refuses it instead, and ``tree_write_pending`` is the honest
    code -- the earlier write may still land, so the caller is told to read the tree and
    try again rather than that anything is wrong.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    async def _drive() -> None:
        first = asyncio.ensure_future(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
        # The caller's own wait runs out while the append is still queued, which is the
        # only way to reach the next verb with the window still running.
        with pytest.raises(sc.SessionControlError) as pending:
            await first
        assert pending.value.code == "tree_write_pending"
        assert settles, "the edge was handed to the writer"

        with pytest.raises(sc.SessionControlError) as reverse:
            await sc.handoff_target(
                state, caller_session_key=_key(lead), target=conductor.key, to=worker.key
            )
        assert (
            reverse.value.code == "tree_write_pending"
        ), "the recipient of the in-flight handoff is fenced too"
        assert [call["op"] for call in calls] == ["adopt"], "the reverse appended nothing"

        settles[0](True)
        for window in list(sc._commit_windows):
            await await_until(asyncio.shield(window), what="a commit window")

    _run(_drive())

    assert conductor.key not in sc._tree_pending_slots, "the parent fence is released"
    assert worker.key not in sc._tree_pending_slots


def test_a_handoff_the_caller_stopped_waiting_for_is_still_audited_and_broadcast(
    tmp_path, monkeypatch, _saves
):
    """The reach moves in the window, so the record of it has to be written there too.

    A caller whose wait runs out raises through ``_audit_denials``, which writes a
    ``denied`` line for that timeout. The window then commits and the manager really
    does move -- so an ``allowed`` line written in the caller's frame is never written
    at all, and the SEL is left saying the opposite of what happened. The sidebar is the
    same problem in a different place: a broadcast tied to the caller's frame never
    tells anyone the tree changed.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    pushes: list[int] = []
    monkeypatch.setattr(state, "push_slots_update", lambda: pushes.append(1))

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError) as pending:
            await sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        assert pending.value.code == "tree_write_pending"
        assert [entry["outcome"] for entry in audits] == ["denied"], "the timeout is denied"
        assert not pushes, "and nothing is broadcast yet"

        settles[0](True)
        for window in list(sc._commit_windows):
            await await_until(asyncio.shield(window), what="a commit window")

    _run(_drive())

    assert worker._manager == conductor.key, "the window published after the caller left"
    outcomes = [entry["outcome"] for entry in audits]
    assert outcomes == ["denied", "allowed"], "and the SEL says the handoff happened"
    allowed = audits[-1]
    assert allowed["slot_key"] == worker.key
    assert allowed["operation"] == "handoff"
    assert pushes == [1], "the sidebar is told exactly once"


# -- the verb's own pre-lock refusals are audited -----------------------------


def test_a_handoff_that_changes_nothing_is_audited(tmp_path, monkeypatch):
    """``already_manager`` is the verb's OWN refusal and it is raised before the lock, so
    nothing on the success path records it. A denied attempt to move control of another
    session is exactly what the SEL is for -- a boundary with no trail is the one most
    worth a trail."""
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=worker.key
            )
        )

    assert exc.value.code == "already_manager"
    assert audits == [
        {
            "caller_session_key": _key(lead),
            "operation": "handoff",
            "slot_key": worker.key,
            "outcome": "denied",
            "detail": {"code": "already_manager"},
        }
    ]


def test_a_recipient_on_another_memory_store_is_audited(tmp_path, monkeypatch):
    """The recipient-side refusals are resolved before the lock as well, and they are the
    ones worth seeing most: each is an attempt to put a session under a principal that
    may not have it. Audited against the TARGET, which is the session whose control was
    being moved; the recipient is named in the refusal's own message."""
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    conductor.memory_store = "member-raymond"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_other_store"
    assert [entry["outcome"] for entry in audits] == ["denied"]
    assert audits[0]["slot_key"] == worker.key, "attributed to the session being moved"
    assert audits[0]["detail"] == {"code": "recipient_other_store"}


# -- the cycle check sees QUEUED edges, not only settled ones -----------------


def _four_node_tree(state):
    """Four sessions and the two settled edges from the finding: ``D -> A``, ``B -> C``.

    All four are opened by the owner tab, so none of them is ownership-fenced and any of
    them may hand any other over -- which is what lets the sequence be driven at all. The
    shape is the point: the loop the fourth move closes runs through every one of them, so
    no two moves share an endpoint and a per-endpoint fence can never see it coming.
    """
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    nodes = {}
    for name in ("a", "b", "c", "d"):
        slot = _live(state, _slot(state, f"chat-{name}"), f"sid-{name}")
        slot._created_by = owner.key
        nodes[name] = slot
    _hold(state, nodes["d"].key, nodes["a"].key, sid="sid-d")
    _hold(state, nodes["b"].key, nodes["c"].key, sid="sid-b")
    return owner, nodes


def test_a_queued_edge_is_part_of_the_cycle_check(tmp_path, monkeypatch):
    """The four-node loop a per-endpoint fence cannot refuse.

    ``D -> A`` and ``B -> C`` are settled. ``A -> B`` is handed to the writer and stays
    queued, so the projection does not carry it. A handoff of ``C`` to ``D`` then names
    neither ``A`` nor ``B``: both endpoint fences pass, and an ancestor check against the
    settled tree alone sees no loop. Let it through and the log holds
    ``A -> B -> C -> D -> A``, four valid append-only entries the fold can only mark.

    The fix is to check against the settled tree PLUS every queued edge, so the move is
    refused ``would_cycle`` on the view that includes the one edge still in flight.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError) as pending:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["a"].key,
                to=nodes["b"].key,
            )
        assert pending.value.code == "tree_write_pending", "A -> B is queued, not settled"
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        with pytest.raises(sc.SessionControlError) as closing:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["c"].key,
                to=nodes["d"].key,
            )
        assert closing.value.code == "would_cycle", "the queued edge closes the loop"
        assert [call["op"] for call in calls] == ["adopt"], "the fourth move wrote nothing"

        settles[0](True)
        for window in list(sc._commit_windows):
            await await_until(asyncio.shield(window), what="a commit window")

    _run(_drive())

    assert sc._tree_pending_edges == {}, "the queued edge is forgotten once it settles"


def test_the_same_move_is_allowed_once_the_queued_edge_settles_elsewhere(tmp_path, monkeypatch):
    """The guard refuses a LOOP, not a coincidence of timing.

    Same four sessions and the same fourth move, with ``A -> B`` never queued: nothing is
    in flight, the settled tree holds no path from ``C`` up to ``D``, and the handoff goes
    through. Without this the previous test would pass just as well against a guard that
    refused every move while any append was pending.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)
    assert sc._tree_pending_edges == {}

    result = _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=nodes["c"].key, to=nodes["d"].key
        )
    )

    assert result["manager"] == nodes["d"].key
    assert [call["op"] for call in calls] == ["adopt", "manager"], "it really moved"
    assert sc._tree_pending_edges == {}


def test_a_queued_edge_that_closes_no_loop_does_not_refuse(tmp_path, monkeypatch):
    """A pending edge is not a veto. ``A -> B`` is queued and a handoff of ``D`` to ``C``
    is decided in that window: the combined view carries the queued edge and still shows
    no path from ``D`` up to ``C``, so the move is allowed.

    This is the half that keeps the guard honest about what it refuses -- an
    overlay that made every concurrent move fail would satisfy the loop case and be
    useless.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError):
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["a"].key,
                to=nodes["b"].key,
            )
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        # D is under A; putting it under C adds D -> C, and C's own chain is C (B -> C is
        # the other direction), so nothing closes.
        moved = await sc.handoff_target(
            state, caller_session_key=_key(owner), target=nodes["d"].key, to=nodes["c"].key
        )
        assert moved["manager"] == nodes["c"].key
        # Asserted HERE, while the first edge is still queued: this is the move that had
        # to be allowed inside the window, and once the first one settles its own commit
        # adds a second manager entry that says nothing about this.
        assert [call["op"] for call in calls].count("manager") == 1, "it committed"
        assert nodes["d"]._manager == nodes["c"].key

        settles[0](True)
        for window in list(sc._commit_windows):
            await await_until(asyncio.shield(window), what="a commit window")

    _run(_drive())

    assert sc._tree_pending_edges == {}, "both edges are forgotten once they settle"


def test_a_pending_set_that_cannot_be_read_refuses(tmp_path, monkeypatch):
    """FAIL CLOSED on the view itself. A cycle check is only as good as the tree it ran
    against, so "no loop" concluded from a view that could not be built is a confident
    wrong answer rather than a missing one -- the same rule this whole PR applies to an
    unreadable log.
    """
    _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    def _unreadable(_nodes):
        raise RuntimeError("the queued edges could not be folded in")

    monkeypatch.setattr(sc, "_tree_with_pending_edges", _unreadable)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(owner), target=nodes["c"].key, to=nodes["d"].key
            )
        )

    assert exc.value.code == "tree_not_ready"
    assert exc.value.status == 409, "retryable: the next attempt may read it"


def test_no_reachable_sequence_writes_a_cycle_to_the_log(tmp_path, monkeypatch):
    """The property the three cases above are instances of, asserted over the whole
    four-node shape: every ORDERED pair of moves is attempted, and whatever the verb
    allows, the edges that reach the log never form a loop.

    Driven against the real fold rather than a hand-checked list, because the thing worth
    pinning is not which pairs refuse but that no pair persists a cycle -- an append-only
    log cannot take one back.
    """

    def _loops(edges: dict[str, str]) -> bool:
        """Whether following ``child -> parent`` from any node ever revisits a node.

        Written out rather than taken from ``is_ancestor``, which answers a different
        question and answers it False for ``candidate == of`` by contract -- so asking it
        whether a slot is its own ancestor can never report a loop.
        """
        for start in edges:
            seen: set[str] = set()
            cursor = start
            while cursor in edges:
                if cursor in seen:
                    return True
                seen.add(cursor)
                cursor = edges[cursor]
        return False

    pairs = [("a", "b"), ("c", "d"), ("d", "c"), ("b", "a"), ("a", "c"), ("b", "d")]
    for first, second in [(x, y) for x in pairs for y in pairs if x != y]:
        calls, settles = _withheld_tree_append(monkeypatch)
        monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
        state = _make_state(tmp_path)
        owner, nodes = _four_node_tree(state)
        edges = {nodes["d"].key: nodes["a"].key, nodes["b"].key: nodes["c"].key}

        async def _drive() -> None:
            for child, parent in (first, second):
                try:
                    await sc.handoff_target(
                        state,
                        caller_session_key=_key(owner),
                        target=nodes[child].key,
                        to=nodes[parent].key,
                    )
                except sc.SessionControlError:
                    # A refusal the CALLER saw says nothing about the log: an append
                    # handed over before the refusal still lands. What reached the log is
                    # read off the emissions below, which is the whole shape of this bug.
                    pass
            for settle in settles:
                if settle is not None:
                    settle(True)
            for window in list(sc._commit_windows):
                try:
                    await await_until(asyncio.shield(window), what="a commit window")
                except sc.SessionControlError:
                    pass

        _run(_drive())

        # Folded from what was EMITTED, in order, the way the tree folds it: an adoption
        # sets the parent and a release clears it, newest entry winning. This is the tree
        # a cold start would rebuild, including every compensation.
        for call in calls:
            if call["op"] == "adopt":
                edges[call["slot"]] = call["parent_slot"]
            elif call["op"] == "release":
                edges.pop(call["slot"], None)

        assert not _loops(edges), f"{first} then {second} persisted a loop: {edges}"
        sc._tree_pending_edges.clear()
        sc._commit_windows.clear()


# -- a rollback is a forward write and is checked like one --------------------


def _loop_waiting_for_the_rollback(state):
    """The tree a compensation of ``A`` meets after the window ran: ``A -> B``,
    ``R -> A``, ``P -> R``.

    No cycle in it -- each edge was checked and each was fine -- and yet putting ``A``
    back under ``P``, the parent it had when the verb started, closes
    ``A -> P -> R -> A``. That is the shape the rollback has to refuse, and the reason it
    cannot be read off the tree the verb started from: the previous parent moved BELOW
    the target while the window was running.
    """
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    nodes = {}
    for name in ("a", "p", "r", "b"):
        slot = _live(state, _slot(state, f"chat-{name}"), f"sid-{name}")
        slot._created_by = owner.key
        nodes[name] = slot
    _hold(state, nodes["a"].key, nodes["b"].key, at=300, sid="sid-a")
    _hold(state, nodes["r"].key, nodes["a"].key, sid="sid-r")
    _hold(state, nodes["p"].key, nodes["r"].key, at=200, sid="sid-p")
    return owner, nodes


def test_a_rollback_that_would_cycle_is_recognised(tmp_path, monkeypatch):
    """The rollback is checked against the same settled-plus-queued view the forward path
    uses. Asserted on the tree above, where no single edge is a loop and the restoration
    is the one that would complete it."""
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    assert sc._restoring_would_cycle(nodes["a"].key, nodes["p"].key) is True
    # The other direction, so the check is not simply answering True: B is above A, and
    # putting A back under B closes nothing.
    assert sc._restoring_would_cycle(nodes["a"].key, nodes["b"].key) is False


def test_a_rollback_that_cannot_be_checked_is_treated_as_cyclic(tmp_path, monkeypatch):
    """FAIL CLOSED, and for a compensation that means answering True. A view this cannot
    build is a view that cannot clear the edge, and the alternative -- releasing to the
    root -- loses nesting rather than persisting a loop. That is the opposite direction
    from the forward path, which refuses the operation, because a compensation has no
    refusal available: it IS the undo.
    """
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    def _unreadable(_nodes):
        raise RuntimeError("the queued edges could not be folded in")

    monkeypatch.setattr(sc, "_tree_with_pending_edges", _unreadable)
    assert sc._restoring_would_cycle(nodes["a"].key, nodes["b"].key) is True


def test_the_compensation_releases_instead_of_restoring_into_a_loop(tmp_path, monkeypatch):
    """The whole rollback, driven through the helper the window calls: the target is
    RELEASED to the root rather than adopted back into the cycle.

    Without this the compensation is the forward path missing its guard -- it would state
    ``A -> P`` on an append-only log, and four valid entries later the fold has a loop it
    can only mark while no verb undoes it.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    landed = _run(
        sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["b"].key,
            parent_sid="sid-b",
            previous_parent=nodes["p"].key,
            previous_parent_sid="sid-p",
        )
    )

    assert landed is True
    assert [call["op"] for call in calls] == ["release"], "released, not adopted back"
    assert calls[0]["slot"] == nodes["a"].key
    assert calls[0]["previous_parent_slot"] == nodes["b"].key, "it says where it came from"


def test_a_rollback_with_no_loop_still_restores_the_parent(tmp_path, monkeypatch):
    """The guard refuses a loop, not a restoration. With the previous parent still ABOVE
    the target, the compensation adopts it back exactly as before -- otherwise every
    refused move would quietly flatten its target to the root."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    landed = _run(
        sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["r"].key,
            parent_sid="sid-r",
            previous_parent=nodes["b"].key,
            previous_parent_sid="sid-b",
        )
    )

    assert landed is True
    assert [call["op"] for call in calls] == ["adopt"]
    assert calls[0]["parent_slot"] == nodes["b"].key, "back under the parent it had"


# -- the commit admission is the admission a restart re-applies ---------------


def test_a_recipient_on_another_store_than_the_target_is_refused(tmp_path, monkeypatch):
    """The sharpest of the store rules: the one a RESTART will apply.

    The commit compared GIVER against RECIPIENT, and the rebuild compares RECIPIENT
    against the session it manages. Three sessions make those disagree -- an
    owner-rooted lead on the default store dispatches a member-store worker and a
    default-store conductor -- so a handoff is accepted now and silently revoked at the
    next start, which is worse than refusing it up front. Both comparisons are made at
    the verb, so the answer the caller gets is the answer the next boot will give.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    lead.memory_store = ""
    conductor.memory_store = ""
    worker.memory_store = "member-raymond"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_other_store"
    assert "different memory stores" in exc.value.message
    assert calls == [], "no grant is appended for a handoff that would not survive"
    assert worker._manager == ""


def test_the_stores_are_re_read_immediately_before_publication(tmp_path, monkeypatch):
    """The append suspends, so neither reading survives from the gate. A target rebound
    inside that window is refused at the publication check rather than published on a
    verdict formed before the change."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    original = emit.on_manager_set

    def _rebind_then_settle(sid, **kw):
        if not any(call["op"] == "manager" for call in calls):
            worker.memory_store = "member-raymond"
        original(sid, **kw)

    emit.on_manager_set = _rebind_then_settle

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert worker._manager == "", "nothing is published across a store that moved"
    assert [call["op"] for call in calls] == [
        "adopt",
        "manager",
        "manager",
        "adopt",
    ], "the grant is taken back on the log and the parent put back"


# -- the manager reconstruction is off the boot path and gated ----------------


def test_the_widened_verbs_refuse_until_the_manager_restore_settles(tmp_path, monkeypatch):
    """The reconstruction is history-sized, so it runs as its own task and READY does not
    wait on it. Until it settles every manager field is empty, so the fence would read the
    CREATOR for every slot -- which admits a giver whose reach a recorded handoff already
    moved away. That is the direction that must not be guessed, so the verbs whose answer
    depends on the reconstruction refuse while it runs.

    Self-clearing: the pass releases the gate however it ends, so a failure cannot leave
    the gateway refusing for the life of the process.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    assert not sc._manager_restore_pending, "no window is open by default"

    sc.arm_manager_restore()
    try:
        assert sc._manager_restore_pending
        for operation in ("send", "read", "stop", "close", "adopt", "release", "handoff"):
            with pytest.raises(sc.SessionControlError) as exc:
                sc.authorize_target(
                    state,
                    caller_session_key=_key(lead),
                    target=worker.key,
                    operation=operation,
                )
            assert exc.value.code == "managers_not_restored", operation
            assert exc.value.status == 409, "retryable: the pass is still running"
        # A verb OUTSIDE the widened set does not depend on the manager, so it is not
        # held up: the fence reads the creator for it either way.
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation="rename"
            )
            is worker
        )
    finally:
        _run(sc.rebuild_managers_from_log(state))

    assert not sc._manager_restore_pending, "the pass cleared the gate"
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
        is worker
    ), "and the verb is served once it has"


def test_the_gate_clears_even_when_the_restore_raises(tmp_path, monkeypatch):
    """A gate left armed by a failure would refuse every widened verb for the life of the
    process, which is worse than the fail-closed per-slot answer the pass already gives.
    Released in a ``finally``, so however the pass ends the gateway is usable."""
    state = _make_state(tmp_path)
    _team(state)

    async def _explode(*_a, **_k):
        raise RuntimeError("the pass could not run")

    monkeypatch.setattr(sc, "_restore_managers", _explode)
    sc.arm_manager_restore()
    assert sc._manager_restore_pending

    with pytest.raises(RuntimeError):
        _run(sc.rebuild_managers_from_log(state))

    assert not sc._manager_restore_pending, "the gate is not left armed by a failure"


def test_a_queued_rollback_is_announced_to_the_next_mover(tmp_path, monkeypatch):
    """The mirror image of the rollback's own cycle guard. Checking the restoration
    against the queued edges while not PUBLISHING it among them leaves the loop reachable
    from the other side: the next mover's cycle check reads a tree without the edge this
    compensation is about to write, admits a move that closes against it, and both land.

    So the rollback takes a settlement-backed waiter like any forward append -- it fences
    the slot and publishes ``slot -> previous parent`` -- and both survive a wait that
    runs out, because an append still queued is exactly the one a later decision must see.
    """
    settles: list = []

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_session_adopted", _withhold)
    monkeypatch.setattr(emit, "COMMIT_WAIT_SECONDS", 0.05)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    async def _drive() -> None:
        landed = await sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["r"].key,
            parent_sid="sid-r",
            previous_parent=nodes["b"].key,
            previous_parent_sid="sid-b",
        )
        assert landed is False, "nothing is on disk while the append is still queued"
        assert sc._tree_pending_edges == {
            nodes["a"].key: nodes["b"].key
        }, "the rollback edge is published for the next mover's cycle check"
        pending = sc._tree_pending_slots.get(nodes["a"].key)
        assert pending is not None and not pending.done(), "and the slot stays fenced"

        settles[0](True)
        await asyncio.sleep(0)

    _run(_drive())

    assert sc._tree_pending_edges == {}, "both clear when the writer settles, not before"
    assert nodes["a"].key not in sc._tree_pending_slots


def test_a_queued_rollback_blocks_the_move_that_would_close_on_it(tmp_path, monkeypatch):
    """What the announcement buys, end to end: while the restoration of ``A`` under ``B``
    is queued, a handoff that would put ``B`` under ``A`` is refused ``would_cycle``
    rather than decided against a tree missing that edge."""
    settles: list = []

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_session_adopted", _withhold)
    monkeypatch.setattr(emit, "COMMIT_WAIT_SECONDS", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _loop_waiting_for_the_rollback(state)

    async def _drive() -> None:
        assert (
            await sc._restore_parent(
                target_sid="sid-a",
                slot_key=nodes["a"].key,
                parent_key=nodes["r"].key,
                parent_sid="sid-r",
                previous_parent=nodes["b"].key,
                previous_parent_sid="sid-b",
            )
            is False
        )
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        with pytest.raises(sc.SessionControlError) as exc:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["b"].key,
                to=nodes["a"].key,
            )
        # Either refusal is the guard doing its job: the slot fence catches the move
        # whose endpoint is already in flight, and the cycle check catches it against
        # the announced edge. Both are fail-closed and retryable.
        assert exc.value.code in ("would_cycle", "tree_write_pending"), exc.value.code

        settles[0](True)
        await asyncio.sleep(0)

    _run(_drive())


# -- a fenced recipient may not be handed an unfenced session -----------------


def test_an_unfenced_target_handed_to_a_fenced_recipient_is_refused(tmp_path, monkeypatch):
    """The deputy the fence exists to prevent, assembled out of two correct admissions.

    The fence decides what a caller may reach, and for the verbs a handoff widens it
    reads the MANAGER. So a fenced, agent-created worker given one of the person's own
    tabs drives a session the fence does not apply to: through it the worker reaches every
    session in the workspace and reads the results back, while each half of how it got
    there was admissible on its own.

    Refused here rather than by making a managed slot count as fenced, which would change
    the TARGET's fence status as a side effect of being managed -- a person's own tab
    would silently lose its own reach for having been handed to someone.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    # The person's OWN second tab: no creator, so the fence does not apply to it.
    own_tab = _live(state, _slot(state, "chat-own-tab-2"), "sid-own-2")
    # An agent-made worker: fenced, reaches only what it created.
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    worker._created_by = owner.key
    assert not sc._caller_is_ownership_fenced(state, own_tab.key), "the target is unfenced"
    assert sc._caller_is_ownership_fenced(state, worker.key), "and the recipient is fenced"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(owner), target=own_tab.key, to=worker.key
            )
        )

    assert exc.value.code == "deputy_refused"
    assert calls == [], "nothing is appended for a handoff that would build a deputy"
    assert own_tab._manager == ""


def test_the_two_handoffs_the_feature_is_for_are_still_allowed(tmp_path, monkeypatch):
    """The refusal costs the feature nothing. Fenced to fenced is the dispatch shape the
    verb exists for -- a lead handing agent-created workers to an agent-created conductor
    -- and unfenced to unfenced is a person moving their own tabs around. Only the mixed
    direction builds a deputy, and only it is refused.
    """
    state = _make_state(tmp_path)
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    lead = _live(state, _slot(state, "chat-lead"), "sid-lead")
    conductor = _live(state, _slot(state, "chat-conductor"), "sid-conductor")
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    second_tab = _live(state, _slot(state, "chat-own-tab-2"), "sid-own-2")

    # Fenced to fenced: the dispatch shape.
    _entries(monkeypatch)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    assert sc._caller_is_ownership_fenced(state, worker.key)
    assert sc._caller_is_ownership_fenced(state, conductor.key)
    moved = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert moved["manager"] == conductor.key

    # Unfenced to unfenced: the person's own tabs.
    _entries(monkeypatch)
    assert not sc._caller_is_ownership_fenced(state, second_tab.key)
    assert not sc._caller_is_ownership_fenced(state, owner.key)
    third = _live(state, _slot(state, "chat-own-tab-3"), "sid-own-3")
    assert not sc._caller_is_ownership_fenced(state, third.key)
    moved = _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=third.key, to=second_tab.key
        )
    )
    assert moved["manager"] == second_tab.key


# -- every path that BUILDS a slot resolves its manager -----------------------


def test_the_resolver_is_what_every_build_path_calls(tmp_path, monkeypatch):
    """One helper, and the boot reconstruction is one of its callers rather than a second
    implementation. Asserted by driving the pass with the resolver stubbed: if the pass
    had its own copy of the read, the stub would not be reached and the count would not
    be the stub's.
    """
    state = _make_state(tmp_path)
    _team(state)
    seen: list[str] = []

    async def _resolver(state_, slot, **kwargs):
        seen.append(slot.key)
        return False

    monkeypatch.setattr(sc, "resolve_slot_manager", _resolver)
    assert _run(sc.rebuild_managers_from_log(state)) == 0
    assert sorted(seen) == sorted(state._slots), "every slot went through the one helper"


def test_a_resumed_handed_session_comes_up_under_its_recorded_manager(tmp_path, monkeypatch):
    """A slot built AFTER boot gets its manager read too, because the slot's own line
    carries none and the boot pass is long over.

    Published without it, the field is empty -- which reads as "the creator holds it" --
    so the handoff silently reverts: the giver gets send, read, stop and close back on a
    session the log records as driven by someone else, and the recorded manager is refused
    ``not_creator``. A grant handed back to someone recorded as not having it.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker._manager = ""

    # What the log says about the rebuilt slot, resolved through the one helper the
    # resume path calls.
    monkeypatch.setattr(sc, "_manager_still_admissible", lambda *_a, **_k: None)

    def _sync_reader(key):
        return (conductor.key, True) if key == worker.key else ("", True)

    moved = _run(sc.resolve_slot_manager(state, worker, slot_manager=_sync_reader, has_log=True))

    assert moved is True
    assert worker._manager == conductor.key, "the recorded manager is in control"
    assert sc.manager_of(worker) == conductor.key
    # And the giver is refused on the verbs a handoff moves.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
    assert exc.value.code == "not_creator", "the giver does not get the reach back"


def test_the_refusal_is_armed_before_the_first_slot_is_restored(tmp_path, monkeypatch):
    """ORDER, not presence. HTTP is already serving when the restore runs, and the
    restores publish slots one at a time and yield to the loop between them -- so a
    request can arrive while some slots exist and none has had its manager read.

    Armed after the work instead, every one of those requests is answered from
    ``_created_by``, which admits a giver whose reach a recorded handoff already moved
    away. The refusal has to be standing before anything can be served, which is a
    statement about sequence that only a sequence assertion can pin.
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    # The owner functions run on ``server``'s globals (see ``server_runtime.compose``),
    # so the names they call are patched there and not on the owner module.
    from kiro_crew.dashboard import server as srv

    order: list[str] = []
    monkeypatch.setattr(sc, "arm_manager_restore", lambda: order.append("armed"))

    async def _restore_open(state_):
        order.append("restore_open_slots")

    async def _restore_recent(state_, *_a, **_k):
        order.append("restore_recent_sessions")
        return 0

    async def _rebuild(state_):
        order.append("rebuild")
        return 0

    monkeypatch.setattr(srv.chat, "restore_open_slots_async", _restore_open)
    monkeypatch.setattr(srv.chat, "restore_recent_sessions_async", _restore_recent)
    monkeypatch.setattr(sc, "rebuild_managers_from_log", _rebuild)

    class _Pause:
        @staticmethod
        async def pause_before(_what):
            return None

    monkeypatch.setattr(srv, "cautious_boot", _Pause)
    state = _make_state(tmp_path)
    state._dynamic_cards = None

    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_window_minutes=0,
            restore_sessions=False,
            idle_slot_sweep_days=0,
            surface_channel_sessions=False,
        )
    )

    async def _drive() -> None:
        await srv._restore_dashboard_sessions(state, cfg)
        # Let the scheduled reconstruction run so the order list is complete.
        for _ in range(4):
            await _asyncio.sleep(0)

    _run(_drive())

    assert order[0] == "armed", f"armed first, before any slot is published: {order}"
    assert order.index("armed") < order.index("restore_open_slots")
    assert order.index("armed") < order.index("restore_recent_sessions")
    assert "rebuild" in order, "and the pass still runs"


# -- UNRESOLVED is its own state, and a slot in it is refused -----------------


def test_an_unresolved_slot_refuses_the_verbs_that_read_the_manager(tmp_path, monkeypatch):
    """An undecided read establishes nothing, so the slot is denied rather than answered
    from its creator.

    Empty means "the creator holds it", which is a claim a failed read cannot make: the
    entry moving the reach away may be sitting in exactly the part the read missed. So
    the two states are kept apart, and the slot that has no established answer refuses
    the verbs whose authorization depends on one.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    worker._manager_unresolved = True

    for operation in ("send", "read", "summary", "stop", "close", "adopt", "release", "handoff"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation=operation
            )
        assert exc.value.code == "manager_unresolved", operation
        assert exc.value.status == 409, "retryable: a later read may succeed"

    # A verb OUTSIDE the widened set does not read the manager, so it is unaffected.
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="rename"
        )
        is worker
    )

    # And an authoritative read clears it.
    def _decided(_key):
        return ("", True)

    assert _run(sc.resolve_slot_manager(state, worker, slot_manager=_decided, has_log=True)) is (
        False
    )
    assert worker._manager_unresolved is False
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
        is worker
    )


def test_a_slot_hydrated_from_a_line_is_unresolved_until_the_log_is_read(tmp_path, monkeypatch):
    """DEFAULT-DENY, which is what makes a forgotten build path fail closed.

    Every path that builds a slot from a metadata line goes through the codec, and the
    codec marks the slot unresolved. Only the resolver clears it. So a path added later
    that publishes a slot without consulting the log does not silently revert a handoff
    -- it produces a slot that refuses the manager-dependent verbs, which is visible.

    This is the closest expressible form of "a new path cannot publish a servable slot
    without the resolver": it is asserted on the hydrating chokepoint rather than on a
    roster of call sites, so a path nobody has written yet is covered too.
    """
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _make_state(tmp_path)
    built = state.get_or_create_slot("chat-rehydrated")
    assert built._manager_unresolved is False, "a freshly minted slot needs no read"

    codec.apply(state, built, {"created_by": "chat-lead"}, codec.Restore(name="chat-rehydrated"))

    assert built._manager_unresolved is True, "hydrating from a line marks it unresolved"
    assert built._manager == "", "and the line carries no manager to adopt"


def test_targeted_rehydration_resolves_the_manager_of_the_slot_it_builds(tmp_path, monkeypatch):
    """The THIRD path that builds a slot after boot, beside revive and resume.

    ``rehydrate_slot_from_history_async`` is how anything outside the persistence module
    rebuilds one slot from disk -- reached from ``api_send_message`` when a cron
    notification wakes an unpinned worker the startup budget skipped. It restores
    ``_created_by`` from the line and the line carries no manager, so without the read
    the handoff silently reverts: the giver gets send, read, stop and close back on a
    session the log records as driven by someone else.

    Asserted on the CALL, which is the same assertion the revive path carries. The codec
    marks every hydrated slot unresolved, so a path that skips this read produces a slot
    that refuses rather than one that quietly reverts -- but refusing forever is not the
    outcome either, which is why the call is pinned too.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import chat_persistence

    state = _make_state(tmp_path)
    built = _live(state, _slot(state, "chat-woken-worker"), "sid-woken")
    built._created_by = "chat-lead"
    # RETRACTED from the table: the function short-circuits on a slot that is already
    # published, and the case under test is the one where it has to build it.
    state._slots.pop(built.key, None)

    monkeypatch.setattr(
        chat_persistence,
        "_prefetch_rehydrate_inputs",
        lambda *a, **k: ({"created_by": "chat-lead"}, True, [{}], {}, None, None, False),
    )
    monkeypatch.setattr(chat_persistence, "slot_closed_since", lambda *a, **k: False)
    monkeypatch.setattr(chat_persistence, "_deletion_during_read", lambda *a, **k: None)
    monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", lambda *a, **k: built)
    monkeypatch.setattr(chat_persistence, "_recover_mcp_app_claims", lambda *a, **k: None)

    resolved: list[str] = []

    async def _traced(state_, slot, **kwargs):
        resolved.append(slot.key)
        return False

    monkeypatch.setattr(sc, "resolve_slot_manager", _traced)

    log = MagicMock()
    state.conversation_log = log

    async def _drive() -> None:
        await chat_persistence.rehydrate_slot_from_history_async(state, built.key)

    _run(_drive())

    assert resolved == [built.key], "the slot it built went through the manager resolver"


# -- the target-side refusals run on the RESOLVED slot, not on the caller's string ----


@pytest.mark.parametrize("naming", ["key", "title", "stem"])
def test_the_deputy_refusal_runs_whatever_the_target_is_named_by(tmp_path, monkeypatch, naming):
    """``_resolve_slot`` accepts a slot key, a transcript stem and a title, so a refusal
    written against the caller's STRING runs on one of those forms and silently skips the
    other two.

    That is the shape this change has had to close eight times: a guard that is correct
    where it runs, on a path that reaches the decision without running it. So the two
    refusals that compare the recipient against the session being handed take the RESOLVED
    slot, from the gate that already holds it -- one copy, every naming form.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    own_tab = _live(state, _slot(state, "chat-own-tab-2"), "sid-own-2")
    own_tab.title = "My Own Notes"
    own_tab._titled = True
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    worker._created_by = owner.key
    assert not sc._caller_is_ownership_fenced(state, own_tab.key)
    assert sc._caller_is_ownership_fenced(state, worker.key)

    named = {
        "key": own_tab.key,
        "title": "My Own Notes",
        # What ``list_sessions`` reports, which the tool description tells callers to pass.
        "stem": f"dashboard_{own_tab.key}",
    }[naming]
    assert sc._resolve_slot(state, named) is own_tab, "the harness names the same slot"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(sc.handoff_target(state, caller_session_key=_key(owner), target=named, to=worker.key))

    assert exc.value.code == "deputy_refused", naming
    assert calls == [], "nothing is appended for a handoff that would build a deputy"
    assert own_tab._manager == ""


@pytest.mark.parametrize("naming", ["key", "title", "stem"])
def test_the_store_comparison_runs_whatever_the_target_is_named_by(tmp_path, monkeypatch, naming):
    """The same hole, in the refusal that keeps a handoff from being accepted now and
    revoked at the next start. It compares the recipient against the session being handed,
    so it needs the resolved slot for the same reason."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    worker.title = "Worker Notes"
    worker._titled = True
    lead.memory_store = ""
    conductor.memory_store = ""
    worker.memory_store = "member-raymond"

    named = {
        "key": worker.key,
        "title": "Worker Notes",
        "stem": f"dashboard_{worker.key}",
    }[naming]

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(state, caller_session_key=_key(lead), target=named, to=conductor.key)
        )

    assert exc.value.code == "recipient_other_store", naming
    assert calls == [], "no grant is appended for a handoff that would not survive"


def test_the_manager_read_is_not_the_last_awaiting_act_on_the_resume_path(tmp_path):
    """ORDERING, asserted on the source because that is what the contract is about.

    ``resume_slot_from_history`` depends on the containment hook being the LAST awaiting
    act: the store-backed boundaries (channel link, Slack binding, outbound mirror) are
    read inside it, so an await placed after it can let a binding recorded during that
    await reach a published slot with no pass left to see it. The manager read is an
    off-loop crew-log read, so it is an awaiting act and belongs above the hook.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    source = (repo_root / "src/kiro_crew/dashboard/chat_api/resume.py").read_text(encoding="utf-8")
    resolve_at = source.index("await _session_control.resolve_slot_manager(state, slot)")
    hook_at = source.index("refusal = await containment(slot)\n", resolve_at)
    final_at = source.index("refusal = final_check(slot)", resolve_at)
    publish_at = source.index("state._slots[slot.key] = slot", resolve_at)

    assert resolve_at < hook_at, "the manager read runs BEFORE the last containment pass"
    assert hook_at < final_at < publish_at, "and the hook stays the last awaiting act"


# ── The refusal lives on the READ, not on an entry predicate ─────────────────
#
# The refusal owed when who manages a slot has not been read belongs on the READ
# (``require_manager_of``), not on a condition at the top of ``authorize_target``. A
# condition there has to name which operations consult the manager, and such a
# predicate only APPROXIMATES the read: it is too wide for a caller whose answer the
# manager cannot change, and too narrow for the three sites that reach the value
# without passing the gate. What this block pins is that the guard sits where the
# value is read, so neither error is reachable.


def test_an_unfenced_caller_is_served_while_the_managers_are_unread(tmp_path):
    """The availability property, and the First Principles clearance condition verbatim.

    A person's own tab carries no ``_created_by`` and no channel link, so it is not
    ownership-fenced and ``authorize_target``'s fence is skipped before any manager is
    read. Its answer therefore cannot depend on the manager -- and a refusal it cannot
    act on is a gateway that stops serving its owner after every restart, for a feature
    that install may never use.

    Both ways the value can be unread are covered, because they were two separate
    conditions and would have had to be fixed twice.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    owner = state.get_slot("chat-owner-tab")
    assert not sc._caller_is_ownership_fenced(state, owner.key), "the premise of this test"
    _hold(state, worker.key, lead.key, sid="sid-worker")

    sc.arm_manager_restore()
    try:
        for operation in ("send", "read", "summary", "stop", "close"):
            assert (
                sc.authorize_target(
                    state,
                    caller_session_key=_key(owner),
                    target=worker.key,
                    operation=operation,
                )
                is worker
            ), operation
    finally:
        _run(sc.rebuild_managers_from_log(state))

    worker._manager_unresolved = True
    for operation in ("send", "read", "summary", "stop", "close"):
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(owner), target=worker.key, operation=operation
            )
            is worker
        ), operation


def test_the_handoff_verb_still_refuses_an_unfenced_caller_on_an_unread_manager(tmp_path):
    """The site the proposed subtraction would have admitted a guess at.

    Gating the refusal on ``ownership_fenced`` alone is not safe, because ``handoff``
    reads the manager itself and not through the fence: its gate asks whether the
    recipient ALREADY manages the slot, and its commit records the manager it replaces.
    An unfenced caller is exactly the caller that subtraction would have let through,
    and a handoff recorded against a guessed previous manager is a correction that
    later names the wrong session.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    owner = state.get_slot("chat-owner-tab")
    assert not sc._caller_is_ownership_fenced(state, owner.key)
    worker._manager_unresolved = True

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(owner), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "manager_unresolved"
    assert exc.value.status == 409


def test_a_broadcast_refuses_rather_than_name_a_guessed_audience(tmp_path):
    """The second site outside the fence, and the one with no per-target gate behind it.

    ``broadcast_audience`` filters EVERY slot by who manages it, for any caller, fenced
    or not. Answered from an unread field it silently reaches a session handed away and
    silently misses one handed over. So an unreadable manager has to change the answer
    -- and the direction is EXCLUDE, not refuse, because enumerating is not authorizing:
    excluding narrows reach, while the fence's refusal protects a grant. Refusing the
    whole audience would make one unreadable slot a process-lifetime outage for every
    caller's broadcasts and rosters, including unfenced owners and other workspaces.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    assert sc.broadcast_audience(state, lead.key) == [conductor.key, worker.key]

    # A slot the CONDUCTOR holds by handoff, so the conductor is neither its creator nor
    # a session the field names once the read fails.
    worker._manager = conductor.key
    assert sc.broadcast_audience(state, conductor.key) == [worker.key]
    worker._manager = ""
    worker._manager_unresolved = True
    assert sc.broadcast_audience(state, conductor.key) == [], "narrowed, not 409"

    # A caller that holds it whatever the read says still gets it, so a read fault
    # cannot hide a caller's own session from its own roster.
    assert worker._created_by == lead.key
    assert sc.broadcast_audience(state, lead.key) == [conductor.key, worker.key]


def test_every_authorization_read_of_the_manager_goes_through_the_chokepoint(tmp_path):
    """The enumeration, mechanized: no src site authorizes off the raw read.

    Nine findings on this line had one shape -- a guard correct where it runs, on a path
    that reaches the decision without running it -- and each was closed by adding the
    guard to one more path. This test closes the SHAPE instead: ``manager_of`` is the
    raw read and may only be called by the rebuild (which is what makes the value
    known) and by :func:`require_manager_of` (which refuses first). Any other caller is
    a path that authorizes off a value nobody checked, and this fails rather than
    waiting for a reviewer to find it.
    """
    import ast
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    # Encoding PINNED: the default is the platform's, which is cp1252 on Windows, and
    # this module carries non-ASCII bytes -- so an unpinned read decodes there and not
    # here, and the contract this test pins would go unchecked on half the matrix.
    source = (repo_root / "src/kiro_crew/dashboard/session_control.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    #: The two functions allowed to read the manager without refusing first, each for a
    #: stated reason. ``require_manager_of`` IS the refusal, and nothing else may read
    #: the raw value for an authorization answer.
    permitted = {"require_manager_of"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in permitted or node.name == "manager_of":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "manager_of":
                offenders.append(f"{node.name}:{inner.lineno}")

    assert not offenders, (
        "these functions read manager_of() directly instead of require_manager_of(), so "
        f"they authorize off a value that may never have been read: {offenders}. Either "
        "call require_manager_of(state, slot), or add the function to `permitted` above "
        "with the reason it may read the raw value."
    )


def test_a_correction_that_does_not_land_leaves_the_slot_unresolved(tmp_path, monkeypatch):
    """A dropped rollback must not leave the giver being served a reach it will lose.

    The grant's settlement times out (still queued, outcome unknown) and the corrective
    append is then REJECTED -- the correlated pair, because a writer backed up enough to
    time out the first is the same writer at its ceiling for the second. The log may then
    end with the refused grant, which leaves this process unable to say who manages the
    slot.

    Answering the giver anyway is a grant handed out here and taken away at the next
    start, which is the direction the whole verb exists to avoid. So the slot goes
    UNRESOLVED, which is default-deny, and the caller is told that plainly with its own
    code instead of being told the giver still holds it.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    real_settlement = emit.awaiting_settlement
    real_commit = emit.awaiting_commit

    async def _manager_entry_stays_queued(emit_call, *, what=""):
        # ONLY the manager entry is left QUEUED -- outcome unknown, which is the one
        # settlement value that can still land after this verb refuses. The tree append
        # the verb makes first keeps its real (stubbed-and-settled) path, because
        # stalling that one refuses `tree_write_pending` before the commit is reached.
        if what.startswith("the handoff of"):
            emit_call(lambda _landed: None)
            return None
        return await real_settlement(emit_call, what=what)

    async def _correction_is_rejected(emit_call, *, what=""):
        # And the correction behind it is REJECTED. The correlated pair: a writer backed
        # up enough to time out the grant is the same writer at its ceiling here.
        if "back to" in what:
            emit_call(lambda _landed: None)
            return False
        return await real_commit(emit_call, what=what)

    monkeypatch.setattr(emit, "awaiting_settlement", _manager_entry_stays_queued)
    monkeypatch.setattr(emit, "awaiting_commit", _correction_is_rejected)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "handoff_unresolved", "not 'the giver still manages it'"
    assert exc.value.status == 409
    assert worker._manager_unresolved is True, "the slot answers no one until it is re-read"
    assert worker._manager == "", "and nothing was published"
    assert [call["op"] for call in calls].count("manager") == 2, "the grant, then the correction"

    # And the refusal is now what every reader of the value gets, including the giver
    # that was being served before.
    with pytest.raises(sc.SessionControlError) as served:
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
    assert served.value.code == "manager_unresolved"


def test_a_refusal_arms_the_read_that_clears_it(tmp_path, monkeypatch):
    """ "Retry in a moment" has to be a true instruction.

    ``resolve_slot_manager`` is called only by BUILD paths -- the boot pass, a revive, a
    resume from history -- and none of them runs again for a slot already open. So a
    slot that went unresolved stayed unresolved until the process restarted, while every
    refusal told the caller to retry. One bad unit anywhere in the store is enough to
    get there, because the listing behind the read is store-wide.

    Arming the re-read from the refusal itself is what makes the window self-clearing,
    and it is armed from the REFUSAL so there is no path that denies without scheduling
    the read that clears it.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True
    reread: list[str] = []

    async def _resolve(_state, slot, **kwargs):
        reread.append(slot.key)
        slot._manager_unresolved = False
        return False

    monkeypatch.setattr(sc, "resolve_slot_manager", _resolve)

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation="send"
            )
        assert exc.value.code == "manager_unresolved"
        await async_wait_until(
            lambda: reread == [worker.key], describe=lambda: "the re-read was never armed"
        )
        # Cleared by the read, so the retry the refusal asked for now works.
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation="send"
            )
            is worker
        )

    _run(_drive())
    assert not sc._manager_reresolve_inflight, "the slot is not left marked in-flight"


def test_the_armed_read_is_scheduled_once_for_a_slot_refused_many_times(tmp_path, monkeypatch):
    """A slot refused on every verb must not queue one store read per refusal.

    The refusal is raised from a sync chokepoint that a busy gateway can reach many
    times before the first read finishes, and the read lists the store. Without the
    in-flight set a fleet retrying in a loop would turn one unreadable slot into a
    store-read storm.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True
    release = asyncio.Event()
    started: list[str] = []

    async def _resolve(_state, slot, **kwargs):
        started.append(slot.key)
        await release.wait()
        slot._manager_unresolved = False
        return False

    monkeypatch.setattr(sc, "resolve_slot_manager", _resolve)

    async def _drive() -> None:
        for _ in range(5):
            with pytest.raises(sc.SessionControlError):
                sc.authorize_target(
                    state, caller_session_key=_key(lead), target=worker.key, operation="send"
                )
        await async_wait_until(lambda: started == [worker.key])
        assert started == [worker.key], "five refusals, one read"
        release.set()
        await async_wait_until(lambda: not sc._manager_reresolve_inflight)

    _run(_drive())


# ── Refusals name a session the way a person recognises it ───────────────────


def test_a_refusal_names_the_session_by_title_not_by_slot_key(tmp_path):
    """``chat-1760244`` identifies a session to the gateway and to nobody else.

    Every refusal this verb hands back is read by somebody -- in a tab, in an agent's
    transcript. A refusal that names only the key makes the reader go and look up which
    session it meant before they can act on what they were told, and the reader is
    usually the one person who already knows the session by its title.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker.title = "crawl the backlog"
    conductor.title = "release conductor"
    # A PRE-LOCK refusal, so the assertion is about the wording and not about which of
    # the verb's writes got furthest.
    conductor.memory_store = "another-principal"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "recipient_other_store"
    assert "release conductor" in exc.value.message
    assert conductor.key not in exc.value.message, "the key is not what a person reads"


def test_an_untitled_session_still_falls_back_to_its_key(tmp_path):
    """The key is the FALLBACK and is never dropped.

    A session whose title is empty, blank or still being generated must still be
    identified. Naming it ``''`` would be worse than naming the key: the reader then has
    nothing at all, and a blank name reads as a bug rather than as a missing title.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)

    assert sc.name_for_person(state, worker) == worker.key, "no title: the key"
    worker.title = "   "
    assert sc.name_for_person(state, worker) == worker.key, "blank title: still the key"
    worker.title = "named at last"
    assert sc.name_for_person(state, worker) == "named at last"

    # A bare key is accepted too, because the verb holds the recipient and the replaced
    # manager as keys alone.
    assert sc.name_for_person(state, worker.key) == "named at last"
    assert sc.name_for_person(state, "chat-never-opened") == "chat-never-opened", (
        "a key naming no live slot answers the key -- there is nothing better to say "
        "about a session this gateway is not holding"
    )


def test_a_long_title_is_cut_rather_than_run_through_a_refusal(tmp_path):
    """A title is author-written and unbounded; a refusal is one line somebody reads."""
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker.title = "x" * 400

    named = sc.name_for_person(state, worker)
    assert len(named) <= sc._NAME_FOR_PERSON_MAX
    assert named.endswith("\u2026"), "cut with an ellipsis, so the reader knows it was cut"
    assert named.startswith("x"), "and the front is kept, which is the identifying part"


def test_a_restored_prompt_is_not_dropped_because_the_manager_is_not_read_yet(tmp_path):
    """The user's own typed prompts must survive a restart, unread manager or not.

    The restore path STRIPS the containment stamp deliberately (it names a write target
    and the metadata line is editable), so every restored entry is compared against the
    all-False fail-closed baseline. Every hydrated slot starts `_manager_unresolved`, and
    the process-wide flag stands while the rebuild runs -- so a boolean counted against
    that baseline makes the first drain after a restart delete every restored prompt with
    "Queued message dropped".

    Those prompts were typed by a person into the session's own surface. Their admission
    never rested on who manages the slot, so an unread manager is not a constraint they
    gained. `manager_unknown` is therefore compared only when the entry RECORDED it, the
    way `workspace` is, and never against the unmarked baseline.
    """
    state = _make_state(tmp_path)
    _lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True
    now = sc.containment_snapshot(state, worker, on_probe_failure=True)
    assert now["manager_unknown"] is True, "the premise: the drain sees it held"

    # A RESTORED entry: no stamp at all, which is what the sanitizer hands back.
    assert sc.newly_held_constraints(now, {}) == [], "an unmarked entry is not dropped"
    assert sc.newly_held_constraints(now, None) == [], "nor is a malformed one"

    # A STAMPED entry admitted while the manager was readable IS dropped: its admission
    # did rest on the value, and it cannot be re-checked now.
    admitted = dict(now)
    admitted["manager_unknown"] = False
    assert sc.newly_held_constraints(
        now, {sc.QUEUED_CONTAINMENT_META_KEY: admitted}, manager_fence_applied=True
    ) == ["manager_unknown"]

    # And one admitted while it was ALREADY unknown is not a change -- it was knowingly
    # admitted under the constraint, so dropping it would refuse designed behaviour.
    assert (
        sc.newly_held_constraints(
            now, {sc.QUEUED_CONTAINMENT_META_KEY: dict(now)}, manager_fence_applied=True
        )
        == []
    )


# ── The caller half of the admission, re-asserted at the drain ───────────────


def test_a_queued_send_is_dropped_once_its_sender_no_longer_manages_the_target(tmp_path):
    """A handoff revokes the giver's reach, including over what it already queued.

    The target-side containment snapshot cannot see this: nothing about the worker
    changed. What changed is WHO manages it, and the entry was admitted on the relation
    between that value and the sender -- a relation that needed no re-check while the
    fence read ``_created_by``, because a slot's creator is written at birth and never
    moves. A handoff moves it, which is the point of the verb.

    Read through the same predicate ``authorize_target`` admitted the entry with, so the
    drain gains no second manager read of its own.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    stamp = sc.send_origin_meta(state, lead.key, manager_fenced=True)

    # Admitted while the lead held the worker.
    assert sc.sender_lost_its_reach(state, worker, stamp) is False

    # Handed over. Nothing about the worker's containment changed.
    worker._manager = conductor.key
    assert sc.sender_lost_its_reach(state, worker, stamp) is True
    assert (
        sc.newly_held_constraints(
            sc.containment_snapshot(state, worker, on_probe_failure=True), stamp
        )
        == []
    ), "the snapshot alone sees no change, which is why this check exists"

    # The new manager's own queued delivery is untouched.
    assert (
        sc.sender_lost_its_reach(
            state, worker, sc.send_origin_meta(state, conductor.key, manager_fenced=True)
        )
        is False
    )


def test_a_human_typed_prompt_is_never_dropped_for_a_sender_relation(tmp_path):
    """There is no sender to re-compare, and inventing one deletes user speech.

    A person typing into a session's own surface is admitted by the authenticated
    composer, which never reads who manages the slot. The absence of the stamp is what
    marks that population -- and it marks restored entries too, whose stamp the restore
    path strips on purpose, so a delivery that outlives a restart is not dropped for a
    relation this process has no way to attribute.
    """
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state)
    worker._manager = conductor.key

    assert sc.sender_lost_its_reach(state, worker, {}) is False, "no stamp, no drop"
    assert sc.sender_lost_its_reach(state, worker, None) is False, "nor a malformed one"
    assert (
        sc.sender_lost_its_reach(state, worker, {"queued_containment": {"linked": False}}) is False
    ), "a containment stamp alone is not a sender"


def test_an_unread_manager_drops_under_its_own_reason_not_the_senders(tmp_path):
    """The wording must not claim the sender lost a reach this cannot verify it had.

    ``sender_lost_its_reach`` reads through ``require_manager_of``, so an unread manager
    raises rather than answering. That is still a drop -- the relation cannot be
    re-checked -- but it is the ``manager_unknown`` drop, and the notice says the value
    could not be read rather than asserting a transfer nobody recorded.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True
    stamp = sc.send_origin_meta(state, lead.key, manager_fenced=True)

    with pytest.raises(sc.SessionControlError) as exc:
        sc.sender_lost_its_reach(state, worker, stamp)
    assert exc.value.code == "manager_unresolved"

    notice = sc.describe_containment_change(["manager_unknown"])
    assert "could no longer be read" in notice
    assert "no longer manages" not in notice, "it must not assert a transfer"


# ── Three outcomes, and only two of them mean the creator holds it ───────────
#
# The invariant: ``_manager`` carries only a statement the log actually makes, and
# EMPTY is a statement -- "the creator holds this session". The log makes it in exactly
# two ways: it records no handoff at all, or it records an explicit ``to_creator``
# transfer back. A recorded manager that cannot be published makes neither.


def test_an_authoritative_no_handoff_gives_the_session_to_its_creator(tmp_path):
    """Outcome 1. An entry-less log is a FACT about the slot, not a failed read."""
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True

    _run(sc.resolve_slot_manager(state, worker, slot_manager=lambda _k: ("", True), has_log=True))

    assert worker._manager == "", "no handoff recorded, so the creator holds it"
    assert worker._manager_unresolved is False, "and the slot is servable"
    assert worker._manager_grant_inadmissible is False
    assert sc.require_manager_of(state, worker) == lead.key


def test_an_explicit_transfer_back_gives_the_session_to_its_creator(tmp_path):
    """Outcome 2. A decided revert reads as empty, which is what ``to_creator`` records.

    The entry form exists so a correction can say "nobody manages this but its creator"
    rather than recording a handoff TO the creator, which would leave the field set
    where it should be empty.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker._manager = conductor.key

    _run(sc.resolve_slot_manager(state, worker, slot_manager=lambda _k: ("", True), has_log=True))

    assert worker._manager == "", "the transfer back is published as empty"
    assert worker._manager_unresolved is False
    assert worker._manager_grant_inadmissible is False
    assert sc.require_manager_of(state, worker) == lead.key


def test_a_recorded_manager_that_cannot_hold_it_denies_everyone(tmp_path):
    """Outcome 3, and the fix. NOT the recipient, and NOT the giver either.

    The log records a handoff to a conductor that has since closed. Handing the session
    back to the lead would be this gateway inventing the one statement the log never
    made -- a transfer back -- and handing a revoked grant to the session that gave it
    away. That is the direction the whole verb exists to prevent, so the answer is
    nobody until a fresh handoff records who holds it.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    # The recorded manager is gone, which is what a restart after it closed looks like.
    state._slots.pop(conductor.key, None)

    moved = _run(
        sc.resolve_slot_manager(
            state, worker, slot_manager=lambda _k: (conductor.key, True), has_log=True
        )
    )

    assert moved is True, "the field carries what the log records"
    assert worker._manager == conductor.key, "which is the inadmissible grant itself"
    assert worker._manager_unresolved is False, "the read succeeded; it is not unread"
    assert worker._manager_grant_inadmissible is True, "it is read and unusable"

    # The GIVER is refused, which is the whole finding.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
    assert exc.value.code == "manager_grant_inadmissible"
    assert exc.value.status == 409

    # A PERSON is not stranded: an owner's own tab is unfenced, so its reach never came
    # from this field and it can still act on the session.
    owner = state.get_slot("chat-owner-tab")
    assert not sc._caller_is_ownership_fenced(state, owner.key)
    for operation in ("send", "stop", "close", "release", "adopt"):
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(owner), target=worker.key, operation=operation
            )
            is worker
        ), operation


def test_an_inadmissible_grant_does_not_arm_a_re_read(tmp_path, monkeypatch):
    """A read that already succeeded will keep succeeding with the same answer.

    The unread refusal arms a re-read because that is what makes "retry in a moment"
    true. This one must not: the store listing would be spent once per refusal to learn
    what the gateway already knows, and a busy fleet retrying in a loop would turn one
    closed conductor into a store-read storm.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager_grant_inadmissible = True
    armed: list[str] = []

    async def _resolve(_state, slot, **kwargs):
        armed.append(slot.key)
        return False

    monkeypatch.setattr(sc, "resolve_slot_manager", _resolve)

    async def _drive() -> None:
        for _ in range(3):
            with pytest.raises(sc.SessionControlError) as exc:
                sc.authorize_target(
                    state, caller_session_key=_key(lead), target=worker.key, operation="send"
                )
            assert exc.value.code == "manager_grant_inadmissible"
        await asyncio.sleep(0)
        assert armed == [], "no re-read is scheduled for a decided answer"

    _run(_drive())


def test_a_fresh_handoff_brings_a_denied_slot_back(tmp_path, monkeypatch):
    """The way out that the refusal names, so the denial is not a dead end."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    worker._manager = "chat-gone"
    worker._manager_grant_inadmissible = True
    # An unfenced owner is who can still drive the recovery.
    owner = state.get_slot("chat-owner-tab")

    _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=worker.key, to=conductor.key
        )
    )

    assert worker._manager == conductor.key
    assert worker._manager_grant_inadmissible is False, "the statement the log was missing"
    assert [call["op"] for call in calls].count("manager") == 1
    assert sc.require_manager_of(state, worker) == conductor.key


def test_an_unfenced_caller_is_served_at_once_on_a_gateway_with_no_handoffs(tmp_path):
    """First Principles' clearance condition, written as the condition it states.

    "On a gateway with no ``session/manager_set`` entries, an unfenced caller's
    session-control verbs succeed right after a restart." Built the way a restart builds
    it -- every slot hydrated and therefore unresolved, the boot reconstruction armed and
    still running -- rather than by setting the flags, so the thing under test is the
    path a boot actually takes.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    owner = state.get_slot("chat-owner-tab")
    assert not sc._caller_is_ownership_fenced(state, owner.key), "the premise"
    # What a restart leaves behind: nothing read yet, the pass armed and in flight.
    worker._manager_unresolved = True
    sc.arm_manager_restore()
    try:
        for operation in ("send", "read", "summary", "stop", "close", "adopt", "release"):
            assert (
                sc.authorize_target(
                    state,
                    caller_session_key=_key(owner),
                    target=worker.key,
                    operation=operation,
                )
                is worker
            ), operation
        # The FENCED giver is still refused in the same window, which is the half of
        # the subtraction that had to survive it.
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation="send"
            )
        assert exc.value.code == "managers_not_restored"
    finally:
        _run(sc.rebuild_managers_from_log(state))

    # And with no entries at all the read decides "no handoff", so the creator holds it.
    assert worker._manager == ""
    assert worker._manager_unresolved is False


# ── The drain re-checks only the population the fence applied to ─────────────
#
# Re-assert at the drain exactly the predicate that admitted the entry, INCLUDING
# whether it applied. An unfenced caller's admission never reads the manager, because
# the fence short-circuits before the read -- so neither "the manager moved" nor "the
# manager cannot be read" is a constraint that admission gained. Getting this wrong
# deletes a message somebody had already been told was accepted, which is why the
# fail-closed direction here is NOT dropping.


def test_an_unfenced_senders_queued_message_is_never_dropped_for_the_manager(tmp_path):
    """A person's own tab sending into a session it did not create.

    ``authorize_target`` skipped the fence entirely, so the manager was never consulted
    and the caller is still authorized at delivery. Re-comparing the relation anyway
    drops the message and tells the sender its session does not manage a session it never
    managed and never needed to.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    owner = state.get_slot("chat-owner-tab")
    assert not sc._caller_is_ownership_fenced(state, owner.key), "the premise"

    # Stamped the way the verb stamps it for an unfenced caller.
    stamp = sc.send_origin_meta(state, owner.key, manager_fenced=False)
    assert sc.send_origin_manager_fenced(stamp) is False

    # Nothing was handed over, and it is still not dropped.
    assert sc.sender_lost_its_reach(state, worker, stamp) is False
    # Nor after a handoff to someone else.
    worker._manager = conductor.key
    assert sc.sender_lost_its_reach(state, worker, stamp) is False
    # Nor while the manager cannot be read at all.
    worker._manager_unresolved = True
    assert sc.sender_lost_its_reach(state, worker, stamp) is False


def test_an_unfenced_senders_entry_is_not_dropped_for_an_unreadable_manager(tmp_path):
    """The same population, through the containment snapshot rather than the sender.

    ``manager_unknown`` has to carry the identical gate. Without it the boot window
    deletes every stamped entry from every unfenced caller, which is the same defect
    reached by the other of the two drain checks.
    """
    state = _make_state(tmp_path)
    _lead, _conductor, worker = _team(state)
    worker._manager_unresolved = True
    now = sc.containment_snapshot(state, worker, on_probe_failure=True)
    assert now["manager_unknown"] is True, "the premise: the drain sees it held"

    admitted = dict(now)
    admitted["manager_unknown"] = False
    entry = {sc.QUEUED_CONTAINMENT_META_KEY: admitted}

    assert (
        sc.newly_held_constraints(now, entry, manager_fence_applied=False) == []
    ), "an admission the fence never touched did not read the manager"
    assert sc.newly_held_constraints(now, entry, manager_fence_applied=True) == [
        "manager_unknown"
    ], "and a fenced one still is"


def test_a_stamp_without_the_verdict_is_read_as_unfenced(tmp_path):
    """Fail-closed here means NOT dropping, which is the opposite of the usual reading.

    An entry written before the verdict was recorded, or with a malformed stamp, carries
    no evidence that the manager was ever read for it. Treating that as "fenced" would
    drop messages on the strength of a predicate nobody can show was applied, and the
    thing being destroyed is text somebody was already told had been accepted.
    """
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state)
    worker._manager = conductor.key

    assert sc.send_origin_manager_fenced({}) is False
    assert sc.send_origin_manager_fenced(None) is False
    assert sc.send_origin_manager_fenced({sc.SEND_ORIGIN_META_KEY: "not-a-dict"}) is False
    # The shape a stamp from before this field had: slot and tab, no verdict.
    legacy = {sc.SEND_ORIGIN_META_KEY: {"slot": "chat-lead", "tab": "tab-1"}}
    assert sc.send_origin_manager_fenced(legacy) is False
    assert sc.sender_lost_its_reach(state, worker, legacy) is False


def test_one_unreadable_slot_does_not_take_out_every_callers_roster(tmp_path):
    """The availability half, and why an audience excludes where a fence refuses.

    ``_manager_grant_inadmissible`` is cleared only by a fresh handoff, so a slot whose
    recorded manager closed before a restart carries it for the life of the process. If
    the audience scan refused on it, every default broadcast and every roster read would
    answer 409 for every caller -- unfenced owners included, and callers in other
    workspaces, none of whom have anything to do with that slot.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    other = _live(state, _slot(state, "chat-other"), "sid-other")
    other._created_by = conductor.key

    # The denied slot, as a restart after its recorded manager closed leaves it.
    worker._manager = "chat-gone"
    worker._manager_grant_inadmissible = True

    # Every caller still gets an answer, and the denied slot is simply absent from the
    # audiences of callers that cannot show they hold it.
    assert sc.broadcast_audience(state, conductor.key) == [other.key]
    owner = state.get_slot("chat-owner-tab")
    # The owner tab created the lead, so the lead is its audience -- and the denied slot
    # is simply not in it, rather than taking the whole answer down with a 409.
    assert sc.broadcast_audience(state, owner.key) == [lead.key]
    # Its creator still sees it, because that verdict does not depend on the read.
    assert worker.key in sc.broadcast_audience(state, lead.key)


def test_no_other_mover_can_slip_between_the_tree_verdict_and_the_commit_fence(tmp_path):
    """The window the target fence must not have, driven rather than argued about.

    ``_tree_append_verdict`` removes the target from ``_tree_pending_slots`` the moment
    the append's outcome is known, and the commit re-holds it. If anything can be
    scheduled between those two, a racing reparent reads an unfenced slot, passes the
    pending check and commits while the manager append is still in flight -- leaving the
    tree naming one parent and the manager naming another, which is the state the
    compensation exists to prevent and cannot repair once both landed.

    Asserted by watching the fence from the settle callback, which is the one place that
    runs at exactly the moment the verdict is published.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    #: Whether the target was EVER observed unfenced by a would-be racing mover, sampled
    #: from the settle callback and from a loop hop immediately after it.
    unfenced_seen: list[str] = []

    def _sample(when: str) -> None:
        if worker.key not in sc._tree_pending_slots:
            unfenced_seen.append(when)

    settles: list = []

    def _record(op, sid, kw, landed):
        settle = kw.pop("on_settled", None)
        if settle is not None:
            if op == "adopt":
                # Published at the instant the verdict becomes known, which is where the
                # unfence happens.
                settle(landed)
                _sample("at the adopt settle")
            else:
                settle(landed)
        settles.append(op)

    import unittest.mock as _mock

    with (
        _mock.patch.object(
            emit, "on_session_adopted", lambda sid, **kw: _record("adopt", sid, kw, True)
        ),
        _mock.patch.object(
            emit, "on_session_released", lambda sid, **kw: _record("release", sid, kw, True)
        ),
        _mock.patch.object(
            emit, "on_manager_set", lambda sid, **kw: _record("manager", sid, kw, True)
        ),
    ):

        async def _drive() -> None:
            call = asyncio.ensure_future(
                sc.handoff_target(
                    state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
                )
            )
            # Hop the loop repeatedly for the whole life of the verb: every hop is a
            # point a racing mover could have been scheduled, so sampling on each one
            # covers the gap if there is one.
            for _ in range(40):
                if call.done():
                    break
                # ONLY the interval that matters: after the tree verdict is published
                # and before the manager append is handed over. Outside it the slot is
                # legitimately unfenced -- before the verb registers the move, and after
                # the whole thing is done -- so sampling there says nothing.
                if "adopt" in settles and "manager" not in settles:
                    _sample("on a loop hop inside the window")
                await asyncio.sleep(0)
            await call

        _run(_drive())

    assert "manager" in settles, "the verb reached its commit, so the window was exercised"
    # The slot is unfenced once the verb is DONE, which is correct and not the race.
    assert worker.key not in sc._tree_pending_slots
    assert unfenced_seen == [], (
        "the target was observable as unfenced mid-verb at: "
        f"{unfenced_seen} -- another mover could have committed there"
    )
