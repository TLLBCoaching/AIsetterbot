"""Offline tests for HQ: store, calendar parsing, Stripe maths, auth and the API."""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import icalendar
import pytest

os.environ.setdefault("HQ_PASSWORD", "correct horse")
os.environ.setdefault("HQ_SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("HQ_SECURE_COOKIES", "false")
os.environ.setdefault("HQ_DB", ":memory:")

from fastapi.testclient import TestClient  # noqa: E402

from hq import server  # noqa: E402
from hq.assistant import clean_assistant_content  # noqa: E402
from hq.calendar import events_from_calendar  # noqa: E402
from hq.clients import render_messages, summarise_conversation  # noqa: E402
from hq.finance import summarise_charges  # noqa: E402
from hq.store import HQStore  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")


@pytest.fixture
def store():
    return HQStore(":memory:")


def test_todos_sort_complete_and_reschedule(store):
    store.add_todo("someday thing")
    later = store.add_todo("later", due="2026-10-20")
    soon = store.add_todo("soon", due="2026-10-09", priority=1)
    assert [t["title"] for t in store.list_todos()] == ["soon", "later", "someday thing"]

    store.update_todo(soon["id"], done=True)
    assert [t["title"] for t in store.list_todos()] == ["later", "someday thing"]
    assert store.get_todo(soon["id"])["done_at"]

    store.update_todo(later["id"], due="")
    assert store.get_todo(later["id"])["due"] is None


def test_new_draft_replaces_old_one(store):
    first = store.add_draft("c1", "contact", "Sam", "Instagram", "hey")
    second = store.add_draft("c1", "contact", "Sam", "Instagram", "hey again")
    assert store.draft_for("c1")["id"] == second["id"]
    assert store.get_draft(first["id"])["status"] == "discarded"


def test_calendar_expands_recurring_events_in_local_time():
    cal = icalendar.Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Client check-ins\r\n"
        "DTSTART:20261005T230000Z\r\nDTEND:20261006T000000Z\r\nRRULE:FREQ=DAILY;COUNT=5\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Gym\r\nDTSTART;VALUE=DATE:20261008\r\nDTEND;VALUE=DATE:20261009\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:3\r\nSUMMARY:Cancelled\r\nSTATUS:CANCELLED\r\n"
        "DTSTART:20261008T010000Z\r\nDTEND:20261008T020000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    start = datetime(2026, 10, 8, tzinfo=SYD)
    events = events_from_calendar(cal, start, datetime(2026, 10, 9, tzinfo=SYD), SYD)
    titles = {e["title"]: e for e in events}
    assert set(titles) == {"Client check-ins", "Gym"}
    # 23:00 UTC on the 7th is 10am in Sydney on the 8th (AEDT, +11).
    assert titles["Client check-ins"]["start"].startswith("2026-10-08T10:00")
    assert titles["Gym"]["all_day"]


def test_stripe_summary_nets_refunds_and_lists_failures():
    def ts(y, m, d):
        return int(datetime(y, m, d, 12, tzinfo=SYD).timestamp())

    charges = [
        {"created": ts(2026, 10, 7), "status": "succeeded", "amount": 30000, "amount_captured": 30000,
         "amount_refunded": 5000, "currency": "aud"},
        {"created": ts(2026, 10, 2), "status": "succeeded", "amount": 20000, "amount_captured": 20000,
         "amount_refunded": 0, "currency": "aud"},
        {"created": ts(2026, 9, 20), "status": "succeeded", "amount": 10000, "amount_captured": 10000,
         "amount_refunded": 0, "currency": "aud"},
        {"created": ts(2026, 10, 6), "status": "failed", "amount": 15000, "currency": "aud",
         "billing_details": {"name": "Jo"}, "outcome": {"seller_message": "Insufficient funds"}},
        {"created": ts(2026, 10, 6), "status": "succeeded", "amount": 99900, "currency": "usd"},
    ]
    s = summarise_charges(charges, date(2026, 10, 8), SYD, "AUD")
    assert s["this_week"] == 250.0  # Mon 5 Oct onwards: 300 - 50 refund
    assert s["this_month"] == 450.0
    assert s["last_month"] == 100.0
    assert s["failed"] == [{"date": "2026-10-06", "amount": 150.0, "customer": "Jo", "reason": "Insufficient funds"}]


def test_conversation_summary_and_send_type():
    c = summarise_conversation({"id": "x", "contactId": "y", "fullName": "Sam", "lastMessageType": "TYPE_INSTAGRAM",
                                "unreadCount": 2, "lastMessageBody": "hey"})
    assert c["channel"] == "Instagram" and c["send_type"] == "IG" and c["unread"] == 2
    email = summarise_conversation({"id": "x", "lastMessageType": "TYPE_EMAIL"})
    assert email["send_type"] == ""  # can't reply to email from HQ
    msgs = render_messages([{"body": "hi", "direction": "inbound"}, {"body": " ", "direction": "outbound"},
                            {"body": "yo", "direction": "outbound"}])
    assert [m["from"] for m in msgs] == ["them", "you"]


def test_fallback_cleanup_drops_declined_attempt_blocks():
    blocks = [{"type": "thinking", "thinking": ""}, {"type": "tool_use", "id": "a"}, {"type": "fallback"},
              {"type": "thinking", "thinking": ""}, {"type": "text", "text": "hi"}]
    assert [b["type"] for b in clean_assistant_content(blocks)] == ["fallback", "thinking", "text"]
    plain = [{"type": "text", "text": "x"}]
    assert clean_assistant_content(plain) == plain


def test_session_tokens():
    token = server.make_session_token()
    assert server.valid_session_token(token)
    assert not server.valid_session_token(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not server.valid_session_token(server.make_session_token(now=0))  # expired
    assert not server.valid_session_token("garbage")


@pytest.fixture
def client():
    server.store = HQStore(":memory:")
    server._failures.clear()
    return TestClient(server.app)


def test_api_requires_login(client):
    assert client.get("/api/todos").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.get("/").status_code == 200  # the page itself loads, then shows the login form


def test_login_lockout(client):
    for _ in range(server.MAX_FAILURES):
        client.post("/api/login", json={"password": "nope"})
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 429


def test_todo_and_money_flow(client):
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 200
    todo = client.post("/api/todos", json={"title": "Film reel", "due": "2026-10-08"}).json()
    assert client.patch(f"/api/todos/{todo['id']}", json={"done": True}).json()["done"] is True
    assert client.get("/api/todos").json() == []
    assert client.post("/api/todos", json={"title": "x", "due": "tomorrow"}).status_code == 422

    client.post("/api/money", json={"amount": -49.5, "category": "Software", "note": "Canva"})
    log = client.get("/api/finance").json()["log"]
    assert log["recent"][0]["category"] == "software"

    me = client.get("/api/me").json()
    assert me["connected"]["stripe"] is False


def test_chat_history_hides_tool_plumbing():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "<now>Thu 08 Oct</now>\n\nwhat's on?"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": ""}, {"type": "tool_use", "id": "t"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "[]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Nothing today."}]},
    ]
    assert server.render_chat(messages) == [
        {"role": "user", "text": "what's on?"},
        {"role": "assistant", "text": "Nothing today."},
    ]


def test_unconfigured_hq_refuses_to_serve(client):
    object.__setattr__(server.hq_settings, "password", "")
    try:
        assert client.post("/api/login", json={"password": ""}).status_code == 503
    finally:
        object.__setattr__(server.hq_settings, "password", "correct horse")



class _Block:
    def __init__(self, **data):
        self.__dict__.update(data)

    def model_dump(self, **_):
        return dict(self.__dict__)


class _FakeMessages:
    """Answers the first call with a tool call, the second with text."""

    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return _Block(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t1", name="add_todo", input={"title": "Call Jake", "due": "2026-10-09"}),
            ])
        return _Block(stop_reason="end_turn", content=[_Block(type="text", text="Added: Call Jake, due tomorrow.")])


class _FakeClient:
    def __init__(self):
        self.beta = _Block(messages=_FakeMessages())


def test_assistant_runs_tools_and_keeps_an_append_only_transcript(store):
    import asyncio

    from hq.assistant import Assistant

    fake = _FakeClient()
    bot = Assistant(store, client=fake)
    chat_id = store.new_chat()
    result = asyncio.run(bot.chat(chat_id, "remind me to call Jake tomorrow"))

    assert result == {"reply": "Added: Call Jake, due tomorrow.", "tools": ["add_todo"]}
    assert [t["title"] for t in store.list_todos()] == ["Call Jake"]
    calls = fake.beta.messages.calls
    # The second request replays the first one's messages unchanged, then adds the tool call and its result.
    assert calls[1]["messages"][:1] == calls[0]["messages"]
    assert [m["role"] for m in calls[1]["messages"]] == ["user", "assistant", "user"]
    assert calls[1]["messages"][2]["content"][0]["tool_use_id"] == "t1"
