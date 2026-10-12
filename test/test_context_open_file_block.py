"""`{{OPEN_FILE_BLOCK}}` resolution: the open-file marker line follows its setting.

The line teaching `<!-- kirocrew:open-file path="..." -->` reaches the system
prompt only on a dashboard surface with `dashboard.auto_open_agent_files` on.
With the setting off the slot resolves to nothing, so the prompt is byte for byte
the one the template reads with the slot deleted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROMPT = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config" / "prompt.md"
TOKEN = "{{OPEN_FILE_BLOCK}}"
MARKER = "kirocrew:open-file"
IMAGE_LINE_END = "(PNG, JPEG, GIF, WebP, BMP, SVG)."


def _resolve(prompt: str, session_key: str, *, open_files: bool) -> str:
    """Resolve ``prompt`` for ``session_key`` with the setting at ``open_files``."""
    from kiro_crew.context import ContextBuilder

    fake_cfg = SimpleNamespace(
        dashboard=SimpleNamespace(widget_density="more", auto_open_agent_files=open_files)
    )
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
        return ContextBuilder._resolve_prompt_templates(prompt, session_key, "4", computer_use=True)


def _template() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_the_template_carries_the_slot_once_at_the_end_of_the_image_line() -> None:
    text = _template()
    assert text.count(TOKEN) == 1
    assert f"{IMAGE_LINE_END}{TOKEN}\n" in text
    assert MARKER not in text


def test_setting_off_leaves_the_prompt_byte_identical_to_the_template_without_the_slot() -> None:
    text = _template()
    without_slot = text.replace(TOKEN, "")
    for key in ("dashboard:abc", "slack:C123:123.456"):
        resolved = _resolve(text, key, open_files=False)
        assert resolved.encode("utf-8") == _resolve(without_slot, key, open_files=False).encode(
            "utf-8"
        )
        assert MARKER not in resolved
        assert TOKEN not in resolved


def test_setting_on_adds_the_marker_line_after_the_image_line_on_a_dashboard() -> None:
    resolved = _resolve(_template(), "dashboard:abc", open_files=True)
    assert TOKEN not in resolved
    assert resolved.count(MARKER) == 1
    image_line_at = resolved.index(IMAGE_LINE_END)
    marker_at = resolved.index(MARKER)
    between = resolved[image_line_at + len(IMAGE_LINE_END) : marker_at]
    assert between.startswith("\n\nIn a dashboard chat, ")
    assert "absolute paths only" in resolved[marker_at:]


def test_setting_on_adds_nothing_where_no_dashboard_renders_the_reply() -> None:
    text = _template()
    for key in ("slack:C123:123.456", "cli:local", ""):
        resolved = _resolve(text, key, open_files=True)
        assert MARKER not in resolved
        assert TOKEN not in resolved


def test_the_default_config_adds_no_marker_line() -> None:
    from kiro_crew.config.sections import DashboardConfig

    default_open = DashboardConfig().auto_open_agent_files
    assert default_open is False
    resolved = _resolve(_template(), "dashboard:abc", open_files=default_open)
    assert MARKER not in resolved
    assert resolved == _resolve(_template().replace(TOKEN, ""), "dashboard:abc", open_files=False)
