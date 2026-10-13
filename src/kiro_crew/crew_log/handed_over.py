"""Which slots have ever been handed over -- one trusted, cheap answer.

The question "was this session ever handed to another session" has to be answered for
every slot a start restores, and the two obvious places to keep the answer are both
wrong.

ON THE SLOT'S OWN METADATA LINE it is cheap and NOT TRUSTED. The line is the
transcript's, which the session's own writers rewrite, so the party whose grant a
handoff revoked can clear the field that says the grant exists. Clearing it is not the
same class of forgery as inventing a ``created_by``: that invents a creator for a slot
out of nothing, while this hands a DELIBERATELY REVOKED authorization back to a party
that already exists and is already authenticated. Cheaper to do and worth more.

IN THE SLOT'S OWN CREW LOG it is trusted and NOT CHEAP. A slot that was never handed
over has no entry to find, and proving that absence means reading the whole surviving
log of that slot -- so the cost falls on every restored tab on every start, forever,
for a feature most of them never used.

So the answer lives HERE: one append-only index, in the same fenced store the entries
are in, listing the DISTINCT slots a handoff has committed for. Read once per process
and cached. A slot absent from it was never handed over and is served by its creator
with no history read at all; a slot present in it has its own log read, which is the
path that already existed. The index grows with the number of slots ever handed over,
not with the number of handoffs and not with the size of any log, so there is no replay
and no per-slot listing.

WHAT MAKES IT SAFE IS THE SHAPE OF ITS FAILURES, and the natural handling of a missing
file is the unsafe one: an absent file reads as the empty set, and the empty set says
"nothing was ever handed over", which serves every slot from its creator. That is
exactly the answer a revoked party wants. So absence is split in two, the same split
:func:`~kiro_crew.crew_log.session_tree.slot_manager` makes:

* the store, or the index inside it, is NOT THERE: decided, and empty. No handoff can
  have been recorded on a store nothing ever wrote, so the first run of a gateway does
  not refuse every slot.
* the index IS there and cannot be read, parsed, or is past its bound: UNDECIDED. The
  caller refuses every slot until a read succeeds, because a short read of this file is
  indistinguishable from "no handoff" and that is the one direction that restores a
  grant the log recorded going away.

A WRITE THAT CANNOT LAND REFUSES THE HANDOFF, for the same reason the entry and the
tree edge do: an unindexed grant is one the next start will not read, so publishing it
would give the recipient a reach that disappears at the next restart while the giver
silently gets it back.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Final

from kiro_crew.atomic_write import atomic_write
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.store import KIND_SESSION, checked_crew_log_root
from kiro_crew.validation import MAX_SHORT_STRING

logger = logging.getLogger(__name__)

#: A LEAF OF ITS OWN under the kind root, not a file beside the unit directories: the
#: readers that list that root treat each child as a candidate unit, and a bare file
#: there would be one more shape each of them has to learn to skip.
INDEX_DIR: Final[str] = "handoff-index"

#: The file inside it. One file, because the answer is one set.
INDEX_NAME: Final[str] = "slots"

#: The most slots this index will carry. A handed-over slot is a session a person or an
#: agent deliberately gave to another session, so the real population is small; the
#: bound is here because this file is READ ON EVERY START and a reader that would
#: accept any size is a reader an append loop can make expensive. Past it the read is
#: UNDECIDED and the write REFUSES, which costs a handoff rather than losing one.
MAX_INDEXED_SLOTS: Final[int] = 4096

#: And the byte ceiling the parse applies before it splits anything, so a file grown by
#: something other than this writer cannot be read into memory first and judged after.
MAX_INDEX_BYTES: Final[int] = MAX_INDEXED_SLOTS * (MAX_SHORT_STRING + 1)

_lock = threading.Lock()

#: ``(slots, decided)`` for the store this process is bound to, or ``None`` before the
#: first read. Cached because the answer is consulted once per restored slot and the
#: file changes only when this process writes it.
_cached: "tuple[frozenset[str], bool] | None" = None


def _index_path() -> Path:
    """The index file's path under the validated store root.

    ``checked_crew_log_root`` is what establishes the root is the real one and not a
    link to somewhere the data home's protections do not reach, so the path is built
    through it on every call rather than cached.
    """
    return checked_crew_log_root(KIND_SESSION) / INDEX_DIR / INDEX_NAME


def reset_caches() -> None:
    """Forget the cached answer. Tests, and a process rebinding to another store."""
    global _cached
    with _lock:
        _cached = None


def handed_over_slots() -> "tuple[frozenset[str], bool]":
    """``(slots, decided)``: which slots a handoff has committed for.

    ``decided`` is False only when the index EXISTS and this reader could not establish
    what it says. A caller must then refuse the manager-dependent verbs for every slot
    rather than treat the empty set as an answer -- see the module docstring.
    """
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        _cached = _read()
        return _cached


def _read() -> "tuple[frozenset[str], bool]":
    try:
        path = _index_path()
    except CrewLogError:
        # The root itself is refused -- a linked or wrong kind directory. Nothing can be
        # established about a store this reader will not open.
        logger.warning("handoff index: the crew log root could not be established")
        return (frozenset(), False)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # DECIDED and empty: no handoff has ever been committed on this store, which is
        # every gateway that has not used the verb. Treating this as undecided would
        # refuse every slot on a first run.
        return (frozenset(), True)
    except OSError as exc:
        # NO TRACEBACK, and that is a decision about this frame rather than a style.
        # Retaining one keeps every local alive: ``raw`` and ``slots`` here ARE the
        # index -- the slot key of every session ever handed over on this store -- and
        # ``path`` is a location inside the operator's own data home. The failure is
        # fully described by its errno, so the stack buys nothing and would put a bulk
        # list of identifiers into a log that outlives the fault.
        logger.warning(
            "handoff index: the index exists and could not be read (%s), so which "
            "slots were handed over is UNDECIDED and the manager-dependent verbs are "
            "refused until a read succeeds",
            exc.strerror or type(exc).__name__,
        )
        return (frozenset(), False)
    if len(raw) > MAX_INDEX_BYTES:
        logger.error(
            "handoff index: %s is %d bytes, past the %d-byte bound, so which slots were "
            "handed over is UNDECIDED and the manager-dependent verbs are refused",
            path,
            len(raw),
            MAX_INDEX_BYTES,
        )
        return (frozenset(), False)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # No traceback, for the reason above: the frame holds the raw index.
        logger.error("handoff index: the index is not valid UTF-8, so it is UNDECIDED")
        return (frozenset(), False)
    slots = {line for line in text.split("\n") if line}
    if any(len(slot) > MAX_SHORT_STRING for slot in slots):
        logger.error("handoff index: %s holds an over-long slot key", path)
        return (frozenset(), False)
    if len(slots) > MAX_INDEXED_SLOTS:
        logger.error("handoff index: %s holds %d slots, past the bound", path, len(slots))
        return (frozenset(), False)
    return (frozenset(slots), True)


def record_handed_over(slot: str) -> bool:
    """Record that *slot* has been handed over. Answers whether the index says so now.

    True when the index holds *slot* after this call, INCLUDING when it already did:
    the fact is "this slot has been handed over at least once", so the second handoff of
    a slot writes nothing and is not a failure. That is what keeps the file proportional
    to handed-over slots rather than to handoffs.

    False on every way the write can fail, and the caller then refuses the handoff
    rather than publishing it: a grant the index does not carry is one the next start
    will not read, so the recipient would hold a reach that vanishes at the next
    restart while the giver silently gets it back.

    Rewritten whole rather than appended to. The set is small and bounded, the write is
    atomic, and a whole-file replace cannot leave a half-written final line -- which an
    append can, and which would read as a slot key nobody handed anything to.
    """
    global _cached
    key = str(slot or "")
    if not key or len(key) > MAX_SHORT_STRING:
        logger.error("handoff index: refusing to record %r as a handed-over slot", slot)
        return False
    with _lock:
        known, decided = _cached if _cached is not None else _read()
        if not decided:
            # The index cannot be read, so it cannot be rewritten either: replacing it
            # with what this process believes would DROP every slot the unreadable file
            # holds, turning one unreadable index into a permanently wrong one.
            logger.error(
                "handoff index: cannot record %s because the index could not be read, "
                "and rewriting it from a partial view would drop the slots it holds",
                key,
            )
            _cached = (frozenset(), False)
            return False
        if key in known:
            _cached = (known, True)
            return True
        if len(known) >= MAX_INDEXED_SLOTS:
            logger.error(
                "handoff index: %d slots already recorded, at the bound, so %s cannot be "
                "recorded and the handoff is refused",
                len(known),
                key,
            )
            return False
        updated = known | {key}
        try:
            atomic_write(
                _index_path(),
                "".join(f"{one}\n" for one in sorted(updated)),
                fsync=True,
                newline="\n",
            )
        except (OSError, CrewLogError) as exc:
            # NO TRACEBACK on the REFUSAL PATH especially. This frame holds ``known``
            # and ``updated`` -- the whole set of handed-over slot keys -- beside the
            # one being added, so a retained stack here would turn the fail-closed
            # answer into a bulk disclosure of exactly the identifiers the index is
            # fenced to protect. The slot key names what did not land, which is what a
            # person acting on this needs; the errno says why.
            logger.warning(
                "handoff index: %s could not be recorded (%s), so the handoff is refused",
                key,
                getattr(exc, "strerror", None) or type(exc).__name__,
            )
            return False
        _cached = (frozenset(updated), True)
        return True
