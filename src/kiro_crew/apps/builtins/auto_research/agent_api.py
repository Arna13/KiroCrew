"""The agent-facing Research Lab routes: file a campaign that does not start, and read one.

WHY THIS EXISTS
---------------
A chat agent asked for deep research has no way to reach the Research Lab. The
Lab's routes need a dashboard credential, and an agent session holds none: its
cookies are httpOnly, ``KIROCREW_INTERNAL_SECRET`` is stripped from its env, and
``.local_secret`` is on the sensitive-path denylist. So the agent researched
inline, and the cycles, findings and report the Lab exists for never happened.

These two routes are the ``research_lab_create_campaign`` and
``research_lab_campaign_status`` MCP tools' path in. The MCP server process
holds the internal secret and the agent never sees one.

WHY THESE ROUTES AND NOT THE LAB'S OWN
--------------------------------------
The Lab's own create route accepts the whole campaign config (cycle budget,
worker count, attended/unattended, model, grill tree), and its
``/campaigns/{id}`` neighbourhood holds start, pause, stop, nudge, add-question
and delete. The internal-path tables match exact-or-prefix, so admitting
``/api/apps/auto-research/campaigns`` to the internal secret would admit every
one of those children too. Instead these two leaf paths are named one by one in
``server._STRICT_INTERNAL_API_PATHS``, the shape Dev Fleet's agent pod routes
use, and every Lab route stays dashboard-only.

WHAT THE AGENT CAN AND CANNOT DO
--------------------------------
* Create takes a question and an execution mode, nothing else; an unknown field
  is refused rather than ignored. The campaign is stored in the ``ready`` state
  with the Lab's defaults and ``created_by = 'agent'``, which the Research Lab
  shows on it. Nothing runs and nothing is spent until the user opens it in the
  Research Lab and presses Start. The tool cannot start, pause, resume, stop,
  nudge or delete a campaign.
* Status returns a fixed, redacted and capped projection of one campaign.

WHAT GATES IT
-------------
* ``research_lab.agent_create_campaigns`` in ``config.json``, off by default.
  Enabling the app does not by itself open these routes: while the setting is off
  they answer 404, like a path that was never registered.
* ``auto-research`` ships ``defaultEnabled: false`` and both handlers refuse while
  it is disabled.
* The caller must be a local process holding the internal secret, and the call
  must name the chat session it acts for (``X-Session-Key``). The secret is the
  transport credential every MCP tool uses and carries no identity; the session
  key is the identity, and the route acts for it alone. A caller the gateway
  attributes to an app (the Lab's own worker sessions included), a cron job or a
  subagent is refused: only a person's chat session may file a campaign.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.campaign import lifecycle, storage, untrusted
from kiro_crew.apps.builtins.auto_research.campaign.storage import CampaignStatus
from kiro_crew.apps.builtins.auto_research.session_keys import AUTO_RESEARCH_APP, is_campaign_id
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.sel import sel
from kiro_crew.validation import (
    RESEARCH_LAB_AGENT_CALLS,
    RESEARCH_LAB_CREATE_PATH,
    RESEARCH_LAB_MAX_QUESTION,
    RESEARCH_LAB_STATUS_PATH,
)

APP_NAME = AUTO_RESEARCH_APP

AGENT_CREATE_PATH = RESEARCH_LAB_CREATE_PATH
AGENT_STATUS_PATH = RESEARCH_LAB_STATUS_PATH

#: Every (method, path) pair this module mounts, shared with the MCP tools. The
#: strict internal-path table names exactly these paths and nothing else under the
#: Lab's prefix.
AGENT_ROUTES: frozenset[tuple[str, str]] = RESEARCH_LAB_AGENT_CALLS

#: Fields a create body may carry. Everything else in a campaign config keeps the
#: Lab's own default and stays the user's to change in the Lab.
CREATE_FIELDS = frozenset({"question", "mode"})

#: ``validate_campaign`` refuses a shorter question as too vague; checked here too
#: so the refusal names the field.
MIN_QUESTION_LEN = 20
#: Bounds one research question. Well above any question a person types, and keeps
#: an unbounded string out of the campaign row, its brief and every Lab render.
MAX_QUESTION_LEN = RESEARCH_LAB_MAX_QUESTION

#: Cap on one string field of the status projection. The projection is a fixed set
#: of fields, so this also bounds the whole response.
STATUS_TEXT_CAP = 1_000

_MAX_BODY_BYTES = 16_384

_Handler = Callable[[web.Request], Awaitable[web.Response]]


def campaign_link(campaign_id: str) -> str:
    """The dashboard path that opens one campaign in the Research Lab."""
    return f"/auto-research?campaign={campaign_id}"


def _audit_agent(op: str, target: str, outcome: str, *, error: str = "", session: str = "") -> None:
    """Record the decision in the Security Event Log, naming the calling session.

    The caller here is an agent rather than a person clicking a button, so the trail
    is what makes an agent-filed campaign reviewable afterwards.
    """
    sel().log_api_access(
        caller=f"agent:{session}" if session else "agent:auto-research",
        operation=f"auto_research.agent.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target,
        error=error[:200] if error else "",
    )


def _refuse(code: str, error: str, *, status: int) -> web.Response:
    return web.json_response({"ok": False, "code": code, "error": error}, status=status)


def _agent_only(handler: _Handler) -> _Handler:
    """Admit a local process holding the internal secret, on behalf of a person.

    Being listed in ``server._STRICT_INTERNAL_API_PATHS`` does not prove the secret
    was checked: with the header absent the middleware falls through to cookie auth,
    and a ``local_only=False`` deployment reclassifies strict paths as mixed. Local
    origin is the union the middleware itself uses (the gateway's AF_UNIX socket,
    where ``request.remote`` is empty, or a loopback address).

    A caller the middleware attributes to an app is refused: the internal secret
    proves the call came from inside, and the app claim says it came from an app's
    session rather than from a person's chat.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        from kiro_crew.dashboard.origin import is_loopback, request_is_unix_socket

        if not (request_is_unix_socket(request) or is_loopback(request.remote or "")):
            _audit_agent("agent_guard", request.path, "denied", error="non-local caller")
            return _refuse("loopback_only", "local callers only", status=403)
        if request.get("internal_auth") is not True:
            _audit_agent("agent_guard", request.path, "denied", error="internal secret required")
            return _refuse(
                "internal_secret_required",
                "this route is reachable only by a local Kiro Crew process holding "
                "the internal secret; use the Research Lab page instead",
                status=403,
            )
        if request.get("app"):
            _audit_agent("agent_guard", request.path, "denied", error="app caller")
            return _refuse(
                "app_caller_refused",
                "an app's session cannot file or read Research Lab campaigns",
                status=403,
            )
        return await handler(request)

    return _wrapped


def _require_enabled(handler: _Handler) -> _Handler:
    """Refuse while the Research Lab app is disabled.

    Routes are registered once at gateway startup, before any app is known to be on.
    ``is_app_enabled`` reads ``installed.json`` synchronously, so it runs off the
    event loop.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return _refuse(
                "app_not_enabled",
                "The Research Lab app is not enabled. Turn it on in the dashboard "
                "App Store before using the Research Lab tools.",
                status=403,
            )
        return await handler(request)

    return _wrapped


def agent_campaigns_allowed() -> bool:
    """Whether ``research_lab.agent_create_campaigns`` is on. Off on any read fault.

    Synchronous (the config loader reads ``config.json``): call it off the event
    loop. A fault reads as off because this switch is the opt-in itself, and the
    off position is what every install has until its owner turns it on.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return KiroCrewConfig.load().research_lab.agent_create_campaigns is True
    except Exception:
        return False


def _require_opt_in(handler: _Handler) -> _Handler:
    """Answer 404, as for an unmounted route, unless the owner opted in.

    Enabling the Research Lab app gives the dashboard user the Lab; it does not give
    chat agents a way in. With ``research_lab.agent_create_campaigns`` off (the
    default) these routes answer exactly what a path that was never registered
    answers, so the API surface is the one the app has without them.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(agent_campaigns_allowed):
            return _refuse(
                "agent_campaigns_off",
                "Not Found. Chat agents may file Research Lab campaigns only when "
                "research_lab.agent_create_campaigns is on.",
                status=404,
            )
        return await handler(request)

    return _wrapped


#: Session-key prefixes that name no person who can be asked first: a scheduled
#: job, and a subagent working for another session.
_UNATTENDED_KEY_PREFIXES = ("cron:", "subagent:")


async def _caller_session(
    request: web.Request, *, person_turn: bool
) -> tuple[str, web.Response | None]:
    """The calling session's own attested key, or the refusal to return.

    The internal secret is the transport credential every MCP tool uses: it says
    the call came from a local Kiro Crew process and nothing about who made it.
    Who made it is the ``X-Session-Key`` the MCP server sets from the calling
    session's own context, and that key counts only with the caller's own
    attestation behind it (``session_key_is_attested``: the kernel peer check on
    the Unix socket, or the signed per-session credential that verifies to that
    key). No dashboard or owner credential is involved, and the route acts for
    that one session.

    ``person_turn`` adds what filing a campaign needs: a live dashboard tab whose
    current turn is one the person sent there. A loop wake, a cron or app
    injection, a ``session_send``, a sub-agent completion and a messaging-channel
    turn are refused, so the agent can file a campaign only in the turn that
    answers the person, after it offered and they agreed.
    """
    from kiro_crew.dashboard.session_control import UNATTENDED_SLOT_PREFIXES, caller_slot_key
    from kiro_crew.member_memory_auth import session_key_is_attested

    key = (request.headers.get("X-Session-Key") or "").strip()
    if not key or key == "dashboard:ui":
        return "", _refuse(
            "session_required",
            "this route acts for a chat session and the call named none",
            status=403,
        )
    if key.startswith(_UNATTENDED_KEY_PREFIXES):
        return "", _refuse(
            "unattended_caller_refused",
            "only a chat session with a person in it may use the Research Lab tools",
            status=403,
        )
    if not await asyncio.to_thread(session_key_is_attested, request, key):
        return "", _refuse(
            "unattested_caller",
            "the calling session could not be verified",
            status=403,
        )
    if not person_turn:
        return key, None
    state = request.app.get("state")
    slot_key = caller_slot_key(state, key) if state is not None else ""
    slot = state.get_slot(slot_key) if slot_key else None
    if slot is None:
        return "", _refuse(
            "no_live_slot",
            "this session is not open in a dashboard tab, so there is no one to ask",
            status=409,
        )
    if getattr(slot, "_app", "") or slot_key.startswith(UNATTENDED_SLOT_PREFIXES):
        return "", _refuse(
            "unattended_caller_refused",
            "only a chat session with a person in it may file Research Lab campaigns",
            status=403,
        )
    if getattr(slot, "_turn_channel_origin", False) or getattr(
        slot, "_turn_channel_narrowed", False
    ):
        return "", _refuse(
            "channel_caller",
            "this message came from a messaging channel, not the dashboard",
            status=403,
        )
    if getattr(slot, "_turn_user_sent", False) is not True:
        return "", _refuse(
            "not_user_turn",
            "only the turn answering a message the user sent in the dashboard can "
            "file a campaign; offer it and wait for their reply",
            status=403,
        )
    return key, None


def _clean_create_body(body: dict[str, Any]) -> tuple[dict[str, Any], web.Response | None]:
    """The campaign config a create body names, or a 400 saying what was wrong."""
    unknown = sorted(str(k) for k in body if k not in CREATE_FIELDS)
    if unknown:
        return {}, _refuse(
            "unknown_field",
            f"unknown field(s): {', '.join(unknown)[:200]}; only 'question' and 'mode' "
            "are accepted",
            status=400,
        )
    raw_question = body.get("question")
    if not isinstance(raw_question, str):
        return {}, _refuse("invalid_question", "'question' must be a string", status=400)
    question = raw_question.strip()
    if len(question) < MIN_QUESTION_LEN:
        return {}, _refuse(
            "invalid_question",
            f"'question' is too short to research (min {MIN_QUESTION_LEN} characters)",
            status=400,
        )
    if len(question) > MAX_QUESTION_LEN:
        return {}, _refuse(
            "invalid_question",
            f"'question' is longer than {MAX_QUESTION_LEN} characters",
            status=400,
        )
    mode = body.get("mode")
    if mode is None or mode == "":
        mode = storage.DEFAULT_EXECUTION_MODE
    if mode not in storage.VALID_EXECUTION_MODES:
        return {}, _refuse(
            "invalid_mode",
            f"'mode' must be one of: {', '.join(storage.VALID_EXECUTION_MODES)}",
            status=400,
        )
    return {"question": question, "execution_mode": mode}, None


def _stored_status(campaign_id: str) -> str | None:
    db = storage._get_db()
    try:
        row = db.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    finally:
        db.close()
    return None if row is None else str(row["status"])


@_require_opt_in
@_agent_only
@_require_enabled
async def handle_agent_create(request: web.Request) -> web.Response:
    """POST /api/apps/auto-research/agent/create-campaign -- file a campaign, not started.

    The campaign is stored ``ready`` (not started) with ``created_by = 'agent'``, which
    the Research Lab shows on the campaign; starting it is the user's click there.
    """
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    session, err = await _caller_session(request, person_turn=True)
    if err is not None:
        return err
    body, err = await read_bounded_json(request, _MAX_BODY_BYTES)
    if err is not None:
        return err
    config, err = _clean_create_body(body or {})
    if err is not None:
        return err
    verdict = await asyncio.to_thread(lifecycle.validate_campaign, config)
    if not verdict["can_start"]:
        errors = [str(e) for e in verdict.get("errors") or []]
        _audit_agent("create_campaign", "*", "denied", error="; ".join(errors), session=session)
        return _refuse("campaign_refused", "; ".join(errors)[:500], status=409)
    result = await asyncio.to_thread(
        partial(lifecycle.create_campaign, config, created_by=lifecycle.CREATED_BY_AGENT)
    )
    campaign_id = str(result["id"])
    # Fail closed on the one property the tool promises: a campaign this route
    # leaves behind is never running.
    stored = await asyncio.to_thread(_stored_status, campaign_id)
    if stored != CampaignStatus.READY:
        _audit_agent(
            "create_campaign",
            campaign_id,
            "failed",
            error=f"stored status {stored}",
            session=session,
        )
        return _refuse(
            "unexpected_state",
            "the campaign was not stored in the not-started state",
            status=500,
        )
    _audit_agent("create_campaign", campaign_id, "granted", session=session)
    name = untrusted._redact_finding({"v": result["name"]})["v"]
    return web.json_response(
        {
            "ok": True,
            "id": campaign_id,
            "name": _capped(name),
            "status": CampaignStatus.READY.value,
            "started": False,
            "link": campaign_link(campaign_id),
        },
        status=201,
    )


def _capped(value: Any) -> Any:
    """A string cut to ``STATUS_TEXT_CAP``; any other value unchanged.

    Every string reaching here is already redacted (``storage.get_campaign`` and the
    findings reader redact at the source), so the cut cannot leave half a credential
    that a later pattern would miss.
    """
    if isinstance(value, str) and len(value) > STATUS_TEXT_CAP:
        return value[:STATUS_TEXT_CAP] + "…"
    return value


def status_projection(campaign: dict[str, Any]) -> dict[str, Any]:
    """The fixed, bounded view of one campaign the status tool returns.

    Named fields only: sub-questions, sources, scope constraints, the model pick and
    the grill tree stay in the Lab, where the user reviews them.
    """
    findings = campaign.get("findings") or []
    findings = [f for f in findings if isinstance(f, dict)]
    latest = findings[-1] if findings else {}
    view = {
        "id": campaign.get("id"),
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "created_by": campaign.get("created_by"),
        "question": campaign.get("question"),
        "execution_mode": campaign.get("execution_mode"),
        "total_cycles": campaign.get("total_cycles"),
        "max_cycles": campaign.get("max_cycles"),
        "created_at": campaign.get("created_at"),
        "started_at": campaign.get("started_at"),
        "completed_at": campaign.get("completed_at"),
        "error_message": campaign.get("error_message"),
        "pending_question": campaign.get("pending_question"),
        "cycles_reported": len(findings),
        "cycles_with_new_findings": sum(
            1
            for f in findings
            if isinstance(f.get("new_findings_count"), int) and f["new_findings_count"] > 0
        ),
        "latest_summary": latest.get("summary") if isinstance(latest.get("summary"), str) else None,
    }
    return {key: _capped(value) for key, value in view.items()}


@_require_opt_in
@_agent_only
@_require_enabled
async def handle_agent_status(request: web.Request) -> web.Response:
    """GET /api/apps/auto-research/agent/campaign-status?id=<id> -- one campaign, read-only."""
    session, err = await _caller_session(request, person_turn=False)
    if err is not None:
        return err
    campaign_id = request.query.get("id") or ""
    if not is_campaign_id(campaign_id):
        return _refuse("invalid_campaign_id", "'id' must be a Research Lab campaign id", status=400)
    campaign = await asyncio.to_thread(storage.get_campaign, campaign_id)
    if not campaign:
        return _refuse("not_found", f"no Research Lab campaign {campaign_id}", status=404)
    _audit_agent("campaign_status", campaign_id, "granted", session=session)
    return web.json_response(
        {"ok": True, "campaign": status_projection(campaign), "link": campaign_link(campaign_id)}
    )


def register_agent_routes(app: web.Application) -> None:
    """Mount the two agent routes on the gateway's aiohttp app.

    Each path here is named in ``server._STRICT_INTERNAL_API_PATHS``. That table is
    exact-or-prefix, so neither ``/api/apps/auto-research`` nor ``.../agent`` is
    listed: a future route under either must be admitted on purpose rather than
    inherit the internal-secret grant by sitting in the same directory.
    """
    app.router.add_post(AGENT_CREATE_PATH, handle_agent_create)
    app.router.add_get(AGENT_STATUS_PATH, handle_agent_status)
