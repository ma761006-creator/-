"""SQLite 儲存：對話紀錄、筆記、提醒。時間一律以 UTC ISO 8601 字串保存。"""
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, due_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- 對話紀錄 -----------------------------------------------------------
    def add_message(self, user_id: str, role: str, content: str) -> None:
        self._exec(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, _iso(utcnow())),
        )

    def recent_messages(self, user_id: str, turns: int) -> List[dict]:
        """取最近 `turns` 輪（一輪 = 使用者 + 助手），由舊到新，且保證第一則是 user。"""
        rows = self._query(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, turns * 2),
        )
        msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def clear_messages(self, user_id: str) -> None:
        self._exec("DELETE FROM messages WHERE user_id = ?", (user_id,))

    # --- 筆記 ---------------------------------------------------------------
    def add_note(self, user_id: str, content: str) -> int:
        cur = self._exec(
            "INSERT INTO notes (user_id, content, created_at) VALUES (?, ?, ?)",
            (user_id, content, _iso(utcnow())),
        )
        return cur.lastrowid

    def search_notes(self, user_id: str, query: str = "", limit: int = 20) -> List[dict]:
        rows = self._query(
            "SELECT id, content, created_at FROM notes WHERE user_id = ? AND content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        )
        return [dict(r) for r in rows]

    def delete_note(self, user_id: str, note_id: int) -> bool:
        cur = self._exec("DELETE FROM notes WHERE user_id = ? AND id = ?", (user_id, note_id))
        return cur.rowcount > 0

    # --- 提醒 ---------------------------------------------------------------
    def add_reminder(self, user_id: str, due_at: datetime, text: str) -> int:
        cur = self._exec(
            "INSERT INTO reminders (user_id, due_at, text) VALUES (?, ?, ?)",
            (user_id, _iso(due_at), text),
        )
        return cur.lastrowid

    def pending_reminders(self, user_id: str) -> List[dict]:
        rows = self._query(
            "SELECT id, due_at, text FROM reminders WHERE user_id = ? AND sent = 0 ORDER BY due_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    def cancel_reminder(self, user_id: str, reminder_id: int) -> bool:
        cur = self._exec(
            "DELETE FROM reminders WHERE user_id = ? AND id = ? AND sent = 0",
            (user_id, reminder_id),
        )
        return cur.rowcount > 0

    def due_reminders(self, now: Optional[datetime] = None) -> List[dict]:
        rows = self._query(
            "SELECT id, user_id, due_at, text FROM reminders WHERE sent = 0 AND due_at <= ? "
            "ORDER BY due_at",
            (_iso(now or utcnow()),),
        )
        return [dict(r) for r in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        self._exec("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
