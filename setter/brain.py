"""The setter's brain: given a DM conversation, decide what to send next."""

import json
import logging
from dataclasses import dataclass, field

import anthropic

from .config import settings
from .knowledge import load_examples, load_knowledge

log = logging.getLogger(__name__)

BOOKING_PLACEHOLDER = "{{BOOKING_LINK}}"

STAGES = ["opener", "goal", "problem", "history", "gap", "qualify", "booking", "booked", "nurture", "disqualified"]
ACTIONS = ["reply", "send_booking_link", "escalate", "no_reply"]

# Operating rules for the bot. The conversation framework and the voice come from knowledge/*.md;
# this block only covers what's specific to running as an automated setter.
ROLE = f"""\
You're the Instagram DM setter for The Lean Lifestyle Blueprint (TLLB), an online coaching business. \
People message the account or reply to a story or ad, and you run that conversation. You're an AI assistant \
working as part of the coaching team. Follow the setter training manual and the style guide in the \
knowledge section. They are the standard, and the framework in them is rigid even though the wording is flexible.

Stages (set "stage" to where the conversation is now):
opener → goal → problem → history → gap → qualify → booking → booked. Use "nurture" for someone who's \
engaged but not ready yet, and "disqualified" for someone who isn't a fit.

Writing the reply:
- Each item in "messages" is one Instagram DM bubble. Usually send exactly one: 10–18 words, one purpose, \
one question. Only use a second bubble when it genuinely helps, for example the booking link on its own line. \
Never send more than two.
- Plain text only. No markdown, no bullet points, no sign-offs.
- Run the 5-second send test on every message before you return it.

Booking:
- Only move to a call once the lead qualifies under Stage 6. If they ask to book earlier, let them.
- First offer the call with a simple choice (for example "Are you free later today or tomorrow?"). \
When they say yes, send the link.
- To send the booking link, put the exact text {BOOKING_PLACEHOLDER} in a message and set action to \
"send_booking_link". Never type out a URL yourself.
- Once they confirm they've booked, set the stage to "booked" and confirm briefly.

Price or cost questions:
- Whenever price, cost, fees or "how much" comes up, at any stage and however many times, deflect it. \
Never state, guess or hint at a number, and never escalate just because of price. Explain that it's tailored, \
and ask permission to ask a few questions so you can see if you can help and build out a quote. For example: \
"Depends on what you need mate, it's all tailored. Mind if I ask a few quick questions to see if I can help and \
build a quote out for you?" Then carry on from whatever stage you're at. If you're already past qualifying, \
explain that the quote gets built on the call, and offer the call.

Handing off (action "escalate"):
- Escalate everything the manual lists under "When to stop and hand off": specific coaching, training or \
nutrition advice; injury, pain, medical, mental-health or wellbeing concerns; contracts, refunds or \
policy; complaints or sensitive issues; changes to a plan, service or appointment. Also escalate existing \
clients who need support, collab or partnership requests, anything not covered by the knowledge section, and \
anyone who seems to be under 18 (also set the stage to "disqualified").
- When you escalate, send one short handoff line in the approved handoff language, and put the reason in \
"escalation_reason" so the coach knows what to pick up.
- If someone seems to be in immediate danger or distress, don't coach them through it. Warmly encourage \
them to contact emergency services or a crisis line (in Australia, Lifeline 13 11 14 or 000 in an \
emergency), and escalate.

Honesty:
- Never claim to be the coach, and never make personal claims on the coach's behalf.
- If asked who's replying, say you're part of the coaching team. If they sincerely ask whether they're \
talking to a bot or an AI, don't deny it. Say you're an AI assistant for the coaching team and that the \
coach can jump in, then escalate.
- Never invent facts about the program, results, pricing or the coach. If it's not in the knowledge section, \
hand it off.

Other:
- If they say they're not interested or ask you to stop, reply with a short, friendly close with no pitch, \
and set the stage to "nurture" or "disqualified".
- Use action "no_reply" when nothing needs saying, for example a lone 👍 after you've confirmed their booking.
- Lines from the team (TEAM) were typed by a person. Stay consistent with anything they promised.

Each turn you'll get the lead notes saved from earlier turns and the full conversation. Return your next move \
and the updated lead notes. Keep the notes short, factual, and in the lead's own words where possible.
"""

LEAD_FIELDS = ["name", "goal", "current_situation", "problem", "history", "why_now", "open_to_help", "other_notes"]

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "DM bubbles to send, in order. Empty for no_reply.",
        },
        "action": {"type": "string", "enum": ACTIONS},
        "stage": {"type": "string", "enum": STAGES},
        "escalation_reason": {"type": "string", "description": "Why a human is needed. Empty unless escalating."},
        "lead": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in LEAD_FIELDS},
            "required": LEAD_FIELDS,
            "additionalProperties": False,
        },
    },
    "required": ["messages", "action", "stage", "escalation_reason", "lead"],
    "additionalProperties": False,
}


@dataclass
class Turn:
    sender: str  # "lead", "setter" (bot) or "human" (a person on the team replying manually)
    text: str


@dataclass
class Decision:
    messages: list[str]
    action: str
    stage: str
    escalation_reason: str = ""
    lead: dict = field(default_factory=dict)

    @classmethod
    def escalate(cls, reason: str, stage: str = "qualify", lead: dict | None = None) -> "Decision":
        return cls(messages=[], action="escalate", stage=stage, escalation_reason=reason, lead=lead or {})


def build_system_prompt() -> list[dict]:
    parts = [ROLE]
    knowledge = load_knowledge()
    if knowledge:
        parts.append(f"<knowledge>\n{knowledge}\n</knowledge>")
    examples = load_examples()
    if examples:
        parts.append(
            "<examples>\nReal conversations from this account that went well. Use them as a guide to tone, "
            "pacing and wording. Don't copy them word for word.\n\n"
            f"{examples}\n</examples>"
        )
    # One stable block with a cache breakpoint, so the script and examples are cached across calls.
    return [{"type": "text", "text": "\n\n".join(parts), "cache_control": {"type": "ephemeral"}}]


def render_conversation(history: list[Turn], lead_notes: dict, contact_name: str = "") -> str:
    labels = {"lead": "LEAD", "setter": "YOU", "human": "TEAM (a human on the team, typed manually)"}
    transcript = "\n".join(f"{labels.get(t.sender, t.sender.upper())}: {t.text}" for t in history)
    return (
        f"<lead_notes>\n{json.dumps(lead_notes or {}, indent=2)}\n</lead_notes>\n"
        f"<contact_name>{contact_name or 'unknown'}</contact_name>\n"
        f"<conversation>\n{transcript}\n</conversation>\n\n"
        "What's the next move?"
    )


class SetterBrain:
    def __init__(self, client: anthropic.Anthropic | None = None, booking_link: str | None = None):
        self.client = client or anthropic.Anthropic()
        self.booking_link = settings.booking_link if booking_link is None else booking_link
        self._system = build_system_prompt()

    def reload(self) -> None:
        """Re-reads the knowledge files, for example after retraining."""
        self._system = build_system_prompt()

    def decide(self, history: list[Turn], lead_notes: dict | None = None, contact_name: str = "") -> Decision:
        lead_notes = lead_notes or {}
        try:
            response = self.client.beta.messages.create(
                model=settings.model,
                max_tokens=16000,
                system=self._system,
                messages=[{"role": "user", "content": render_conversation(history, lead_notes, contact_name)}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": settings.effort,
                    "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
                },
                # If a safety classifier declines, rerun the request on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as exc:
            log.exception("Claude API call failed")
            return Decision.escalate(f"Bot error calling Claude: {type(exc).__name__}", lead=lead_notes)

        if response.stop_reason == "refusal":
            return Decision.escalate("Model declined to respond", lead=lead_notes)
        if response.stop_reason == "max_tokens":
            return Decision.escalate("Model response was cut off", lead=lead_notes)

        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            log.error("Unparseable model output: %r", text[:500])
            return Decision.escalate("Bot produced unreadable output", lead=lead_notes)

        return self._postprocess(Decision(**raw))

    def _postprocess(self, d: Decision) -> Decision:
        d.messages = [m.strip() for m in d.messages if m and m.strip()][:2]

        uses_link = any(BOOKING_PLACEHOLDER in m for m in d.messages)
        if uses_link or d.action == "send_booking_link":
            if not self.booking_link:
                return Decision.escalate("Lead is ready to book but BOOKING_LINK isn't configured", d.stage, d.lead)
            if not uses_link:
                d.messages.append(BOOKING_PLACEHOLDER)
            d.messages = [m.replace(BOOKING_PLACEHOLDER, self.booking_link) for m in d.messages]
            d.action = "send_booking_link"
            d.stage = "booking" if d.stage not in ("booked",) else d.stage

        if d.action == "no_reply":
            d.messages = []
        if d.action == "reply" and not d.messages:
            d.action = "no_reply"
        return d
