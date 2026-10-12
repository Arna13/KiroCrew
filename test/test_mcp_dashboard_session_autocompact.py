"""``session_set_autocompact``: a session's compaction threshold over the dashboard MCP.

Pinned here: the scope (the caller's own session or one it created, for every
caller class), the range check shared with the per-slot route, the read shape
(``pct`` omitted), that a clear drops the override, that the write lands
through the slot route's own transaction (durable field, live override map),
that the write linearizes at the in-lock gate so no refusal follows a commit,
and that the route is a strict internal-secret path.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_VERIFIED = "dashboard:chat-verified"
_ROUTE = "POST /api/session-control/autocompact"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    # No crew default: the session follows the global.
    state.sessions.agent_autocompact_default = MagicMock(return_value=None)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    state.conversation_log.append(slot_history_key(target), "user", "hello")
    state.conversation_log.append(slot_history_key(caller), "user", "hi")
    if created:
        target._created_by = caller.key
    return state, caller, target


def _call(state, caller, target: str = "chat-2", **kw) -> dict:
    return asyncio.run(
        sc.autocompact_target(
            state, caller_session_key=slot_history_key(caller), target=target, **kw
        )
    )


def _durable(state, slot):
    meta = state.conversation_log._read_metadata(slot_history_key(slot)) or {}
    return meta.get("autocompact_pct")


# ── Scope ────────────────────────────────────────────────────────────────────


def test_sets_a_session_the_caller_created(tmp_path):
    state, caller, target = _pair(tmp_path)
    out = _call(state, caller, pct=40)
    assert out["ok"] is True and out["pct"] == 40.0
    assert target.autocompact_pct == 40.0
    assert _durable(state, target) == 40.0
    state.sessions.set_autocompact_pct.assert_called_with(effective_session_key(target), 40.0)


def test_a_session_the_caller_did_not_create_is_refused_even_unfenced(tmp_path):
    """The creator check runs for every caller class, not only fenced ones."""
    state, caller, target = _pair(tmp_path, created=False)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=40)
    assert exc.value.code == "not_creator"
    assert target.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_not_called()


def test_the_read_is_scoped_like_the_write(tmp_path):
    state, caller, _target = _pair(tmp_path, created=False)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller)
    assert exc.value.code == "not_creator"


def test_the_caller_may_address_itself(tmp_path):
    state, caller, _target = _pair(tmp_path, created=False)
    out = _call(state, caller, target="chat-1", pct=85)
    assert out["target"] == "chat-1" and caller.autocompact_pct == 85.0


def test_self_addressing_mid_turn_does_not_touch_the_turn(tmp_path):
    """The override feeds the between-turn check only; nothing here stops a turn."""
    state, caller, _target = _pair(tmp_path)
    turn = MagicMock()
    turn.done.return_value = False
    caller.task = turn
    assert caller.running is True
    _call(state, caller, target="chat-1", pct=70)
    assert caller.running is True and caller.autocompact_pct == 70.0
    turn.cancel.assert_not_called()


def test_a_relay_archive_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.executor = "remote"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=40)
    assert exc.value.code == sc.RELAY_ARCHIVE_CODE
    assert target.autocompact_pct is None


# ── Values ───────────────────────────────────────────────────────────────────


def test_read_reports_override_defaults_and_range(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.autocompact_pct = 33.0
    out = _call(state, caller)
    assert out["pct"] == 33.0
    assert out["min"] == AUTOCOMPACT_PCT_MIN and out["max"] == AUTOCOMPACT_PCT_MAX
    assert {"global_pct", "agent_pct", "agent"} <= out.keys()
    state.sessions.set_autocompact_pct.assert_not_called()


def test_null_clears_the_override(tmp_path):
    state, caller, target = _pair(tmp_path)
    _call(state, caller, pct=33)
    out = _call(state, caller, pct=None)
    assert out["pct"] is None and target.autocompact_pct is None
    assert _durable(state, target) is None
    state.sessions.set_autocompact_pct.assert_called_with(effective_session_key(target), None)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (AUTOCOMPACT_PCT_MAX + 1, "pct_out_of_range"),
        (AUTOCOMPACT_PCT_MIN - 1, "pct_out_of_range"),
        (True, "pct_not_a_number"),
        ("50", "pct_not_a_number"),
        (float("nan"), "pct_not_finite"),
        (10**400, "pct_not_finite"),
    ],
)
def test_invalid_values_are_refused_before_the_gate(tmp_path, monkeypatch, raw, code):
    state, caller, target = _pair(tmp_path)
    gate = MagicMock()
    monkeypatch.setattr(sc, "authorize_target", gate)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=raw)
    assert exc.value.code == code and exc.value.status == 400
    gate.assert_not_called()
    assert target.autocompact_pct is None


def test_the_range_refusal_names_the_limits(tmp_path):
    state, caller, _target = _pair(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=AUTOCOMPACT_PCT_MAX + 1)
    assert f"{AUTOCOMPACT_PCT_MIN:g}" in exc.value.message
    assert f"{AUTOCOMPACT_PCT_MAX:g}" in exc.value.message


# ── Where the write linearizes ───────────────────────────────────────────────


def test_a_refusal_at_the_in_lock_gate_writes_nothing(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    _call(state, caller, pct=30)
    real = sc.authorize_target
    calls = {"n": 0}

    def _second_refuses(*args, **kwargs):
        calls["n"] += 1
        # Call 1 is the up-front gate, call 2 the re-check under the lock.
        if calls["n"] >= 2:
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _second_refuses)
    state.sessions.set_autocompact_pct.reset_mock()
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=60)
    assert exc.value.code == "linked_session_target"
    assert target.autocompact_pct == 30.0
    assert _durable(state, target) == 30.0
    state.sessions.set_autocompact_pct.assert_not_called()


def test_a_revocation_during_the_save_does_not_turn_a_commit_into_a_refusal(tmp_path, monkeypatch):
    """The write linearizes at the in-lock gate. A creator change landing while
    the save is in flight is ordered after it: the commit stands, the live
    override follows it, and disk and live agree, so nothing is ever undone
    after a durable commit."""
    state, caller, target = _pair(tmp_path)
    real_save = chat_handlers.save_slot_off_loop

    async def _save_then_revoke(*args, **kwargs):
        applied = await real_save(*args, **kwargs)
        target._created_by = "chat-someone-else"
        return applied

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save_then_revoke)
    out = _call(state, caller, pct=45)
    assert out["ok"] is True and out["pct"] == 45.0
    assert target.autocompact_pct == 45.0
    assert _durable(state, target) == 45.0
    state.sessions.set_autocompact_pct.assert_called_with(effective_session_key(target), 45.0)


def test_the_slot_route_still_rechecks_after_the_commit(tmp_path):
    """The shared transaction keeps the slot route's own post-commit check."""
    state, _caller, target = _pair(tmp_path)
    seen = {"n": 0}

    def _reauthorize():
        seen["n"] += 1
        if seen["n"] >= 2:
            return chat_handlers.web.json_response({"error": "no", "code": "x"}, status=403)
        return None

    resp = asyncio.run(
        chat_handlers.commit_slot_autocompact(
            state, target, target.key, 40.0, reauthorize=_reauthorize
        )
    )
    assert resp.status == 403 and seen["n"] == 2
    assert target.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_not_called()


# ── The route ────────────────────────────────────────────────────────────────


def _request(state, body: dict, *, internal: bool = True):
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/autocompact"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.query = {}

    async def _json():
        return body

    request.json = _json
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )
    return request


def test_the_route_refuses_a_cookie_only_caller(tmp_path):
    state, _caller, target = _pair(tmp_path)
    req = _request(state, {"target": "chat-2", "pct": 40}, internal=False)
    resp = asyncio.run(handlers_sc.api_session_control_autocompact(req))
    assert resp.status == 403
    assert target.autocompact_pct is None


def test_the_route_reads_without_pct_and_writes_with_it(tmp_path):
    state, _caller, target = _pair(tmp_path)
    read = asyncio.run(
        handlers_sc.api_session_control_autocompact(_request(state, {"target": "chat-2"}))
    )
    assert read.status == 200 and json.loads(read.body.decode())["pct"] is None
    state.sessions.set_autocompact_pct.assert_not_called()
    wrote = asyncio.run(
        handlers_sc.api_session_control_autocompact(
            _request(state, {"target": "chat-2", "pct": 55})
        )
    )
    assert wrote.status == 200 and target.autocompact_pct == 55.0


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/autocompact" in _STRICT_INTERNAL_API_PATHS


# ── The MCP tool ─────────────────────────────────────────────────────────────


def _tool(args: dict, reply: dict, caller: str = _VERIFIED) -> tuple[str, list[DashboardRequest]]:
    dash = InMemoryDashboardClient({_ROUTE: reply})
    out = TABLE.call("session_set_autocompact", args, ToolContext(dash, Caller.strict(caller)))
    return out, dash.requests


def test_the_tool_omits_pct_to_read():
    out, (post,) = _tool(
        {"target": "chat-2"},
        {"ok": True, "target": "chat-2", "pct": None, "global_pct": 80.0, "min": 5.0, "max": 90.0},
    )
    assert post.path == "/api/session-control/autocompact"
    assert post.body == {"target": "chat-2"}
    assert post.session_key == _VERIFIED
    assert "follows the global default (80%)" in out and "5-90" in out


def test_the_tool_names_the_crew_default_when_one_applies():
    out, _ = _tool(
        {"target": "chat-2"},
        {"ok": True, "target": "chat-2", "pct": None, "global_pct": 80.0, "agent_pct": 60.0},
    )
    assert "follows its crew's default (60%)" in out


def test_the_tool_sends_null_to_clear():
    out, (post,) = _tool(
        {"target": "chat-2", "pct": None},
        {"ok": True, "target": "chat-2", "pct": None, "global_pct": 80.0},
    )
    assert post.body == {"target": "chat-2", "pct": None}
    assert out.startswith("Cleared")


def test_the_tool_reports_a_set():
    out, _ = _tool(
        {"target": "chat-2", "pct": 40},
        {"ok": True, "target": "chat-2", "pct": 40.0, "global_pct": 80.0, "min": 5.0, "max": 90.0},
    )
    assert out == "Set `chat-2`'s auto-compact threshold to 40% (allowed range 5-90)."


@pytest.mark.parametrize("args", [{"target": "chat-2", "pct": 40}, {"target": "chat-2"}])
def test_a_channel_caller_is_refused_at_dispatch_and_audited(args):
    """An auto-approved call never reaches the permission-prompt name match, so
    the refusal is here."""
    sel_obj = MagicMock()
    with patch("kiro_crew.mcp_dashboard.sel", return_value=sel_obj):
        out, requests = _tool(args, {"ok": True}, caller="channel:slack:C1.100")
    assert out.startswith("Error:") and "channel agents" in out
    assert requests == []
    sel_obj.log_tool_invocation.assert_called_once()
    assert sel_obj.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_blocked_tool"


def test_the_tool_reports_a_refusal_as_an_error():
    out, _ = _tool({"target": "chat-2", "pct": 40}, {"error": "not yours"})
    assert out == "Error: could not change that session's threshold: not yours"


def test_the_channel_block_lists_the_tool():
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

    assert "session_set_autocompact" in CHANNEL_AGENT_BLOCKED_TOOLS
