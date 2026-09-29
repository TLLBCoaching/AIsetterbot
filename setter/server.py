"""Webhook server. GoHighLevel posts each inbound Instagram DM here.

Run it with:  uvicorn setter.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import hmac
import logging

from fastapi import FastAPI, HTTPException, Request

from .brain import SetterBrain
from .config import settings
from .engine import SetterEngine
from .ghl import GHLClient
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("setter")

app = FastAPI(title="TLLB AI Setter")
store = Store(settings.db_path)
_engine: SetterEngine | None = None
_pending: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}


def engine() -> SetterEngine:
    global _engine
    if _engine is None:
        _engine = SetterEngine(SetterBrain(), GHLClient(), store)
    return _engine


def _check_secret(request: Request) -> None:
    if not settings.webhook_secret:
        return
    supplied = request.headers.get("x-webhook-secret") or request.query_params.get("secret") or ""
    if not hmac.compare_digest(supplied, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="bad secret")


def extract_inbound(payload: dict) -> tuple[str | None, str]:
    """Pulls the contact ID and channel out of a GHL workflow webhook or an InboundMessage event."""
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    contact_id = payload.get("contact_id") or payload.get("contactId") or contact.get("id")
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    channel = str(payload.get("messageType") or message.get("type") or payload.get("channel") or "").upper()
    return contact_id, channel


# Channels the bot must never answer. Anything else (including numeric codes some payloads use)
# is let through, and the GHL workflow itself should be filtered to Instagram.
NON_INSTAGRAM = ("SMS", "EMAIL", "WHATSAPP", "FACEBOOK", "TYPE_FB", "GMB", "LIVE_CHAT", "CALL", "WEBCHAT")


async def _debounced(contact_id: str) -> None:
    # Leads often send several messages in a row, so wait for them to finish before answering once.
    await asyncio.sleep(settings.debounce_seconds)
    lock = _locks.setdefault(contact_id, asyncio.Lock())
    async with lock:
        if _pending.get(contact_id) is asyncio.current_task():
            _pending.pop(contact_id)
        try:
            await engine().handle(contact_id)
        except Exception:
            log.exception("[%s] reply cycle failed", contact_id)


@app.post("/webhook/ghl")
async def ghl_webhook(request: Request) -> dict:
    _check_secret(request)
    payload = await request.json()
    contact_id, channel = extract_inbound(payload)
    if not contact_id:
        raise HTTPException(status_code=400, detail="no contact id in payload")
    if any(c in channel for c in NON_INSTAGRAM) or channel == "FB":
        return {"status": "ignored", "reason": f"channel {channel}"}

    existing = _pending.get(contact_id)
    if existing and not existing.done():
        existing.cancel()
    _pending[contact_id] = asyncio.create_task(_debounced(contact_id))
    return {"status": "queued", "contact_id": contact_id}


@app.post("/leads/{contact_id}/pause")
async def pause(contact_id: str, request: Request) -> dict:
    _check_secret(request)
    store.set_paused(contact_id, True)
    return {"contact_id": contact_id, "paused": True}


@app.post("/leads/{contact_id}/resume")
async def resume(contact_id: str, request: Request) -> dict:
    _check_secret(request)
    store.set_paused(contact_id, False)
    return {"contact_id": contact_id, "paused": False}


@app.post("/admin/reload")
async def reload_knowledge(request: Request) -> dict:
    """Re-reads the knowledge files and examples after retraining, without a restart."""
    _check_secret(request)
    engine().brain.reload()
    return {"status": "reloaded"}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "bot_enabled": settings.bot_enabled, "dry_run": settings.dry_run}
