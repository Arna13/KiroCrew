"""The ``exact=1`` flag of ``/api/file-read`` and ``/api/file-diff``.

The dashboard sends it only for an open it makes on the agent's behalf (the
``kirocrew:open-file`` marker). With it, the gateway opens only a regular file at
exactly the path named, judged on the open descriptor; anything else answers 422
"not opened automatically". Without it, both endpoints answer as they always have.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import api_file_read
from kiro_crew.dashboard.handlers.files import api_file_diff

needs_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="creating a symlink on Windows needs elevation"
)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-read", api_file_read)
    app.router.add_get("/api/file-diff", api_file_diff)
    return as_owner(app)


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def root(tmp_path):
    # Canonical: on macOS the temp dir sits under a symlinked /var.
    return tmp_path.resolve()


def _url(endpoint: str, path: object, *, exact: bool) -> str:
    return f"/api/{endpoint}?path={quote(str(path))}" + ("&exact=1" if exact else "")


async def _get(endpoint: str, path: object, *, exact: bool):
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(_url(endpoint, path, exact=exact))
        if resp.content_type == "application/json":
            return resp.status, await resp.json()
        return resp.status, await resp.text()


@needs_symlinks
@pytest.mark.asyncio
async def test_exact_refuses_a_symlink_to_another_file(root, mock_sel):
    target = root / "target.md"
    target.write_text("target body", encoding="utf-8")
    link = root / "note.md"
    link.symlink_to(target)

    status, body = await _get("file-read", link, exact=True)

    assert status == 422
    assert body == {"error": "not opened automatically", "code": "not_exact"}


@needs_symlinks
@pytest.mark.asyncio
async def test_without_the_flag_the_same_symlink_opens_as_today(root, mock_sel):
    target = root / "target.md"
    target.write_text("target body", encoding="utf-8")
    link = root / "note.md"
    link.symlink_to(target)

    status, body = await _get("file-read", link, exact=False)

    assert status == 200
    assert body == "target body"


@pytest.mark.asyncio
async def test_exact_opens_a_regular_file(root, mock_sel):
    note = root / "note.md"
    note.write_text("# Notes\n", encoding="utf-8")

    status, body = await _get("file-read", note, exact=True)

    assert status == 200
    assert body == "# Notes\n"


@needs_symlinks
@pytest.mark.asyncio
async def test_exact_refuses_a_file_under_a_symlinked_directory(root, mock_sel):
    real_dir = root / "real"
    real_dir.mkdir()
    (real_dir / "note.md").write_text("body", encoding="utf-8")
    (root / "alias").symlink_to(real_dir, target_is_directory=True)

    status, _ = await _get("file-read", root / "alias" / "note.md", exact=True)
    assert status == 422
    status, body = await _get("file-read", root / "alias" / "note.md", exact=False)
    assert (status, body) == (200, "body")


@pytest.mark.asyncio
async def test_exact_refuses_a_path_not_in_normal_form(root, mock_sel):
    (root / "sub").mkdir()
    (root / "note.md").write_text("body", encoding="utf-8")

    status, _ = await _get("file-read", f"{root}/sub/../note.md", exact=True)

    assert status == 422


@pytest.mark.asyncio
async def test_exact_refuses_a_directory_and_answers_a_missing_file_as_today(root, mock_sel):
    (root / "folder.md").mkdir()

    status, _ = await _get("file-read", root / "folder.md", exact=True)
    assert status == 422
    status, body = await _get("file-read", root / "absent.md", exact=True)
    assert status == 404
    assert body == {"error": "not found"}


@pytest.mark.asyncio
async def test_exact_answers_a_sensitive_path_with_the_existing_refusal(root, mock_sel):
    note = root / "note.md"
    note.write_text("body", encoding="utf-8")

    with patch("kiro_crew.dashboard.handlers.files.is_sensitive_resolved_path", return_value=True):
        status, body = await _get("file-read", note, exact=True)

    assert status == 400
    assert body == {"error": "invalid or forbidden path"}


@needs_symlinks
@pytest.mark.asyncio
async def test_exact_diff_refuses_a_symlink_and_the_plain_diff_is_unchanged(root, mock_sel):
    target = root / "target.md"
    target.write_text("target body", encoding="utf-8")
    link = root / "note.md"
    link.symlink_to(target)

    status, body = await _get("file-diff", link, exact=True)
    assert status == 422
    assert body == {"error": "not opened automatically", "code": "not_exact"}
    status, body = await _get("file-diff", link, exact=False)
    assert status == 200
    assert "code" not in body


@pytest.mark.asyncio
async def test_exact_diff_admits_a_regular_file(root, mock_sel):
    note = root / "note.md"
    note.write_text("body", encoding="utf-8")

    status, body = await _get("file-diff", note, exact=True)

    assert status == 200
    assert "code" not in body
