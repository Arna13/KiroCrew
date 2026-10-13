"""The ``mcp.share`` change card: one press shares a server, through the switch's path."""

from __future__ import annotations

import pytest

from kiro_crew import change_card_catalog as catalog

_EXPECTED = "a" * 64 + ":" + "b" * 64


def _snap(**over):
    base = {
        "exists": True,
        "can_share": True,
        "shared": False,
        "share_blocked": None,
        "launch_commands": ["/opt/slack --stdio"],
        "launch_env_lines": ["SLACK_TOKEN=<hidden sha256:00>"],
        "expected_launch": _EXPECTED,
        "sharing_on": True,
        "other_marked": [],
    }
    base.update(over)
    return base


def _preview(params, snapshot):
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, params)
    return catalog.build_preview(catalog.KIND_MCP_SHARE, p, snapshot, {})


def test_the_kind_is_listed_for_the_guide():
    kinds = {k["id"]: k for k in catalog.list_kinds()}
    assert "mcp.share" in kinds
    assert set(kinds["mcp.share"]["params_schema"]["required"]) == {"server", "shared"}


def test_one_press_goes_through_the_same_endpoint_the_switch_uses():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert preview["apply"] == [
        {
            "method": "POST",
            "path": "/api/mcp-gateway/servers/stub",
            "body": {"name": "slack-mcp", "stub": True, "expected_launch": _EXPECTED},
        }
    ]
    assert ("POST", "/api/mcp-gateway/servers/stub") in catalog.HOOKED_ROUTES
    assert preview["changes"][0] == {
        "label": "slack-mcp",
        "before": "One per chat",
        "after": "Shared",
    }


def test_sharing_with_the_switch_off_turns_it_on_and_names_what_else_starts():
    """A share saves nothing while sharing is off, and turning it on starts every marked server."""
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(sharing_on=False, other_marked=["chorus-mcp", "grasp-mcp"]),
    )

    assert [s["path"] for s in preview["apply"]] == [
        "/api/mcp-gateway/servers/stub",
        "/api/mcp-gateway/enable",
    ]
    assert preview["apply"][1]["body"] == {"enabled": True}
    assert {"label": "Sharing", "before": "Off", "after": "On"} in preview["changes"]
    assert {"label": "Also starts sharing", "after": "chorus-mcp, grasp-mcp"} in preview["changes"]
    assert ("POST", "/api/mcp-gateway/enable") in catalog.HOOKED_ROUTES


def test_a_moving_memory_reading_is_not_a_change_since_preview():
    """RSS moves between preview and press; the card must still apply."""
    from kiro_crew.dashboard import change_cards

    before = _snap(saves_bytes=100_000_000)
    now = _snap(saves_bytes=120_000_000)
    same = change_cards.canonical(change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, before))
    assert same == change_cards.canonical(
        change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, now)
    )
    # A real change still counts.
    moved = _snap(saves_bytes=100_000_000, expected_launch="c" * 64)
    assert same != change_cards.canonical(
        change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, moved)
    )


def test_the_connection_routes_stay_hooked():
    assert ("POST", "/api/connections/mint") in catalog.HOOKED_ROUTES
    assert ("GET", "/api/connections/mint") in catalog.HOOKED_ROUTES


def test_a_share_left_with_the_switch_off_is_not_reported_as_done():
    from kiro_crew.dashboard import change_cards

    p = {"server": "slack-mcp", "shared": True}
    assert not change_cards.after_shows_the_write(
        catalog.KIND_MCP_SHARE, p, {"shared": True, "sharing_on": False}
    )
    assert change_cards.after_shows_the_write(
        catalog.KIND_MCP_SHARE, p, {"shared": True, "sharing_on": True}
    )


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("credential_vending", "Can't share: gives each chat its own credentials"),
        ("needs_tenant_nonce", "Can't share: each chat needs its own copy"),
        ("computer_use", "Can't share: it controls this computer"),
    ],
)
def test_each_leakage_class_is_refused_with_one_line(code, line):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "creds-agent", "shared": True}, _snap(share_blocked=code))

    assert exc.value.code == "share_refused"
    assert str(exc.value) == line


def test_sharing_shows_the_launch_and_is_rated_as_running_code():
    """The shared copy runs outside the chat sandbox: show and pin what it runs."""
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert preview["risk"] == catalog.RISK_CODE_EXEC
    assert {"label": "Command", "after": "/opt/slack --stdio"} in preview["changes"]
    assert {"label": "Env", "after": "SLACK_TOKEN=<hidden sha256:00>"} in preview["changes"]


@pytest.mark.parametrize(
    "over",
    [{"expected_launch": None}, {"launch_commands": []}, {"expected_launch": ""}],
)
def test_a_launch_that_cannot_be_shown_cannot_be_shared(over):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, _snap(**over))
    assert exc.value.code == "launch_unreadable"


def test_stopping_sharing_is_tightening_and_unpinned():
    preview = _preview({"server": "slack-mcp", "shared": False}, _snap(shared=True))
    assert preview["risk"] == catalog.RISK_TIGHTEN
    assert preview["apply"][0]["body"] == {"name": "slack-mcp", "stub": False}


def test_turning_a_refused_class_off_is_allowed():
    preview = _preview(
        {"server": "creds-agent", "shared": False},
        _snap(shared=True, share_blocked="credential_vending"),
    )
    assert preview["apply"][0]["body"] == {"name": "creds-agent", "stub": False}


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snap(exists=False), "not_found"),
        (_snap(can_share=False), "not_shareable"),
        (_snap(shared=True), "no_change"),
    ],
)
def test_a_card_that_changes_nothing_is_refused(snapshot, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snapshot)
    assert exc.value.code == code


def test_the_saving_row_is_the_measured_number():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap(saves_bytes=240_000_000))

    assert {"label": "Saves", "after": "240 MB"} in preview["changes"]


def test_no_saving_row_without_a_measurement():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert all(row["label"] != "Saves" for row in preview["changes"])


def test_undo_restores_the_state_before_the_press():
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": True})
    plan, reason = catalog.build_undo(catalog.KIND_MCP_SHARE, p, {"shared": False}, [{}], 1)

    assert reason is None
    assert plan == [
        {
            "method": "POST",
            "path": "/api/mcp-gateway/servers/stub",
            "body": {"name": "slack-mcp", "stub": False},
        }
    ]


def test_undoing_stop_sharing_never_shares_again_unseen():
    """Re-sharing approves a launch outside the sandbox, so Undo does not offer it."""
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": False})
    before = {"shared": True, "expected_launch": _EXPECTED}
    plan, reason = catalog.build_undo(catalog.KIND_MCP_SHARE, p, before, [{}], 1)

    assert plan is None and reason == "reshare_needs_review"


def test_a_long_command_is_shown_whole():
    long_cmd = "/bin/sh -c " + "x" * 400 + " && suffix"
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap(launch_commands=[long_cmd]))

    assert {"label": "Command", "after": long_cmd} in preview["changes"]


def test_the_manual_toggle_is_remembered_by_name_only():
    line = catalog.describe_manual_change(
        ("POST", "/api/mcp-gateway/servers/stub"), {}, {"name": "slack-mcp", "stub": True}
    )
    assert line == "shared MCP server slack-mcp across chats"


def test_params_are_validated():
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp"})
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": True, "extra": 1}
        )


def test_the_snapshot_reads_the_same_sources_as_the_page(monkeypatch):
    from kiro_crew.dashboard import change_cards
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import direct_copies

    monkeypatch.setattr(
        mcp_mod,
        "_collect_server_rows",
        lambda: {"slack-mcp": {"transport": "stdio", "commands": {("/opt/slack",)}}},
    )
    monkeypatch.setattr(
        direct_copies,
        "measure",
        lambda commands: {"slack-mcp": direct_copies.DirectCopies(2, 200_000_000)},
    )
    monkeypatch.setattr(
        mcp_mod.launch_resolve, "resolve_launches", lambda names, refresh_specs: {"slack-mcp": ()}
    )
    monkeypatch.setattr(
        mcp_mod,
        "display_launches",
        lambda name, launches: {
            "commands": [["/opt/slack", "--stdio"]],
            "envs": [["SLACK_TOKEN=<hidden sha256:00>"]],
            "complete": True,
            "expected_launch": _EXPECTED,
        },
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp")

    assert snap["exists"] and snap["can_share"]
    assert snap["share_blocked"] is None
    assert snap["saves_bytes"] == 100_000_000
    assert snap["launch_commands"] == ["/opt/slack --stdio"]
    assert snap["launch_env_lines"] == ["SLACK_TOKEN=<hidden sha256:00>"]
    assert snap["expected_launch"] == _EXPECTED
    assert change_cards._mcp_share_snapshot("missing-mcp") == {"exists": False}


def test_every_env_value_the_card_approves_is_on_the_card():
    """A value such as LD_PRELOAD is part of what runs outside the sandbox."""
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(launch_env_lines=["LD_PRELOAD=/tmp/x.so", "API_KEY=<hidden sha256:00>"]),
    )

    assert {"label": "Env", "after": "LD_PRELOAD=/tmp/x.so"} in preview["changes"]
    assert {"label": "Env", "after": "API_KEY=<hidden sha256:00>"} in preview["changes"]


def test_hidden_characters_in_a_launch_are_escaped_for_the_card():
    from kiro_crew.dashboard import change_cards

    assert change_cards._visible("/opt/a\u202egnp.exe") == "/opt/a\\u{202e}gnp.exe"
    assert change_cards._visible("a\u200bb\tc") == "a\\u{200b}b\\x09c"
    assert change_cards._visible("plain é") == "plain é"
