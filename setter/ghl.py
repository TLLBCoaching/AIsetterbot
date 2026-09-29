"""Minimal GoHighLevel (LeadConnector) API client for Instagram DMs.

Uses a Private Integration token (Settings → Private Integrations in the sub-account)
with the conversations, conversations/message and contacts scopes.
"""

import logging

import httpx

from .brain import Turn
from .config import settings

log = logging.getLogger(__name__)

CONVERSATIONS_VERSION = "2021-04-15"
CONTACTS_VERSION = "2021-07-28"


class GHLClient:
    def __init__(self, token: str | None = None, location_id: str | None = None, base_url: str | None = None):
        self.location_id = location_id or settings.ghl_location_id
        self._http = httpx.AsyncClient(
            base_url=base_url or settings.ghl_base_url,
            headers={"Authorization": f"Bearer {token or settings.ghl_token}", "Accept": "application/json"},
            timeout=20.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, version: str, **kwargs) -> dict:
        resp = await self._http.request(method, path, headers={"Version": version}, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def get_contact(self, contact_id: str) -> dict:
        data = await self._request("GET", f"/contacts/{contact_id}", CONTACTS_VERSION)
        return data.get("contact", data)

    async def add_tags(self, contact_id: str, tags: list[str]) -> None:
        await self._request("POST", f"/contacts/{contact_id}/tags", CONTACTS_VERSION, json={"tags": tags})

    async def find_conversation_id(self, contact_id: str) -> str | None:
        data = await self._request(
            "GET",
            "/conversations/search",
            CONVERSATIONS_VERSION,
            params={"locationId": self.location_id, "contactId": contact_id},
        )
        convos = data.get("conversations") or []
        return convos[0]["id"] if convos else None

    async def get_messages(self, conversation_id: str, limit: int = 100) -> list[dict]:
        """Returns messages oldest-first."""
        data = await self._request(
            "GET", f"/conversations/{conversation_id}/messages", CONVERSATIONS_VERSION, params={"limit": limit}
        )
        msgs = data.get("messages", [])
        if isinstance(msgs, dict):  # the API nests the list as {"messages": {"messages": [...]}}
            msgs = msgs.get("messages", [])
        return sorted(msgs, key=lambda m: m.get("dateAdded", ""))

    async def send_instagram_dm(self, contact_id: str, text: str) -> str | None:
        data = await self._request(
            "POST",
            "/conversations/messages",
            CONVERSATIONS_VERSION,
            json={"type": "IG", "contactId": contact_id, "message": text},
        )
        return data.get("messageId") or data.get("id")


def to_turns(messages: list[dict], bot_message_ids: set[str]) -> list[Turn]:
    """Converts GHL messages into Turns, telling the bot's own messages apart from the team's."""
    turns: list[Turn] = []
    for m in messages:
        body = (m.get("body") or "").strip()
        if not body:
            continue
        if m.get("direction") == "inbound":
            sender = "lead"
        elif m.get("id") in bot_message_ids:
            sender = "setter"
        else:
            sender = "human"
        turns.append(Turn(sender=sender, text=body))
    return turns
