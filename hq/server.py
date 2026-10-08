"""HQ web app: a private dashboard for your calendar, to-dos, client inbox, finances and an assistant.

Run it with:  uvicorn hq.server:app --host 0.0.0.0 --port 8100
"""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calendar, finance
from .assistant import Assistant
from .clients import InboxClient, draft_reply, render_messages
from .config import hq_settings
from .store import HQStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hq")

STATIC = Path(__file__).parent / "static"
COOKIE = "hq_session"

app = FastAPI(title="HQ", docs_url=None, redoc_url=None, openapi_url=None)
store = HQStore(hq_settings.db_path)
_assistant: Assistant | None = None
_chat_lock = asyncio.Lock()


def assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        _assistant = Assistant(store)
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
            "calendar": bool(hq_settings.calendar_urls),
            "inbox": hq_settings.ghl_configured,
            "stripe": bool(hq_settings.stripe_key),
        },
    }


def _today() -> date:
    return datetime.now(hq_settings.tz).date()


# Today

@app.get("/api/today", dependencies=[Depends(require_login)])
async def today_view() -> dict:
    today = _today()
    events, stripe, unread = await asyncio.gather(
        calendar.get_events(*calendar.day_bounds(today, 2)),
        finance.stripe_summary(today),
        _unread_conversations(),
    )
    todos = store.list_todos()
    iso = today.isoformat()
    return {
        "date": iso,
        "events": events,
        "todos_due": [t for t in todos if t["due"] and t["due"] <= iso],
        "todos_important": [t for t in todos if t["priority"] and not (t["due"] and t["due"] <= iso)][:5],
        "open_todos": len(todos),
        "unread": unread,
        "drafts": store.list_drafts(),
        "money": {k: stripe.get(k) for k in ("configured", "this_week", "this_month", "error", "failed")},
    }


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


@app.get("/api/todos", dependencies=[Depends(require_login)])
async def list_todos(include_done: bool = False) -> list[dict]:
    return store.list_todos(include_done=include_done)


@app.post("/api/todos", dependencies=[Depends(require_login)])
async def add_todo(body: TodoIn) -> dict:
    return store.add_todo(body.title, body.notes, body.due, body.area, body.priority)


@app.patch("/api/todos/{todo_id}", dependencies=[Depends(require_login)])
async def update_todo(todo_id: int, body: TodoPatch) -> dict:
    todo = store.update_todo(todo_id, **body.model_dump(exclude_unset=True))
    if not todo:
        raise HTTPException(404, "No such to-do")
    return todo


@app.delete("/api/todos/{todo_id}", dependencies=[Depends(require_login)])
async def delete_todo(todo_id: int) -> dict:
    store.delete_todo(todo_id)
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


@app.post("/api/chat/new", dependencies=[Depends(require_login)])
async def new_chat() -> dict:
    return {"chat_id": store.new_chat()}


# Front end

@app.get("/health")
async def health() -> dict:
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
