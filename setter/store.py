"""Small SQLite store for per-lead state and the IDs of messages the bot sent."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    contact_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL DEFAULT 'opener',
    notes TEXT NOT NULL DEFAULT '{}',
    paused INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_messages (
    message_id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL,
    action TEXT NOT NULL,
    stage TEXT NOT NULL,
    messages TEXT NOT NULL,
    escalation_reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def get_lead(self, contact_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM leads WHERE contact_id = ?", (contact_id,)).fetchone()
        if not row:
            return {"contact_id": contact_id, "stage": "opener", "notes": {}, "paused": False}
        return {"contact_id": contact_id, "stage": row["stage"], "notes": json.loads(row["notes"]), "paused": bool(row["paused"])}

    def save_lead(self, contact_id: str, stage: str, notes: dict, paused: bool | None = None) -> None:
        current = self.get_lead(contact_id)
        paused = current["paused"] if paused is None else paused
        with self._lock:
            self._conn.execute(
                "INSERT INTO leads (contact_id, stage, notes, paused, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(contact_id) DO UPDATE SET stage=excluded.stage, notes=excluded.notes, "
                "paused=excluded.paused, updated_at=excluded.updated_at",
                (contact_id, stage, json.dumps(notes), int(paused), _now()),
            )
            self._conn.commit()

    def set_paused(self, contact_id: str, paused: bool) -> None:
        lead = self.get_lead(contact_id)
        self.save_lead(contact_id, lead["stage"], lead["notes"], paused)

    def record_bot_message(self, contact_id: str, message_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO bot_messages (message_id, contact_id, sent_at) VALUES (?, ?, ?)",
                (message_id, contact_id, _now()),
            )
            self._conn.commit()

    def bot_message_ids(self, contact_id: str) -> set[str]:
        rows = self._conn.execute("SELECT message_id FROM bot_messages WHERE contact_id = ?", (contact_id,))
        return {r["message_id"] for r in rows}

    def log_decision(self, contact_id: str, action: str, stage: str, messages: list[str], reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (contact_id, action, stage, messages, escalation_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (contact_id, action, stage, json.dumps(messages), reason, _now()),
            )
            self._conn.commit()
