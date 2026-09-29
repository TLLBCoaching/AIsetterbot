"""Runs one reply cycle for a contact: guards → decide → send → tag."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .brain import Decision, SetterBrain
from .config import Settings, settings as default_settings
from .ghl import GHLClient, to_turns
from .store import Store

log = logging.getLogger(__name__)


def _parse_date(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def human_recently_replied(messages: list[dict], bot_ids: set[str], window_minutes: int) -> bool:
    """True if someone on the team (not the bot) sent a DM within the takeover window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    for m in messages:
        if m.get("direction") != "outbound" or m.get("id") in bot_ids:
            continue
        sent = _parse_date(m.get("dateAdded", ""))
        if sent and sent >= cutoff:
            return True
    return False


class SetterEngine:
    def __init__(self, brain: SetterBrain, ghl: GHLClient, store: Store, cfg: Settings = default_settings):
        self.brain, self.ghl, self.store, self.cfg = brain, ghl, store, cfg

    async def handle(self, contact_id: str) -> Decision | None:
        """Processes the latest state of a contact's DM thread. Returns the decision, or None if skipped."""
        if not self.cfg.bot_enabled:
            log.info("[%s] skipped: BOT_ENABLED is off", contact_id)
            return None

        lead = self.store.get_lead(contact_id)
        if lead["paused"]:
            log.info("[%s] skipped: bot paused for this lead", contact_id)
            return None

        contact = await self.ghl.get_contact(contact_id)
        tags = {t.lower() for t in contact.get("tags", [])}
        if self.cfg.tag_bot_off in tags or self.cfg.tag_needs_human in tags:
            log.info("[%s] skipped: contact tagged %s", contact_id, tags & {self.cfg.tag_bot_off, self.cfg.tag_needs_human})
            return None

        conversation_id = await self.ghl.find_conversation_id(contact_id)
        if not conversation_id:
            log.warning("[%s] no conversation found", contact_id)
            return None
        raw = await self.ghl.get_messages(conversation_id)
        bot_ids = self.store.bot_message_ids(contact_id)

        if human_recently_replied(raw, bot_ids, self.cfg.human_takeover_minutes):
            log.info("[%s] skipped: a human on the team is handling this thread", contact_id)
            return None

        history = to_turns(raw, bot_ids)
        if not history or history[-1].sender != "lead":
            log.info("[%s] skipped: nothing new from the lead", contact_id)
            return None

        name = contact.get("firstName") or contact.get("name") or ""
        decision = await asyncio.to_thread(self.brain.decide, history, lead["notes"], name)
        await self._act(contact_id, decision)
        return decision

    async def _act(self, contact_id: str, d: Decision) -> None:
        log.info("[%s] %s (stage=%s) %s %s", contact_id, d.action, d.stage, d.messages, d.escalation_reason)
        self.store.log_decision(contact_id, d.action, d.stage, d.messages, d.escalation_reason)

        for text in d.messages:
            if self.cfg.dry_run:
                log.info("[%s] DRY RUN, would send: %s", contact_id, text)
                continue
            # Pause as if typing, so bubbles don't all land in the same second.
            await asyncio.sleep(min(len(text) * self.cfg.typing_seconds_per_char, self.cfg.max_typing_seconds))
            message_id = await self.ghl.send_instagram_dm(contact_id, text)
            if message_id:
                self.store.record_bot_message(contact_id, message_id)

        tags = []
        paused = None
        if d.action == "send_booking_link":
            tags.append(self.cfg.tag_link_sent)
        if d.stage == "disqualified":
            tags.append(self.cfg.tag_disqualified)
        if d.action == "escalate":
            tags.append(self.cfg.tag_needs_human)
            paused = True
        if tags and not self.cfg.dry_run:
            await self.ghl.add_tags(contact_id, tags)
        self.store.save_lead(contact_id, d.stage, d.lead, paused)
