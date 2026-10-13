"""The turn-end file-change flush reads and redacts off the event loop.

``_flush_file_changes_off_loop`` is what the chat turn awaits on every exit path.
Its after-content reads reach ``validate_file_path``'s subprocess-pool round trip
and its redaction scans the whole turn budget; on the loop that was a stall every
other session paid for, seconds long under GIL load. These tests pin where the
work runs, that the loop keeps serving while it runs, that the attach has
happened when the await returns (the durable save that follows relies on it),
and that a cancellation arriving mid-read is held: the chips are still attached,
and in the turn's ``finally`` the session permit is still released and the queue
still hands off before the cancellation resurfaces.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base
from turn_harness import Do, Raise, SlotSpec, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import (
    _flush_file_changes,
    _flush_file_changes_off_loop,
    _Snapshot,
)
from kiro_crew.dashboard.chat_utils import run_to_completion_held
from kiro_crew.dashboard.state import _ChatSlot

#: A ceiling that only names a hang (rule D1): a read parked on the loop can never
#: be released by a loop task, so the wait ends here with a failure that says so.
_LOST_RUN_CEILING_SECS = 30.0


@pytest.fixture
def short_tmp_dir():
    """A short ``/tmp`` path: a ``tmp_path`` carries ids the redactor can trip on."""
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "offloop-", dir=short_tmp_base()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _slot_with_one_change(path: str = "/tmp/off-loop-flush/x.py") -> _ChatSlot:
    slot = _ChatSlot("test-flush-off-loop")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    slot._file_changes = [{"path": path, "canonical_path": path, "content": "before\n"}]
    return slot


def _after(content: str):
    def read(path: str) -> _Snapshot:
        return _Snapshot(content, False)

    return read


class _HeldRead:
    """A patched after-content read parked until a loop task releases it.

    Both handshakes are bounded by the same ceiling, and ``release`` is set by
    the test's ``finally`` as well, so a flush that never calls the reader fails
    by name instead of parking a worker thread past the test timeout.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, path: str) -> _Snapshot:
        self.started.set()
        # Only a loop task sets this. A read running ON the loop would wait out
        # the ceiling, and the test asserting on it names the stall.
        self.release.wait(_LOST_RUN_CEILING_SECS)
        return _Snapshot("after\n", False)

    async def wait_started(self) -> None:
        started = await asyncio.to_thread(self.started.wait, _LOST_RUN_CEILING_SECS)
        assert started, "the after-content read was never started"


# ── the flush itself ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_content_is_read_on_a_worker_thread_and_attached_before_return() -> None:
    loop_thread = threading.get_ident()
    read_on: list[int] = []

    def read(path: str) -> _Snapshot:
        read_on.append(threading.get_ident())
        return _Snapshot("after\n", False)

    slot = _slot_with_one_change()
    with patch.object(chat_runner, "_safe_read_snapshot", read):
        held = await _flush_file_changes_off_loop(slot, turn_boundary=0, turn_start_mid="")

    assert held is None
    assert read_on and all(ident != loop_thread for ident in read_on)
    meta = slot.messages[-1]["meta"]
    assert meta["file_changes"][0]["before"] == "before\n"
    assert meta["file_changes"][0]["after"] == "after\n"
    # The save that follows the flush on the landed path reads these two.
    assert slot._dirty is True
    assert slot._file_changes == []


@pytest.mark.asyncio
async def test_loop_keeps_serving_while_the_after_content_read_is_in_flight() -> None:
    read = _HeldRead()
    loop_ran_during_read: list[bool] = []

    async def release_once_the_loop_turns() -> None:
        await read.wait_started()
        loop_ran_during_read.append(True)
        read.release.set()

    slot = _slot_with_one_change()
    releaser = asyncio.create_task(release_once_the_loop_turns())
    try:
        with patch.object(chat_runner, "_safe_read_snapshot", read):
            await _flush_file_changes_off_loop(slot, turn_boundary=0, turn_start_mid="")
        await releaser
    finally:
        read.release.set()

    assert loop_ran_during_read, "the event loop did not turn while the read was in flight"
    assert slot.messages[-1]["meta"]["file_changes"][0]["after"] == "after\n"


@pytest.mark.asyncio
async def test_cancellation_during_the_read_is_held_and_handed_back_after_the_attach() -> None:
    read = _HeldRead()
    slot = _slot_with_one_change()

    async def flush() -> asyncio.CancelledError | None:
        return await _flush_file_changes_off_loop(slot, turn_boundary=0, turn_start_mid="")

    try:
        with patch.object(chat_runner, "_safe_read_snapshot", read):
            task = asyncio.create_task(flush())
            await read.wait_started()
            # Two cancels, each delivered: a graceful shutdown escalating after
            # its timeout is the second one. Neither may abandon the finished read.
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "the flush unwound while its worker was still reading"
            read.release.set()
            held = await task
    finally:
        read.release.set()

    # Handed back, not raised: the caller chooses where it resurfaces.
    assert isinstance(held, asyncio.CancelledError)
    assert slot.messages[-1]["meta"]["file_changes"][0]["after"] == "after\n"
    assert slot._dirty is True
    assert slot._file_changes == []


@pytest.mark.asyncio
async def test_off_loop_flush_attaches_what_the_synchronous_composition_attaches() -> None:
    sync_slot = _slot_with_one_change()
    async_slot = _slot_with_one_change()
    with patch.object(chat_runner, "_safe_read_snapshot", _after("after\n")):
        _flush_file_changes(sync_slot, turn_boundary=0, turn_start_mid="")
        await _flush_file_changes_off_loop(async_slot, turn_boundary=0, turn_start_mid="")

    # The attach writes the ``file_changes`` list; the row's minted ``mid`` is the
    # slot's own and differs between the two slots.
    assert (
        async_slot.messages[-1]["meta"]["file_changes"]
        == sync_slot.messages[-1]["meta"]["file_changes"]
    )


@pytest.mark.asyncio
async def test_redaction_runs_on_the_worker_too() -> None:
    slot = _slot_with_one_change()
    with patch.object(chat_runner, "_safe_read_snapshot", _after("AKIAIOSFODNN7EXAMPLE after\n")):
        await _flush_file_changes_off_loop(slot, turn_boundary=0, turn_start_mid="")
    entry = slot.messages[-1]["meta"]["file_changes"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in entry["after"]
    assert entry["after"].endswith(" after\n")


@pytest.mark.asyncio
async def test_empty_or_mock_accumulator_schedules_no_worker() -> None:
    slot = _ChatSlot("test-flush-off-loop-empty")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)

    def read(path: str) -> _Snapshot:  # pragma: no cover - must not run
        raise AssertionError("no accumulated change, nothing to read")

    with patch.object(chat_runner, "_safe_read_snapshot", read):
        assert await _flush_file_changes_off_loop(slot) is None
        slot._file_changes = object()  # type: ignore[assignment]
        assert await _flush_file_changes_off_loop(slot) is None
    assert "file_changes" not in slot.messages[-1].get("meta", {})


@pytest.mark.asyncio
async def test_run_to_completion_held_returns_the_result_and_the_held_cancellation() -> None:
    assert await run_to_completion_held(asyncio.sleep(0, result="plain")) == ("plain", None)

    gate: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def held() -> tuple[str, asyncio.CancelledError | None]:
        return await run_to_completion_held(gate)

    task = asyncio.create_task(held())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    gate.set_result("late")
    result, cancelled = await task
    assert result == "late"
    assert isinstance(cancelled, asyncio.CancelledError)


# ── through the real turn ────────────────────────────────────────────────────

_SEEDED = [("user", "earlier"), ("assistant", "earlier answer")]


def _write(path: Path) -> list[AcpEvent]:
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="call-w",
            tool_name="fs_write",
            tool_kind="edit",
            title="Write",
            raw_tool_params={"command": "create", "path": str(path)},
            diff_old_text="before\n",
            diff_path=str(path),
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="call-w",
            tool_output="ok",
            tool_final=True,
            tool_status="completed",
        ),
    ]


def _window_chips(record: TurnRecord) -> list[dict]:
    changes = [
        row["meta"]["file_changes"]
        for row in record.window
        if row.get("role") == "assistant" and (row.get("meta") or {}).get("file_changes")
    ]
    assert changes, "no file_changes meta in the window"
    return changes[-1]


@pytest.mark.asyncio
async def test_a_landed_turn_reads_after_content_off_its_loop_and_persists_the_chips(
    short_tmp_dir: Path,
) -> None:
    f = short_tmp_dir / "x.py"
    f.write_text("after\n")
    loop_thread: list[int] = []
    read_on: list[int] = []

    def read(path: str) -> _Snapshot:
        read_on.append(threading.get_ident())
        return _Snapshot("after\n", False)

    def note_loop_thread(ctx: TurnContext) -> None:
        loop_thread.append(threading.get_ident())

    events = [
        Do(note_loop_thread),
        *_write(f),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]
    with patch.object(chat_runner, "_safe_read_snapshot", read):
        record = await run_turn(TurnScript(events=events), slot=SlotSpec(rows=_SEEDED))

    assert record.stop_reason == STOP_REASON_END_TURN
    assert read_on and loop_thread and set(read_on).isdisjoint(loop_thread)
    # Attached before the durable save: the PERSISTED reply row carries the chips.
    [*_, row] = [r for r in record.rows("assistant") if r["content"] == "done"]
    assert row["meta"]["file_changes"][0]["after"] == "after\n"


@pytest.mark.asyncio
async def test_a_cancel_during_the_finally_flush_still_releases_the_permit_and_hands_off(
    short_tmp_dir: Path,
) -> None:
    """The ``finally``'s flush is its first await. A cancel landing there (Stop,
    the turn ceiling, a shutdown) must not skip the session permit release or
    the queue hand-off, and the chips the turn made must still surface."""
    f = short_tmp_dir / "x.py"
    f.write_text("after\n")
    read = _HeldRead()

    async def cancel_mid_read(ctx: TurnContext) -> None:
        await read.wait_started()
        ctx.slot.task.cancel()
        read.release.set()

    def setup(ctx: TurnContext) -> None:
        ctx.slot.queue_insert(0, "next please")
        asyncio.get_running_loop().create_task(cancel_mid_read(ctx))

    events = [*_write(f), Raise(RuntimeError("boom"))]
    try:
        with patch.object(chat_runner, "_safe_read_snapshot", read):
            record = await run_turn(
                TurnScript(events=events, setup=setup), slot=SlotSpec(rows=_SEEDED)
            )
    finally:
        read.release.set()

    assert read.started.is_set()
    assert [call for call in record.session_calls if call.name == "release"]
    [successor] = record.successors
    assert successor.args[0] == "next please"
    assert _window_chips(record)[0]["after"] == "after\n"
