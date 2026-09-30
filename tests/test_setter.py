import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from setter.brain import BOOKING_PLACEHOLDER, Decision, SetterBrain, Turn, build_system_prompt
from setter.config import settings
from setter.engine import SetterEngine, human_recently_replied
from setter.ghl import to_turns
from setter.server import extract_inbound
from setter.store import Store
from evals.run import lint

LEAD = {k: "" for k in ["name", "goal", "current_situation", "problem", "history", "why_now", "open_to_help", "other_notes"]}


class FakeClient:
    """Stands in for anthropic.Anthropic and returns a canned decision."""

    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.calls = []
        response = SimpleNamespace(
            stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def decision(**kw) -> dict:
    base = {"messages": ["Got you mate. What's been getting in the way?"], "action": "reply", "stage": "problem",
            "escalation_reason": "", "lead": LEAD}
    base.update(kw)
    return base


def test_system_prompt_includes_manual_and_voice():
    text = build_system_prompt()[0]["text"]
    assert "Framework rigid. Language flexible." in text
    assert "👊" in text
    assert "<!--" not in text  # editor notes are stripped


def test_decide_sends_request_with_cache_and_fallback():
    client = FakeClient(decision())
    d = SetterBrain(client=client, booking_link="https://x.co/book").decide([Turn("lead", "keen to lose the gut")])
    assert d.action == "reply" and d.messages
    call = client.calls[0]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["fallbacks"] == "default"
    assert "keen to lose the gut" in call["messages"][0]["content"]


def test_booking_placeholder_is_replaced_with_link():
    client = FakeClient(decision(messages=["Legend 👊 Here's the link", BOOKING_PLACEHOLDER], action="send_booking_link", stage="booking"))
    d = SetterBrain(client=client, booking_link="https://x.co/book").decide([Turn("lead", "yep keen")])
    assert d.messages == ["Legend 👊 Here's the link", "https://x.co/book"]


def test_booking_without_configured_link_escalates():
    client = FakeClient(decision(messages=[BOOKING_PLACEHOLDER], action="send_booking_link"))
    d = SetterBrain(client=client, booking_link="").decide([Turn("lead", "yep keen")])
    assert d.action == "escalate" and "BOOKING_LINK" in d.escalation_reason


def test_refusal_escalates():
    d = SetterBrain(client=FakeClient({}, stop_reason="refusal"), booking_link="x").decide([Turn("lead", "hi")])
    assert d.action == "escalate"


def test_caps_bubbles_at_two():
    client = FakeClient(decision(messages=["a", "b", "c", "d"]))
    assert len(SetterBrain(client=client, booking_link="x").decide([Turn("lead", "hi")]).messages) == 2


def test_to_turns_separates_bot_from_team():
    msgs = [
        {"id": "1", "direction": "inbound", "body": "hey"},
        {"id": "2", "direction": "outbound", "body": "hey mate"},
        {"id": "3", "direction": "outbound", "body": "Alistair here"},
        {"id": "4", "direction": "inbound", "body": ""},
    ]
    turns = to_turns(msgs, bot_message_ids={"2"})
    assert [t.sender for t in turns] == ["lead", "setter", "human"]


def test_human_takeover_window():
    now = datetime.now(timezone.utc)
    msgs = [{"id": "h", "direction": "outbound", "dateAdded": (now - timedelta(minutes=5)).isoformat()}]
    assert human_recently_replied(msgs, set(), 60)
    assert not human_recently_replied(msgs, {"h"}, 60)  # that was the bot
    old = [{"id": "h", "direction": "outbound", "dateAdded": (now - timedelta(hours=5)).isoformat()}]
    assert not human_recently_replied(old, set(), 60)


def test_extract_inbound_shapes():
    assert extract_inbound({"contact_id": "c1", "message": {"type": 18, "body": "hi"}}) == ("c1", "18")
    assert extract_inbound({"contactId": "c2", "messageType": "IG"}) == ("c2", "IG")
    assert extract_inbound({"contact": {"id": "c3"}}) == ("c3", "")


def test_lint_flags_manual_violations():
    assert lint(["Got you mate. What's been hardest about it?"]) == []
    assert any("price" in i for i in lint(["It's $99 a week"]))
    assert any("question" in i for i in lint(["What's your goal? And how long?"]))


class FakeGHL:
    def __init__(self, messages, tags=()):
        self.messages, self.tags, self.sent, self.added_tags = messages, list(tags), [], []

    async def get_contact(self, contact_id):
        return {"firstName": "Jake", "tags": self.tags}

    async def find_conversation_id(self, contact_id):
        return "conv1"

    async def get_messages(self, conversation_id):
        return self.messages

    async def send_instagram_dm(self, contact_id, text):
        self.sent.append(text)
        return f"m{len(self.sent)}"

    async def add_tags(self, contact_id, tags):
        self.added_tags += tags


class FakeBrain:
    def __init__(self, d: Decision):
        self.d, self.seen = d, None

    def decide(self, history, notes, name=""):
        self.seen = history
        return self.d


def _engine(ghl, d, **cfg):
    config = replace(settings, typing_seconds_per_char=0, dry_run=False, bot_enabled=True, **cfg)
    return SetterEngine(FakeBrain(d), ghl, Store(":memory:"), config)


def test_engine_replies_and_records_bot_messages():
    ghl = FakeGHL([{"id": "1", "direction": "inbound", "body": "keen to lose the gut"}])
    eng = _engine(ghl, Decision(["Love it. How long have you been stuck?"], "reply", "goal", "", LEAD))
    asyncio.run(eng.handle("c1"))
    assert ghl.sent == ["Love it. How long have you been stuck?"]
    assert eng.store.bot_message_ids("c1") == {"m1"}


def test_engine_escalation_tags_and_pauses():
    ghl = FakeGHL([{"id": "1", "direction": "inbound", "body": "i want a refund"}])
    eng = _engine(ghl, Decision(["Thanks for letting me know. I'll bring that to the coach."], "escalate", "goal", "refund request", LEAD))
    asyncio.run(eng.handle("c1"))
    assert settings.tag_needs_human in ghl.added_tags
    assert eng.store.get_lead("c1")["paused"]
    assert asyncio.run(eng.handle("c1")) is None  # stays quiet afterwards


def test_engine_respects_bot_off_tag_and_human_takeover():
    ghl = FakeGHL([{"id": "1", "direction": "inbound", "body": "hi"}], tags=[settings.tag_bot_off])
    eng = _engine(ghl, Decision(["x"], "reply", "opener"))
    assert asyncio.run(eng.handle("c1")) is None

    recent = datetime.now(timezone.utc).isoformat()
    ghl = FakeGHL([
        {"id": "1", "direction": "inbound", "body": "hi"},
        {"id": "2", "direction": "outbound", "body": "hey it's Alistair", "dateAdded": recent},
        {"id": "3", "direction": "inbound", "body": "oh hey"},
    ])
    eng = _engine(ghl, Decision(["x"], "reply", "opener"))
    assert asyncio.run(eng.handle("c1")) is None and ghl.sent == []


def test_engine_skips_when_last_message_is_ours():
    ghl = FakeGHL([{"id": "1", "direction": "inbound", "body": "hi"}, {"id": "2", "direction": "outbound", "body": "hey"}])
    eng = _engine(ghl, Decision(["x"], "reply", "opener"), human_takeover_minutes=0)
    eng.store.record_bot_message("c1", "2")
    assert asyncio.run(eng.handle("c1")) is None


def test_price_deflection_rule_in_prompt():
    text = build_system_prompt()[0]["text"]
    assert "build a quote out" in text
    assert "never escalate just because of price" in text
