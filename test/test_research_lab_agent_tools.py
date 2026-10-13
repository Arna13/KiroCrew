"""The Research Lab agent surface: two tools, two routes, and nothing that starts work.

A chat agent asked for deep research files it as a Research Lab campaign through
``research_lab_create_campaign`` and reads it back through
``research_lab_campaign_status``. What is worth pinning is the boundary, not the
Lab's own campaign mechanics (``test_auto_research.py`` owns those):

* the tools exist for a model only while the app is enabled AND the owner turned
  ``research_lab.agent_create_campaigns`` on; with the setting off the tool list
  and the API are what they are without this feature;
* the tool side reaches exactly two (method, path) pairs, and the gateway admits
  the internal secret on exactly those two routes, so start, pause, nudge,
  add-question and delete stay dashboard-only;
* a created campaign is stored not started and marked as created by an agent;
* the call carries the calling session's own attested identity, from a turn the
  person sent in the dashboard;
* the status answer is a fixed, redacted and capped projection.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.auto_research import agent_api
from kiro_crew.apps.builtins.auto_research import handlers as lab_handlers
from kiro_crew.apps.builtins.auto_research.campaign import (
    agent_mode,
    lifecycle,
    storage,
    workflow_mode,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import ResearchLabConfig
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.mcp_tools import apps as apps_tools
from kiro_crew.mcp_tools import build_tool_list
from kiro_crew.validation import (
    RESEARCH_LAB_AGENT_CALLS,
    RESEARCH_LAB_CAMPAIGN_STATUS_SCHEMA,
    RESEARCH_LAB_CREATE_CAMPAIGN_SCHEMA,
    RESEARCH_LAB_CREATE_PATH,
    RESEARCH_LAB_STATUS_PATH,
    ValidationError,
    validate_tool_args,
)

RESEARCH_LAB_TOOLS = frozenset({"research_lab_create_campaign", "research_lab_campaign_status"})
QUESTION = "How do vector databases handle deletes without rebuilding the index?"
SLOT = "tab-research"
SESSION = f"dashboard:{SLOT}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_lab(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    """The campaign DB and research dir live in this test's tmp tree."""
    _floor_monkeypatch.setattr(storage, "DB_PATH", tmp_path / "campaigns.db")
    _floor_monkeypatch.setattr(storage, "RESEARCH_DIR", tmp_path / "research")
    return tmp_path


@pytest.fixture(autouse=True)
def _nothing_launches(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Any launch of a research worker or workflow fails the test that caused it."""

    async def _forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a Research Lab campaign was started")

    _floor_monkeypatch.setattr(agent_mode, "_launch_loop", _forbidden)
    _floor_monkeypatch.setattr(agent_mode, "_prepare_loop_launch", _forbidden)
    _floor_monkeypatch.setattr(workflow_mode, "_launch_workflow", _forbidden)


def _pin_setting(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    pinned = KiroCrewConfig(research_lab=ResearchLabConfig(agent_create_campaigns=on))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: pinned))


def _write_installed(name: str, *, enabled: bool) -> None:
    from kiro_crew.apps import manager as apps_manager

    path = apps_manager.app_dir(name) / apps_manager.INSTALLED_META_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "enabled": enabled}), encoding="utf-8")


@pytest.fixture()
def opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The posture the route tests start from: app enabled, setting on."""
    _pin_setting(monkeypatch, on=True)
    monkeypatch.setattr(agent_api, "is_app_enabled", lambda _name: True)


def _rows() -> list[dict[str, Any]]:
    path = storage.db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM campaigns")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class _State:
    """The slice of ``DashboardState`` the caller check reads."""

    def __init__(self) -> None:
        self._slots: dict[str, _ChatSlot] = {}

    def get_slot(self, name: str) -> _ChatSlot | None:
        return self._slots.get(name)

    def open(self, key: str = SLOT, *, user_sent: bool = True) -> _ChatSlot:
        slot = _ChatSlot(key)
        slot._turn_user_sent = user_sent
        self._slots[key] = slot
        return slot


@web.middleware
async def _fake_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    """Set exactly the attributes the real ``token_auth_middleware`` sets."""
    who = request.headers.get("X-Test-Auth", "")
    if who == "internal":
        request["internal_auth"] = True
        request["peer_verified"] = True
    elif who == "internal-unattested":
        request["internal_auth"] = True
    elif who == "internal-app":
        request["internal_auth"] = True
        request["peer_verified"] = True
        request["app"] = "some-app"
    elif who == "cookie":
        request["user"] = "owner"
    return await handler(request)


def _serve(coro_fn: Any, state: _State | None = None, *, lab_routes: bool = False) -> Any:
    """Serve the agent routes (and optionally the Lab's own) in-process."""

    async def _main() -> Any:
        app = web.Application(middlewares=[_fake_auth])
        app["state"] = state if state is not None else _State()
        agent_api.register_agent_routes(app)
        if lab_routes:
            base = "/api/apps/auto-research/campaigns"
            app.router.add_post(base, lab_handlers._handle_create)
            app.router.add_get(base, lab_handlers._handle_list)
            app.router.add_get(base + "/{id}", lab_handlers._handle_get)
            app.router.add_patch(base + "/{id}", lab_handlers._handle_action)
            app.router.add_delete(base + "/{id}", lab_handlers._handle_delete)
            app.router.add_post(base + "/{id}/nudge", lab_handlers._handle_nudge)
            app.router.add_post(base + "/{id}/questions", lab_handlers._handle_add_question)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await coro_fn(client)
        finally:
            await client.close()

    return asyncio.run(_main())


def _agent_headers(session: str = SESSION, auth: str = "internal") -> dict[str, str]:
    return {"X-Test-Auth": auth, "X-Session-Key": session}


def _create(
    body: Any, *, state: _State | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    if state is None:
        state = _State()
        state.open()

    async def _go(client: TestClient) -> tuple[int, dict[str, Any]]:
        resp = await client.post(
            RESEARCH_LAB_CREATE_PATH,
            json=body,
            headers=headers if headers is not None else _agent_headers(),
        )
        return resp.status, await resp.json()

    return _serve(_go, state)


def _status(
    campaign_id: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    async def _go(client: TestClient) -> tuple[int, dict[str, Any]]:
        resp = await client.get(
            RESEARCH_LAB_STATUS_PATH,
            params={"id": campaign_id},
            headers=headers if headers is not None else _agent_headers(),
        )
        return resp.status, await resp.json()

    return _serve(_go)


# ---------------------------------------------------------------------------
# The owner gate: what tools/list carries
# ---------------------------------------------------------------------------


def _listed() -> set[str]:
    return {t["name"] for t in build_tool_list()}


def test_app_disabled_leaves_tools_list_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the app disabled, the listing is exactly the listing without these tools."""
    _pin_setting(monkeypatch, on=True)
    _write_installed("auto-research", enabled=False)
    listed = build_tool_list()
    assert {t["name"] for t in listed} & RESEARCH_LAB_TOOLS == set()
    monkeypatch.setattr(
        apps_tools,
        "schemas",
        lambda real=apps_tools.schemas: [s for s in real() if s["name"] not in RESEARCH_LAB_TOOLS],
    )
    assert build_tool_list() == listed


def test_enabling_the_app_alone_gives_agents_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app switch is the user's Lab; only the agent setting opens the tools."""
    _pin_setting(monkeypatch, on=False)
    _write_installed("auto-research", enabled=True)
    listed = build_tool_list()
    assert {t["name"] for t in listed} & RESEARCH_LAB_TOOLS == set()
    monkeypatch.setattr(
        apps_tools,
        "schemas",
        lambda real=apps_tools.schemas: [s for s in real() if s["name"] not in RESEARCH_LAB_TOOLS],
    )
    assert build_tool_list() == listed


def test_app_enabled_and_setting_on_lists_both_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_setting(monkeypatch, on=True)
    _write_installed("auto-research", enabled=True)
    assert RESEARCH_LAB_TOOLS <= _listed()


def test_setting_on_with_the_app_disabled_lists_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_setting(monkeypatch, on=True)
    _write_installed("auto-research", enabled=False)
    assert _listed() & RESEARCH_LAB_TOOLS == set()


def test_an_unreadable_setting_hides_the_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting IS the opt-in, so a read fault keeps the tools hidden."""

    def _boom(cls: Any) -> Any:
        raise OSError("config.json is unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    _write_installed("auto-research", enabled=True)
    assert _listed() & RESEARCH_LAB_TOOLS == set()
    assert agent_api.agent_campaigns_allowed() is False


def test_the_setting_defaults_off_and_reads_from_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert KiroCrewConfig().research_lab.agent_create_campaigns is False
    stored = tmp_path / "config.json"
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
    stored.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    assert KiroCrewConfig.load().research_lab.agent_create_campaigns is False
    stored.write_text(
        json.dumps({"research_lab": {"agent_create_campaigns": True}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().research_lab.agent_create_campaigns is True
    assert KiroCrewConfig.load().to_dict()["research_lab"] == {"agent_create_campaigns": True}


# ---------------------------------------------------------------------------
# The setting off: the API is what it is without this feature
# ---------------------------------------------------------------------------


def test_setting_off_answers_404_and_creates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_setting(monkeypatch, on=False)
    monkeypatch.setattr(agent_api, "is_app_enabled", lambda _name: True)
    status, body = _create({"question": QUESTION})
    assert status == 404
    assert body["code"] == "agent_campaigns_off"
    assert _rows() == []
    status, body = _status("abcd1234")
    assert status == 404
    assert body["code"] == "agent_campaigns_off"


# ---------------------------------------------------------------------------
# Create: not started, marked, nothing else accepted
# ---------------------------------------------------------------------------


def test_create_stores_a_not_started_campaign_marked_as_the_agents(opted_in: None) -> None:
    status, body = _create({"question": QUESTION})
    assert status == 201, body
    assert body["ok"] is True
    assert body["status"] == "ready"
    assert body["started"] is False
    assert body["link"] == f"/auto-research?campaign={body['id']}"
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == body["id"]
    assert row["status"] == "ready"
    assert row["started_at"] is None
    assert row["created_by"] == "agent"
    assert row["execution_mode"] == "agent"
    # Everything the agent did not name is the Lab's own default.
    assert row["auto_approve"] == 0
    assert row["parallel_workers"] == 1
    assert row["model"] == ""
    status_file = storage._campaign_dir(body["id"]) / "status.json"
    if status_file.exists():
        assert json.loads(status_file.read_text(encoding="utf-8"))["status"] == "ready"


def test_create_accepts_workflow_mode(opted_in: None) -> None:
    status, body = _create({"question": QUESTION, "mode": "workflow"})
    assert status == 201, body
    assert _rows()[0]["execution_mode"] == "workflow"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"question": QUESTION, "max_cycles": 100}, "unknown_field"),
        ({"question": QUESTION, "auto_approve": True}, "unknown_field"),
        ({"question": QUESTION, "status": "running"}, "unknown_field"),
        ({"question": QUESTION, "created_by": None}, "unknown_field"),
        ({"question": "too short"}, "invalid_question"),
        ({"question": "x" * (agent_api.MAX_QUESTION_LEN + 1)}, "invalid_question"),
        ({"question": 42}, "invalid_question"),
        ({}, "invalid_question"),
        ({"question": QUESTION, "mode": "turbo"}, "invalid_mode"),
    ],
)
def test_create_refuses_anything_but_a_question_and_a_mode(
    opted_in: None, body: dict[str, Any], code: str
) -> None:
    status, payload = _create(body)
    assert status == 400
    assert payload["code"] == code
    assert _rows() == []


def test_create_refuses_while_another_campaign_is_active(opted_in: None) -> None:
    lifecycle.create_campaign({"question": QUESTION})
    first = _rows()[0]["id"]
    lifecycle.update_campaign_status(first, storage.CampaignStatus.RUNNING)
    status, body = _create({"question": QUESTION})
    assert status == 409
    assert body["code"] == "campaign_refused"
    assert len(_rows()) == 1


def test_a_create_that_left_a_campaign_running_is_reported_failed(
    opted_in: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route checks the stored state, not the create helper's word for it."""
    real = lifecycle.create_campaign

    def _runs(config: dict[str, Any], **kw: Any) -> dict[str, Any]:
        result = real(config, **kw)
        lifecycle.update_campaign_status(result["id"], storage.CampaignStatus.RUNNING)
        return result

    monkeypatch.setattr(lifecycle, "create_campaign", _runs)
    status, body = _create({"question": QUESTION})
    assert status == 500
    assert body["code"] == "unexpected_state"


def test_the_provenance_marker_is_not_a_config_key() -> None:
    """The dashboard route passes its whole body as config; a body cannot claim it."""
    result = lifecycle.create_campaign({"question": QUESTION, "created_by": "agent"})
    assert _rows()[0]["created_by"] is None
    assert result["status"] == "ready"
    with pytest.raises(ValueError):
        lifecycle.create_campaign({"question": QUESTION}, created_by="someone")


# ---------------------------------------------------------------------------
# Who may call: the caller's own attested session, in a turn the person sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"X-Test-Auth": "cookie", "X-Session-Key": SESSION}, 403, "internal_secret_required"),
        (_agent_headers(auth="internal-app"), 403, "app_caller_refused"),
        ({"X-Test-Auth": "internal"}, 403, "session_required"),
        (_agent_headers(session="dashboard:ui"), 403, "session_required"),
        (_agent_headers(session="cron:abc123"), 403, "unattended_caller_refused"),
        (_agent_headers(session="subagent:abc123"), 403, "unattended_caller_refused"),
        (_agent_headers(auth="internal-unattested"), 403, "unattested_caller"),
        (_agent_headers(session="dashboard:tab-gone"), 409, "no_live_slot"),
    ],
)
def test_create_refuses_a_caller_that_is_not_an_attested_chat(
    opted_in: None, headers: dict[str, str], status: int, code: str
) -> None:
    got_status, body = _create({"question": QUESTION}, headers=headers)
    assert (got_status, body["code"]) == (status, code)
    assert _rows() == []


def test_create_refuses_a_turn_the_person_did_not_send(opted_in: None) -> None:
    """A loop wake, cron or app injection, or a session_send is not the person's yes."""
    state = _State()
    state.open(user_sent=False)
    status, body = _create({"question": QUESTION}, state=state)
    assert (status, body["code"]) == (403, "not_user_turn")
    assert _rows() == []


def test_create_refuses_a_channel_turn(opted_in: None) -> None:
    state = _State()
    state.open()._turn_channel_origin = True
    status, body = _create({"question": QUESTION}, state=state)
    assert (status, body["code"]) == (403, "channel_caller")


def test_create_refuses_an_app_owned_slot(opted_in: None) -> None:
    state = _State()
    state.open()._app = "auto-research"
    status, body = _create({"question": QUESTION}, state=state)
    assert (status, body["code"]) == (403, "unattended_caller_refused")


def test_a_non_local_caller_is_refused(opted_in: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``local_only=False`` deployment reclassifies strict paths as mixed."""
    monkeypatch.setattr("kiro_crew.dashboard.origin.is_loopback", lambda _remote: False)
    monkeypatch.setattr("kiro_crew.dashboard.origin.request_is_unix_socket", lambda _r: False)
    status, body = _create({"question": QUESTION})
    assert (status, body["code"]) == (403, "loopback_only")


def test_a_disabled_app_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_setting(monkeypatch, on=True)
    monkeypatch.setattr(agent_api, "is_app_enabled", lambda _name: False)
    status, body = _create({"question": QUESTION})
    assert (status, body["code"]) == (403, "app_not_enabled")
    assert _rows() == []


# ---------------------------------------------------------------------------
# Status: fixed, redacted, capped
# ---------------------------------------------------------------------------


def test_status_is_a_fixed_redacted_capped_projection(opted_in: None) -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    result = lifecycle.create_campaign(
        {
            "question": f"{QUESTION} with key {secret}",
            "sub_questions": [{"text": "kept in the Lab"}],
            "sources": ["https://example.invalid/private"],
        },
        created_by=lifecycle.CREATED_BY_AGENT,
    )
    cid = result["id"]
    lifecycle.update_campaign_status(
        cid, storage.CampaignStatus.FAILED, error_message="boom " + "e" * 5000
    )
    status, body = _status(cid)
    assert status == 200, body
    view = body["campaign"]
    assert set(view) == {
        "id",
        "name",
        "status",
        "created_by",
        "question",
        "execution_mode",
        "total_cycles",
        "max_cycles",
        "created_at",
        "started_at",
        "completed_at",
        "error_message",
        "pending_question",
        "cycles_reported",
        "cycles_with_new_findings",
        "latest_summary",
    }
    assert view["created_by"] == "agent"
    assert secret not in json.dumps(body)
    assert len(view["error_message"]) <= agent_api.STATUS_TEXT_CAP + 1
    assert body["link"] == f"/auto-research?campaign={cid}"


@pytest.mark.parametrize(("cid", "status", "code"), [("nothex!!", 400, "invalid_campaign_id")])
def test_status_refuses_a_malformed_id(opted_in: None, cid: str, status: int, code: str) -> None:
    got, body = _status(cid)
    assert (got, body["code"]) == (status, code)


def test_status_of_an_unknown_campaign_is_404(opted_in: None) -> None:
    got, body = _status("abcd1234")
    assert (got, body["code"]) == (404, "not_found")


def test_status_needs_an_attested_session_but_not_a_person_turn(opted_in: None) -> None:
    """A status read is harmless, so a monitor loop may check on a campaign."""
    cid = lifecycle.create_campaign({"question": QUESTION})["id"]
    got, _body = _status(cid)
    assert got == 200
    got, body = _status(cid, headers=_agent_headers(auth="internal-unattested"))
    assert (got, body["code"]) == (403, "unattested_caller")


# ---------------------------------------------------------------------------
# The two-route admission
# ---------------------------------------------------------------------------


def test_register_routes_mounts_exactly_the_two_agent_routes() -> None:
    app = web.Application()
    agent_api.register_agent_routes(app)
    mounted = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    assert mounted == set(RESEARCH_LAB_AGENT_CALLS)
    assert agent_api.AGENT_ROUTES == RESEARCH_LAB_AGENT_CALLS


def test_the_lab_route_hook_mounts_both_route_families() -> None:
    import kiro_crew.apps.builtins.auto_research as pkg

    app = web.Application()
    pkg.register_routes(app)
    mounted = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    assert set(RESEARCH_LAB_AGENT_CALLS) <= mounted
    assert ("PATCH", "/api/apps/auto-research/campaigns/{id}") in mounted


def test_only_the_two_agent_paths_are_admitted_to_the_internal_secret() -> None:
    """Every other Research Lab route stays dashboard-only at the middleware too."""
    import kiro_crew.apps.builtins.auto_research as pkg
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import internal_path_matches

    app = web.Application()
    pkg.register_routes(app)
    internal = _STRICT_INTERNAL_API_PATHS | _MIXED_INTERNAL_API_PATHS
    admitted = set()
    for route in app.router.routes():
        path = route.resource.canonical.replace("{id}", "abcd1234")
        if internal_path_matches(path, internal):
            admitted.add(route.resource.canonical)
    assert admitted == {RESEARCH_LAB_CREATE_PATH, RESEARCH_LAB_STATUS_PATH}
    assert {RESEARCH_LAB_CREATE_PATH, RESEARCH_LAB_STATUS_PATH} <= _STRICT_INTERNAL_API_PATHS
    for prefix in ("/api/apps/auto-research", "/api/apps/auto-research/agent"):
        assert prefix not in internal
        assert not internal_path_matches(prefix, internal)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/api/apps/auto-research/campaigns/abcd1234", {"action": "start"}),
        ("PATCH", "/api/apps/auto-research/campaigns/abcd1234", {"action": "pause"}),
        ("POST", "/api/apps/auto-research/campaigns/abcd1234/questions", {"text": "more"}),
        ("POST", "/api/apps/auto-research/campaigns/abcd1234/nudge", {"text": "go"}),
        ("POST", "/api/apps/auto-research/campaigns", {"question": QUESTION}),
        ("GET", "/api/apps/auto-research/campaigns/abcd1234", None),
        ("GET", "/api/apps/auto-research/campaigns", None),
        ("DELETE", "/api/apps/auto-research/campaigns/abcd1234", None),
    ],
)
def test_a_lab_route_refuses_an_internal_secret_caller(
    opted_in: None, method: str, path: str, body: Any
) -> None:
    """The Lab's own handlers admit only the dashboard user the cookie path sets."""

    async def _go(client: TestClient) -> int:
        resp = await client.request(method, path, json=body, headers=_agent_headers())
        return resp.status

    assert _serve(_go, lab_routes=True) == 401
    assert _rows() == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", RESEARCH_LAB_CREATE_PATH),
        ("PATCH", RESEARCH_LAB_CREATE_PATH),
        ("POST", RESEARCH_LAB_STATUS_PATH),
        ("DELETE", RESEARCH_LAB_STATUS_PATH),
    ],
)
def test_the_agent_routes_take_only_their_own_method(
    opted_in: None, method: str, path: str
) -> None:
    async def _go(client: TestClient) -> int:
        resp = await client.request(method, path, headers=_agent_headers())
        return resp.status

    assert _serve(_go) == 405


# ---------------------------------------------------------------------------
# The tool side
# ---------------------------------------------------------------------------


class _Wire:
    """Records every transport call the tools make."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, Any, str | None]] = []

    def post(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("POST", path, body, kw.get("session_key")))
        return dict(self.reply)

    def get(self, path: str, session_key: str | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("GET", path, None, session_key))
        return dict(self.reply)


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    w = _Wire({"ok": True, "id": "abcd1234", "name": "Vector deletes", "status": "ready"})
    monkeypatch.setattr(mcp_core, "_post", w.post)
    monkeypatch.setattr(mcp_core, "_get", w.get)
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *a, **k: (SESSION, ""))
    return w


def test_the_tool_allowlist_is_exactly_two_calls() -> None:
    assert RESEARCH_LAB_AGENT_CALLS == frozenset(
        {("POST", RESEARCH_LAB_CREATE_PATH), ("GET", RESEARCH_LAB_STATUS_PATH)}
    )
    assert apps_tools._RESEARCH_LAB_CREATE_URL == RESEARCH_LAB_CREATE_PATH
    assert apps_tools._RESEARCH_LAB_STATUS_URL == RESEARCH_LAB_STATUS_PATH


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/apps/auto-research/campaigns/abcd1234"),
        ("POST", "/api/apps/auto-research/campaigns/abcd1234/questions"),
        ("POST", "/api/apps/auto-research/campaigns/abcd1234/nudge"),
        ("POST", "/api/apps/auto-research/campaigns"),
        ("GET", "/api/apps/auto-research/campaigns"),
        ("GET", "/api/apps/auto-research/campaigns/abcd1234"),
        ("DELETE", "/api/apps/auto-research/campaigns/abcd1234"),
        ("GET", RESEARCH_LAB_CREATE_PATH),
        ("POST", RESEARCH_LAB_STATUS_PATH),
        ("PATCH", RESEARCH_LAB_CREATE_PATH),
    ],
)
def test_every_other_call_is_refused_before_it_is_sent(wire: _Wire, method: str, path: str) -> None:
    out = apps_tools._research_lab_call(method, path, session_key=SESSION, body={"action": "start"})
    assert out["code"] == "not_agent_surface"
    assert wire.calls == []


def test_create_tool_sends_only_the_question_and_mode_for_the_caller(wire: _Wire) -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    out = apps_tools.research_lab_create_campaign(
        "research_lab_create_campaign", {"question": f"{QUESTION} {secret}", "mode": "agent"}
    )
    ((method, path, body, session_key),) = wire.calls
    assert (method, path, session_key) == ("POST", RESEARCH_LAB_CREATE_PATH, SESSION)
    assert set(body) == {"question", "mode"}
    assert secret not in body["question"]
    assert "NOT started" in out
    assert "[Open in Research Lab](/auto-research?campaign=abcd1234)" in out
    assert agent_api.campaign_link("abcd1234") == "/auto-research?campaign=abcd1234"


def test_create_tool_without_a_strict_identity_sends_nothing(
    wire: _Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda refusal, *a, **k: ("", refusal)
    )
    out = apps_tools.research_lab_create_campaign(
        "research_lab_create_campaign", {"question": QUESTION}
    )
    assert out.startswith("Error:")
    assert wire.calls == []


def test_create_tool_surfaces_the_route_refusal(wire: _Wire) -> None:
    wire.reply = {"error": "only the turn answering a message...", "code": "not_user_turn"}
    out = apps_tools.research_lab_create_campaign(
        "research_lab_create_campaign", {"question": QUESTION}
    )
    assert out.startswith("Error: creating the Research Lab campaign failed [not_user_turn]")


def test_status_tool_is_redacted_and_capped(wire: _Wire) -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    wire.reply = {
        "ok": True,
        "campaign": {"id": "abcd1234", "latest_summary": f"{secret} " + "s" * 20_000},
    }
    out = apps_tools.research_lab_campaign_status(
        "research_lab_campaign_status", {"id": "abcd1234"}
    )
    ((method, path, _body, session_key),) = wire.calls
    assert (method, session_key) == ("GET", SESSION)
    assert path == f"{RESEARCH_LAB_STATUS_PATH}?id=abcd1234"
    assert secret not in out
    assert len(out) < apps_tools._RESEARCH_LAB_STATUS_CAP + 200


def test_the_schemas_bound_every_argument() -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"question": QUESTION, "max_cycles": 5}, RESEARCH_LAB_CREATE_CAMPAIGN_SCHEMA
        )
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"question": QUESTION, "mode": "turbo"}, RESEARCH_LAB_CREATE_CAMPAIGN_SCHEMA
        )
    with pytest.raises(ValidationError):
        validate_tool_args({"question": "x" * 4001}, RESEARCH_LAB_CREATE_CAMPAIGN_SCHEMA)
    with pytest.raises(ValidationError):
        validate_tool_args({"id": "../etc"}, RESEARCH_LAB_CAMPAIGN_STATUS_SCHEMA)
    assert validate_tool_args({"id": "abcd1234"}, RESEARCH_LAB_CAMPAIGN_STATUS_SCHEMA)["id"] == (
        "abcd1234"
    )
