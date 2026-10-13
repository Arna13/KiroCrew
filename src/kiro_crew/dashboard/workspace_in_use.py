"""Which open chats hold a workspace, for the workspace update and delete routes.

A chat slot keeps the ``project`` directory it was given when its workspace was
picked, and re-picking the same workspace does not recompute it. Re-pointing or
removing the workspace under it would leave the slot's cwd and its workspace
entry disagreeing, so those writes are refused while any slot holds it.
"""

from __future__ import annotations

from aiohttp import web

_UNTITLED = "Untitled chat"


def chats_using_workspace(request: web.Request, name: str) -> list[str]:
    """Titles of the owner's open chats bound to workspace *name*.

    Only chats the owner can close from the sidebar count. An app's own worker
    (``_app`` set), a cron run and a Slack/Discord/Webex thread
    (``channel_origin``) are left out: there is no tab for the owner to close,
    so listing them would make the refusal one nobody can clear.
    """
    state = request.app.get("state")
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return []
    titles = []
    for slot in list(slots.values()):
        if getattr(slot, "_app", "") or getattr(slot, "channel_origin", False):
            continue
        if getattr(slot, "_origin", "") == "cron":
            continue
        if str(getattr(slot, "workspace", "") or "default") != name:
            continue
        title = str(getattr(slot, "title", "") or "")
        # A slot with no title carries its key as the title; the key is an id
        # the owner never sees, so name it the way the sidebar does.
        titles.append(title if title and title != str(slot.key) else _UNTITLED)
    return titles


def workspace_in_use_response(name: str, titles: list[str], action: str) -> web.Response:
    """409 naming the chats that hold the workspace, so the owner can close them."""
    # Five names keep the message one line.
    shown = titles[:5]
    more = len(titles) - len(shown)
    listed = ", ".join(shown) + (f" and {more} more" if more > 0 else "")
    return web.json_response(
        {
            "error": f"Workspace '{name}' is in use by open chats: {listed}. "
            f"Close them first, then {action}.",
            "code": "workspace_in_use",
        },
        status=409,
    )
