"""HQ web app: a private dashboard for your calendar, to-dos, client inbox, finances and an assistant.

Run it with:  uvicorn hq.server:app --host 0.0.0.0 --port 8100
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calendar, finance
from .assistant import Assistant
from .clients import InboxClient, draft_reply, render_messages
from .config import hq_settings
from .connector import ConnectorRoute, connector_app
from .gcal import GoogleCalendar, NeedsReconnect
from .store import HQStore
from .todos import todo_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hq")

STATIC = Path(__file__).parent / "static"
COOKIE = "hq_session"

store = HQStore(hq_settings.db_path)
todos = todo_backend(store)
google = GoogleCalendar(store)
calendar.google = google
# The Claude connector (see hq/connector.py), when HQ_CONNECTOR_TOKEN is set.
mcp_app = connector_app(store, todos)


@asynccontextmanager
async def lifespan(_app):
    if mcp_app is None:
        yield
        return
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(title="HQ", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(ConnectorRoute, mcp_app=mcp_app, token=hq_settings.connector_token)
_assistant: Assistant | None = None
_chat_lock = asyncio.Lock()


def assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        _assistant = Assistant(store, todos=todos)
    return _assistant


# Auth: one password, a signed session cookie. Changing HQ_PASSWORD signs every device out.

def _sign(expires: int) -> str:
    pw_hash = hashlib.sha256(hq_settings.password.encode()).hexdigest()
    return hmac.new(hq_settings.secret_key.encode(), f"{expires}:{pw_hash}".encode(), hashlib.sha256).hexdigest()


def make_session_token(now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + hq_settings.session_days * 86400)
    return f"{expires}.{_sign(expires)}"


def valid_session_token(token: str, now: float | None = None) -> bool:
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except (ValueError, AttributeError):
        return False
    return expires > (time.time() if now is None else now) and hmac.compare_digest(sig, _sign(expires))


def _configured() -> None:
    if not hq_settings.password or len(hq_settings.secret_key) < 16:
        raise HTTPException(503, "HQ isn't set up yet: set HQ_PASSWORD and HQ_SECRET_KEY (16+ characters).")


def require_login(request: Request) -> None:
    _configured()
    if not valid_session_token(request.cookies.get(COOKIE, "")):
        raise HTTPException(401, "Please log in")


_failures: dict[str, list[float]] = {}
MAX_FAILURES, FAILURE_WINDOW = 5, 900


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    _configured()
    who = request.client.host if request.client else "unknown"
    recent = [t for t in _failures.get(who, []) if time.time() - t < FAILURE_WINDOW]
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    if not hmac.compare_digest(body.password.encode(), hq_settings.password.encode()):
        _failures[who] = recent + [time.time()]
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    _failures.pop(who, None)
    response.set_cookie(
        COOKIE, make_session_token(), max_age=hq_settings.session_days * 86400,
        httponly=True, secure=hq_settings.secure_cookies, samesite="strict",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_login)])
async def me() -> dict:
    return {
        "ok": True,
        "timezone": hq_settings.timezone,
        "currency": hq_settings.currency,
        "connected": {
            "calendar": bool(hq_settings.calendar_urls) or google.connected,
            "google": {"configured": google.configured, "connected": google.connected, "account": google.account},
            "inbox": hq_settings.ghl_configured,
            "stripe": bool(hq_settings.stripe_key),
            "asana": todos.source == "asana",
            "claude_connector": mcp_app is not None,
        },
    }


def _today() -> date:
    return datetime.now(hq_settings.tz).date()


# Today

@app.get("/api/today", dependencies=[Depends(require_login)])
async def today_view() -> dict:
    today = _today()
    events, stripe, unread, open_todos = await asyncio.gather(
        calendar.get_events(*calendar.day_bounds(today, 2)),
        finance.stripe_summary(today),
        _unread_conversations(),
        _open_todos(),
    )
    iso = today.isoformat()
    return {
        "date": iso,
        "events": events,
        "todos_due": [t for t in open_todos if t["due"] and t["due"] <= iso],
        "todos_important": [t for t in open_todos if t["priority"] and not (t["due"] and t["due"] <= iso)][:5],
        "open_todos": len(open_todos),
        "unread": unread,
        "drafts": store.list_drafts(),
        "money": {k: stripe.get(k) for k in ("configured", "this_week", "this_month", "error", "failed")},
    }


async def _open_todos() -> list[dict]:
    try:
        return await todos.list()
    except httpx.HTTPError as exc:
        log.error("To-do source failed: %s", type(exc).__name__)
        return []


async def _unread_conversations() -> dict:
    if not hq_settings.ghl_configured:
        return {"configured": False, "conversations": []}
    inbox = InboxClient()
    try:
        return {"configured": True, "conversations": await inbox.list_conversations(limit=10, unread_only=True)}
    except httpx.HTTPError as exc:
        log.error("GHL request failed: %s", type(exc).__name__)
        return {"configured": True, "conversations": [], "error": "Couldn't reach GoHighLevel"}
    finally:
        await inbox.aclose()


# Calendar

@app.get("/api/calendar", dependencies=[Depends(require_login)])
async def calendar_view(start: str | None = None, days: int = 7) -> dict:
    first = date.fromisoformat(start) if start else _today()
    return await calendar.get_events(*calendar.day_bounds(first, max(1, min(days, 42))))


DATE_RE_EVENT = r"^\d{4}-\d{2}-\d{2}$"


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    date: str = Field(pattern=DATE_RE_EVENT)
    start_time: str = Field(default="", pattern=r"^(\d{2}:\d{2})?$")  # empty = all day
    duration_minutes: int = Field(default=60, ge=5, le=24 * 60)
    location: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=2000)


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    date: str | None = Field(default=None, pattern=DATE_RE_EVENT)
    start_time: str | None = Field(default=None, pattern=r"^(\d{2}:\d{2})?$")
    duration_minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    location: str | None = Field(default=None, max_length=300)


def _need_google() -> GoogleCalendar:
    if not google.connected:
        raise HTTPException(400, "Google Calendar isn't connected. Press Connect Google Calendar on the Calendar tab.")
    return google


async def _google_call(coro):
    try:
        return await coro
    except NeedsReconnect:
        raise HTTPException(409, "Google Calendar needs reconnecting. Press Connect Google Calendar on the Calendar tab.")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise HTTPException(404 if status == 404 else 502, "Google Calendar didn't accept that." if status != 404 else "That event no longer exists.")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach Google Calendar: {type(exc).__name__}")


@app.post("/api/calendar/events", dependencies=[Depends(require_login)])
async def add_event(body: EventIn) -> dict:
    g = _need_google()
    return await _google_call(g.create_event(body.title, body.date, body.start_time, body.duration_minutes,
                                             body.location, body.notes))


@app.patch("/api/calendar/events/{event_id}", dependencies=[Depends(require_login)])
async def move_event(event_id: str, body: EventPatch, calendar_id: str = "primary") -> dict:
    g = _need_google()
    changes = body.model_dump(exclude_unset=True)
    return await _google_call(g.update_event(
        event_id, calendar_id, title=changes.get("title"), day=changes.get("date"),
        start_time=changes.get("start_time"), duration_minutes=changes.get("duration_minutes"),
        location=changes.get("location")))


@app.delete("/api/calendar/events/{event_id}", dependencies=[Depends(require_login)])
async def delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    g = _need_google()
    await _google_call(g.delete_event(event_id, calendar_id))
    return {"ok": True}


# Connecting Google: HQ sends you to Google's sign-in, Google sends you back to /oauth/google/callback.
# The session cookie is SameSite=Strict, so it isn't sent on the way back from Google; a signed,
# short-lived state cookie (SameSite=Lax) proves the request started from a logged-in HQ.

OAUTH_COOKIE = "hq_oauth_state"


def _redirect_uri(request: Request) -> str:
    if hq_settings.public_url:
        return f"{hq_settings.public_url}/oauth/google/callback"
    # Behind Railway's proxy the app itself sees plain http, so trust the proxy's note of the real scheme.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/oauth/google/callback"


def _make_state() -> str:
    expires = int(time.time()) + 600
    nonce = secrets.token_urlsafe(18).replace(".", "")
    payload = f"{expires}.{nonce}"
    sig = hmac.new(hq_settings.secret_key.encode(), f"oauth:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid_state(state: str) -> bool:
    try:
        expires, nonce, sig = state.split(".")
    except ValueError:
        return False
    good = hmac.new(hq_settings.secret_key.encode(), f"oauth:{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


@app.get("/api/google/connect", dependencies=[Depends(require_login)])
async def google_connect(request: Request) -> RedirectResponse:
    if not google.configured:
        raise HTTPException(400, "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Railway first.")
    state = _make_state()
    resp = RedirectResponse(google.auth_url(state, _redirect_uri(request)), status_code=302)
    resp.set_cookie(OAUTH_COOKIE, state, max_age=600, httponly=True, secure=hq_settings.secure_cookies,
                    samesite="lax", path="/oauth")
    return resp


@app.get("/oauth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    _configured()
    cookie = request.cookies.get(OAUTH_COOKIE, "")
    outcome = "failed"
    if error:
        outcome = "cancelled" if error == "access_denied" else "failed"
    elif code and state and cookie and hmac.compare_digest(state, cookie) and _valid_state(state):
        try:
            await google.finish_connect(code, _redirect_uri(request))
            outcome = "connected"
        except Exception:
            log.exception("Finishing the Google connection failed")
    else:
        log.warning("Google callback with a missing or mismatched state")
    resp = RedirectResponse(f"/?google={outcome}#calendar", status_code=302)
    resp.delete_cookie(OAUTH_COOKIE, path="/oauth")
    return resp


@app.post("/api/google/disconnect", dependencies=[Depends(require_login)])
async def google_disconnect() -> dict:
    await google.disconnect()
    return {"ok": True}


# To-dos

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    notes: str = ""
    due: str | None = Field(default=None, pattern=DATE_RE)
    area: str = "business"
    priority: int = 0


class TodoPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = None
    due: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    area: str | None = None
    priority: int | None = None
    done: bool | None = None


TODO_ID = r"^[0-9]{1,30}$"


def _todo_error(exc: httpx.HTTPError) -> HTTPException:
    log.error("To-do source failed: %s", type(exc).__name__)
    where = "Asana" if todos.source == "asana" else "the to-do list"
    return HTTPException(502, f"Couldn't reach {where}. Try again.")


@app.get("/api/todos", dependencies=[Depends(require_login)])
async def list_todos(include_done: bool = False) -> list[dict]:
    try:
        return await todos.list(include_done=include_done)
    except httpx.HTTPError as exc:
        raise _todo_error(exc)


@app.post("/api/todos", dependencies=[Depends(require_login)])
async def add_todo(body: TodoIn) -> dict:
    try:
        return await todos.add(body.title, body.notes, body.due, body.area, body.priority)
    except httpx.HTTPError as exc:
        raise _todo_error(exc)


@app.patch("/api/todos/{todo_id}", dependencies=[Depends(require_login)])
async def update_todo(body: TodoPatch, todo_id: str = PathParam(pattern=TODO_ID)) -> dict:
    try:
        todo = await todos.update(todo_id, **body.model_dump(exclude_unset=True))
    except httpx.HTTPError as exc:
        raise _todo_error(exc)
    if not todo:
        raise HTTPException(404, "No such to-do")
    return todo


@app.delete("/api/todos/{todo_id}", dependencies=[Depends(require_login)])
async def delete_todo(todo_id: str = PathParam(pattern=TODO_ID)) -> dict:
    try:
        await todos.delete(todo_id)
    except httpx.HTTPError as exc:
        raise _todo_error(exc)
    return {"ok": True}


# Client inbox

def _inbox() -> InboxClient:
    if not hq_settings.ghl_configured:
        raise HTTPException(400, "GoHighLevel isn't connected. Set GHL_API_TOKEN and GHL_LOCATION_ID.")
    return InboxClient()


@app.get("/api/inbox", dependencies=[Depends(require_login)])
async def inbox_list(unread: bool = False) -> dict:
    inbox = _inbox()
    try:
        return {"conversations": await inbox.list_conversations(limit=40, unread_only=unread)}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GoHighLevel error: {type(exc).__name__}")
    finally:
        await inbox.aclose()


@app.get("/api/inbox/{conversation_id}", dependencies=[Depends(require_login)])
async def inbox_thread(conversation_id: str) -> dict:
    inbox = _inbox()
    try:
        convo = await inbox.get_conversation(conversation_id)
        messages = render_messages(await inbox.get_messages(conversation_id), limit=60)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GoHighLevel error: {type(exc).__name__}")
    finally:
        await inbox.aclose()
    return {**convo, "messages": messages, "draft": store.draft_for(conversation_id)}


class DraftIn(BaseModel):
    instructions: str = ""


@app.post("/api/inbox/{conversation_id}/draft", dependencies=[Depends(require_login)])
async def inbox_draft(conversation_id: str, body: DraftIn) -> dict:
    inbox = _inbox()
    try:
        convo = await inbox.get_conversation(conversation_id)
        messages = render_messages(await inbox.get_messages(conversation_id))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GoHighLevel error: {type(exc).__name__}")
    finally:
        await inbox.aclose()
    try:
        text = await draft_reply(assistant().client, convo["name"], convo["channel"], messages, body.instructions)
    except Exception as exc:
        log.exception("Drafting failed")
        raise HTTPException(502, f"Couldn't draft a reply: {type(exc).__name__}")
    return store.add_draft(conversation_id, convo["contact_id"], convo["name"], convo["channel"], text)


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    draft_id: int | None = None


@app.post("/api/inbox/{conversation_id}/send", dependencies=[Depends(require_login)])
async def inbox_send(conversation_id: str, body: SendIn) -> dict:
    inbox = _inbox()
    try:
        convo = await inbox.get_conversation(conversation_id)
        if not convo["send_type"]:
            raise HTTPException(400, f"Replying on {convo['channel']} isn't supported here. Reply in GoHighLevel.")
        message_id = await inbox.send_message(convo["contact_id"], convo["send_type"], body.text.strip())
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GoHighLevel didn't send it: {type(exc).__name__}")
    finally:
        await inbox.aclose()
    if body.draft_id:
        store.set_draft_status(body.draft_id, "sent", body.text.strip())
    log.info("Sent a %s message to contact %s", convo["send_type"], convo["contact_id"])
    return {"ok": True, "message_id": message_id}


@app.post("/api/drafts/{draft_id}/discard", dependencies=[Depends(require_login)])
async def discard_draft(draft_id: int) -> dict:
    store.set_draft_status(draft_id, "discarded")
    return {"ok": True}


# Finances

@app.get("/api/finance", dependencies=[Depends(require_login)])
async def finance_view() -> dict:
    today = _today()
    return {"stripe": await finance.stripe_summary(today), "log": finance.money_log_summary(store, today)}


class MoneyIn(BaseModel):
    amount: float
    category: str = Field(default="other", max_length=60)
    note: str = Field(default="", max_length=500)
    date: str | None = Field(default=None, pattern=DATE_RE)


@app.post("/api/money", dependencies=[Depends(require_login)])
async def add_money(body: MoneyIn) -> dict:
    return store.add_money(body.date or _today().isoformat(), body.amount, body.category.strip().lower(), body.note)


@app.delete("/api/money/{entry_id}", dependencies=[Depends(require_login)])
async def delete_money(entry_id: int) -> dict:
    store.delete_money(entry_id)
    return {"ok": True}


# Assistant

NOW_TAG = re.compile(r"^<now>.*?</now>\s*", re.S)


def render_chat(messages: list[dict]) -> list[dict]:
    """Turns the stored Claude transcript into chat bubbles, hiding tool plumbing."""
    out = []
    for m in messages:
        texts = [b["text"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            continue
        text = "\n\n".join(texts)
        if m["role"] == "user":
            text = NOW_TAG.sub("", text)
        out.append({"role": m["role"], "text": text.strip()})
    return out


@app.get("/api/chat", dependencies=[Depends(require_login)])
async def chat_history() -> dict:
    chat_id = store.latest_chat()
    return {"chat_id": chat_id, "messages": render_chat(store.chat_messages(chat_id)) if chat_id else []}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@app.post("/api/chat", dependencies=[Depends(require_login)])
async def chat(body: ChatIn) -> dict:
    async with _chat_lock:
        chat_id = store.latest_chat() or store.new_chat()
        return await assistant().chat(chat_id, body.message.strip())


@app.post("/api/chat/stream", dependencies=[Depends(require_login)])
async def chat_stream(body: ChatIn) -> StreamingResponse:
    """The same as /api/chat, streamed as one JSON event per line (text, status, then done)."""
    async def events():
        async with _chat_lock:
            chat_id = store.latest_chat() or store.new_chat()
            async for event in assistant().chat_events(chat_id, body.message.strip()):
                yield json.dumps(event) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat/new", dependencies=[Depends(require_login)])
async def new_chat() -> dict:
    return {"chat_id": store.new_chat()}


# Front end

@app.get("/health")
async def health() -> dict:
    return {"ok": True}


class FreshStaticFiles(StaticFiles):
    """Static files the browser re-checks on every load (a cheap 304 when unchanged), so updates show at once."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
