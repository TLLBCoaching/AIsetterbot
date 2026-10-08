"""Client inbox: recent GoHighLevel conversations, reply drafting in your voice, and sending once you approve."""

import logging

import anthropic

from setter.ghl import CONVERSATIONS_VERSION, GHLClient
from setter.knowledge import _read

from .config import hq_settings, setter_settings

log = logging.getLogger(__name__)

# GHL's lastMessageType → the "type" the send-message endpoint expects.
SEND_TYPES = {
    "TYPE_INSTAGRAM": "IG",
    "TYPE_SMS": "SMS",
    "TYPE_FACEBOOK": "FB",
    "TYPE_WHATSAPP": "WhatsApp",
    "TYPE_LIVE_CHAT": "Live_Chat",
}
CHANNEL_NAMES = {
    "TYPE_INSTAGRAM": "Instagram",
    "TYPE_SMS": "SMS",
    "TYPE_FACEBOOK": "Facebook",
    "TYPE_WHATSAPP": "WhatsApp",
    "TYPE_LIVE_CHAT": "Live chat",
    "TYPE_EMAIL": "Email",
    "TYPE_CALL": "Call",
}


class InboxClient(GHLClient):
    async def list_conversations(self, limit: int = 30, unread_only: bool = False) -> list[dict]:
        params = {"locationId": self.location_id, "limit": limit, "sortBy": "last_message_date", "sort": "desc"}
        if unread_only:
            params["status"] = "unread"
        data = await self._request("GET", "/conversations/search", CONVERSATIONS_VERSION, params=params)
        return [summarise_conversation(c) for c in data.get("conversations") or []]

    async def get_conversation(self, conversation_id: str) -> dict:
        data = await self._request("GET", f"/conversations/{conversation_id}", CONVERSATIONS_VERSION)
        return summarise_conversation(data.get("conversation", data))

    async def send_message(self, contact_id: str, send_type: str, text: str) -> str | None:
        data = await self._request(
            "POST", "/conversations/messages", CONVERSATIONS_VERSION,
            json={"type": send_type, "contactId": contact_id, "message": text},
        )
        return data.get("messageId") or data.get("id")


def summarise_conversation(c: dict) -> dict:
    msg_type = c.get("lastMessageType") or c.get("type") or ""
    return {
        "id": c.get("id", ""),
        "contact_id": c.get("contactId", ""),
        "name": c.get("fullName") or c.get("contactName") or c.get("email") or c.get("phone") or "Unknown",
        "last_message": (c.get("lastMessageBody") or "")[:300],
        "last_message_at": c.get("lastMessageDate") or c.get("dateUpdated") or "",
        "last_direction": c.get("lastMessageDirection") or "",
        "unread": int(c.get("unreadCount") or 0),
        "channel": CHANNEL_NAMES.get(msg_type, msg_type.replace("TYPE_", "").title() or "Unknown"),
        "send_type": SEND_TYPES.get(msg_type, ""),  # empty → can't reply from HQ, reply in GHL
        "tags": c.get("tags") or [],
    }


def render_messages(messages: list[dict], limit: int = 40) -> list[dict]:
    out = []
    for m in messages[-limit:]:
        body = (m.get("body") or "").strip()
        if not body:
            continue
        out.append({
            "from": "them" if m.get("direction") == "inbound" else "you",
            "text": body,
            "at": m.get("dateAdded", ""),
        })
    return out


DRAFT_ROLE = """\
You draft replies for Alistair, the coach who runs The Lean Lifestyle Blueprint (TLLB), an online coaching \
business. The conversation could be with a current client, a lead, or someone else. Write the next message \
Alistair would send, in his voice as described in the style guide. Alistair reviews every draft before it's sent.

- Match the channel: short and casual for DMs and SMS.
- Plain text only, no markdown. No sign-off unless the conversation uses them.
- Only state facts that appear in the conversation, the offer section, or Alistair's instructions. Never invent \
prices, results, dates or promises.
- If the reply needs a decision or a fact only Alistair knows, write the best draft you can and put the gap in \
[square brackets] so he fills it in.
- Return only the message text."""


def _draft_system() -> list[dict]:
    knowledge_dir = setter_settings.knowledge_dir
    parts = [DRAFT_ROLE]
    for filename, heading in (("voice.md", "HOW ALISTAIR TALKS"), ("offer.md", "THE OFFER")):
        body = _read(knowledge_dir / filename)
        if body:
            parts.append(f"<section name=\"{heading}\">\n{body}\n</section>")
    return [{"type": "text", "text": "\n\n".join(parts), "cache_control": {"type": "ephemeral"}}]


async def draft_reply(client: anthropic.AsyncAnthropic, name: str, channel: str, messages: list[dict],
                      instructions: str = "") -> str:
    transcript = "\n".join(f"{'THEM' if m['from'] == 'them' else 'ALISTAIR'}: {m['text']}" for m in messages)
    prompt = (
        f"<contact>{name}</contact>\n<channel>{channel}</channel>\n"
        f"<conversation>\n{transcript or '(no messages yet)'}\n</conversation>\n"
    )
    if instructions:
        prompt += f"<alistair_instructions>{instructions}</alistair_instructions>\n"
    prompt += "\nDraft Alistair's next message."

    response = await client.beta.messages.create(
        model=hq_settings.model,
        max_tokens=16000,
        system=_draft_system(),
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": hq_settings.effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to draft this reply")
    return "".join(b.text for b in response.content if b.type == "text").strip()
