"""SQLite store for HQ: to-dos, a money log, reply drafts and assistant chats."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    due TEXT,                         -- YYYY-MM-DD, or NULL for someday
    area TEXT NOT NULL DEFAULT 'business',
    priority INTEGER NOT NULL DEFAULT 0,  -- 1 = important
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS money (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,               -- YYYY-MM-DD
    amount REAL NOT NULL,             -- positive = money in, negative = money out
    category TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | sent | discarded
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,            -- JSON: the exact content blocks sent to / received from Claude
    created_at TEXT NOT NULL
);
"""

TODO_FIELDS = ("title", "notes", "due", "area", "priority", "done")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HQStore:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # To-dos

    def add_todo(self, title: str, notes: str = "", due: str | None = None, area: str = "business",
                 priority: int = 0) -> dict:
        todo_id = self._write(
            "INSERT INTO todos (title, notes, due, area, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title.strip(), notes, due or None, area or "business", int(priority), _now()),
        )
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM todos WHERE id = ?", (todo_id,))
        return _todo(rows[0]) if rows else None

    def list_todos(self, include_done: bool = False) -> list[dict]:
        where = "" if include_done else "WHERE done = 0"
        # Open items first, then by due date (someday last), then important first.
        return [_todo(r) for r in self._rows(
            f"SELECT * FROM todos {where} ORDER BY done, due IS NULL, due, priority DESC, id"
        )]

    def update_todo(self, todo_id: int, **changes) -> dict | None:
        changes = {k: v for k, v in changes.items() if k in TODO_FIELDS}
        if "done" in changes:
            changes["done"] = int(bool(changes["done"]))
            changes["done_at"] = _now() if changes["done"] else None
        if "due" in changes:
            changes["due"] = changes["due"] or None
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)
            self._write(f"UPDATE todos SET {cols} WHERE id = ?", (*changes.values(), todo_id))
        return self.get_todo(todo_id)

    def delete_todo(self, todo_id: int) -> None:
        self._write("DELETE FROM todos WHERE id = ?", (todo_id,))

    # Money log

    def add_money(self, date: str, amount: float, category: str = "other", note: str = "") -> dict:
        entry_id = self._write(
            "INSERT INTO money (date, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (date, float(amount), category or "other", note, _now()),
        )
        return self._rows("SELECT * FROM money WHERE id = ?", (entry_id,))[0]

    def list_money(self, since: str | None = None) -> list[dict]:
        if since:
            return self._rows("SELECT * FROM money WHERE date >= ? ORDER BY date DESC, id DESC", (since,))
        return self._rows("SELECT * FROM money ORDER BY date DESC, id DESC")

    def delete_money(self, entry_id: int) -> None:
        self._write("DELETE FROM money WHERE id = ?", (entry_id,))

    # Reply drafts

    def add_draft(self, conversation_id: str, contact_id: str, contact_name: str, channel: str, text: str) -> dict:
        # One live draft per conversation: a new one replaces the old.
        self._write(
            "UPDATE drafts SET status = 'discarded', updated_at = ? WHERE conversation_id = ? AND status = 'draft'",
            (_now(), conversation_id),
        )
        draft_id = self._write(
            "INSERT INTO drafts (conversation_id, contact_id, contact_name, channel, text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, contact_id, contact_name, channel, text, _now(), _now()),
        )
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return rows[0] if rows else None

    def list_drafts(self) -> list[dict]:
        return self._rows("SELECT * FROM drafts WHERE status = 'draft' ORDER BY id DESC")

    def draft_for(self, conversation_id: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM drafts WHERE conversation_id = ? AND status = 'draft' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        )
        return rows[0] if rows else None

    def set_draft_status(self, draft_id: int, status: str, text: str | None = None) -> None:
        if text is None:
            self._write("UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), draft_id))
        else:
            self._write("UPDATE drafts SET status = ?, text = ?, updated_at = ? WHERE id = ?",
                        (status, text, _now(), draft_id))

    # Assistant chats

    def new_chat(self, title: str = "") -> int:
        return self._write("INSERT INTO chats (title, created_at) VALUES (?, ?)", (title, _now()))

    def latest_chat(self) -> int | None:
        rows = self._rows("SELECT id FROM chats ORDER BY id DESC LIMIT 1")
        return rows[0]["id"] if rows else None

    def set_chat_title(self, chat_id: int, title: str) -> None:
        self._write("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))

    def append_message(self, chat_id: int, role: str, content: list | str) -> None:
        self._write(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, json.dumps(content), _now()),
        )

    def chat_messages(self, chat_id: int) -> list[dict]:
        rows = self._rows("SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def _todo(row: dict) -> dict:
    row["done"] = bool(row["done"])
    return row
