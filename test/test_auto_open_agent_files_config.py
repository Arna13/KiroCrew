"""Tests for the auto_open_agent_files dashboard config field.

The flag decides whether the chat page opens the file an agent's hidden
``kirocrew:open-file`` marker names in the side panel's Files tab. It is off by
default, an opt-in, because with it on the system prompt carries the marker's
line.

`/api/dashboard/config` validates each field explicitly and echoes a fixed dict,
so the field is wired in three places (PUT allowlist, validation block, GET
response); these tests cover all three, because missing any one of them leaves
the toggle unable to persist.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_auto_open_agent_files_default_false(cfg_file):
    assert KiroCrewConfig().dashboard.auto_open_agent_files is False
    # A config file that never mentions the key loads the default as well.
    assert KiroCrewConfig.load().dashboard.auto_open_agent_files is False


def test_auto_open_agent_files_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.auto_open_agent_files = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["auto_open_agent_files"] is True

    assert KiroCrewConfig.load().dashboard.auto_open_agent_files is True


@pytest.mark.parametrize("value", ["true", "maybe", 7, None, [], {}])
def test_auto_open_agent_files_non_bool_falls_back_to_default(cfg_file, value):
    # Only a real JSON boolean is read, as for the sibling dashboard switches;
    # the Settings toggle always writes one.
    cfg_file.write_text(
        json.dumps({"dashboard": {"auto_open_agent_files": value}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().dashboard.auto_open_agent_files is False


def test_auto_open_agent_files_in_generated_schema():
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"]["auto_open_agent_files"]
    assert node["type"] == "boolean"
    assert node["default"] is False
    assert node["x-meta"]["label"]
    assert node["x-meta"]["help"]


def test_auto_open_agent_files_has_a_change_card_setting():
    from kiro_crew.change_card_catalog import DASHBOARD_SETTINGS

    assert DASHBOARD_SETTINGS["chat.auto-open-agent-files-in-side-panel"] == (
        "auto_open_agent_files",
        (True, False),
    )


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


@pytest.mark.asyncio
async def test_handler_put_auto_open_agent_files_true(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"auto_open_agent_files": True})
        assert resp.status == 200
        assert KiroCrewConfig.load().dashboard.auto_open_agent_files is True


@pytest.mark.asyncio
async def test_handler_put_auto_open_agent_files_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"auto_open_agent_files": "no"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]
        assert body["code"] == "invalid_auto_open_agent_files"
    assert KiroCrewConfig.load().dashboard.auto_open_agent_files is False


@pytest.mark.asyncio
async def test_handler_get_returns_auto_open_agent_files(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        assert (await resp.json())["auto_open_agent_files"] is False
    cfg_file.write_text(
        json.dumps({"dashboard": {"auto_open_agent_files": True}}), encoding="utf-8"
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert (await resp.json())["auto_open_agent_files"] is True
