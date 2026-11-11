"""Persistent local storage: what Jarvis remembers, reminders, transcript and audit trail.

One SQLite file in the data directory. Operations are small and synchronous;
a lock makes the single connection safe to use from worker threads too.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    fired_at REAL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL,
    all_day INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS transcript_conv ON transcript(conversation_id, id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    input TEXT NOT NULL,
    risk TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
"""

_MAX_MEMORY_CHARS = 2000
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Memory:
    id: int
    category: str
    content: str
    updated_at: float

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "category": self.category, "content": self.content, "updated": _iso(self.updated_at)}


@dataclass
class Reminder:
    id: int
    text: str
    due_at: float
    status: str
    created_at: float

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "due": _iso_local(self.due_at), "due_at": self.due_at, "status": self.status}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


class Store:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- memories

    def remember(self, content: str, category: str = "general") -> Memory:
        content = " ".join(content.split())[:_MAX_MEMORY_CHARS]
        category = (category or "general").strip().lower()[:40] or "general"
        if not content:
            raise ValueError("there is nothing to remember")
        now = time.time()
        with self._lock:
            # Saying the same thing twice refreshes the existing note instead of duplicating it.
            row = self._db.execute(
                "SELECT id FROM memories WHERE category = ? AND lower(content) = lower(?)", (category, content)
            ).fetchone()
            if row:
                self._db.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (now, row["id"]))
                return Memory(row["id"], category, content, now)
            cur = self._db.execute(
                "INSERT INTO memories(category, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (category, content, now, now),
            )
            return Memory(int(cur.lastrowid or 0), category, content, now)

    def update_memory(self, memory_id: int, content: str) -> bool:
        content = " ".join(content.split())[:_MAX_MEMORY_CHARS]
        with self._lock:
            cur = self._db.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?", (content, time.time(), memory_id)
            )
            return cur.rowcount > 0

    def forget(self, memory_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def memories(self, category: str | None = None, limit: int = 200) -> list[Memory]:
        sql = "SELECT id, category, content, updated_at FROM memories"
        args: tuple[Any, ...] = ()
        if category:
            sql += " WHERE category = ?"
            args = (category.strip().lower(),)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [Memory(r["id"], r["category"], r["content"], r["updated_at"]) for r in rows]

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        """Rank stored notes by how many of the query's words they contain."""
        words = set(_WORD.findall(query.lower()))
        if not words:
            return self.memories(limit=limit)
        scored: list[tuple[float, Memory]] = []
        for m in self.memories(limit=1000):
            have = set(_WORD.findall(f"{m.category} {m.content}".lower()))
            overlap = len(words & have)
            if overlap:
                scored.append((overlap / len(words) + 0.01 * overlap, m))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return [m for _, m in scored[:limit]]

    # --------------------------------------------------------------- reminders

    def add_reminder(self, text: str, due_at: float) -> Reminder:
        text = " ".join(text.split())[:500]
        if not text:
            raise ValueError("the reminder has no text")
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders(text, due_at, created_at) VALUES (?, ?, ?)", (text, due_at, now)
            )
            return Reminder(int(cur.lastrowid or 0), text, due_at, "pending", now)

    def reminders(self, status: str | None = "pending", limit: int = 100) -> list[Reminder]:
        sql = "SELECT id, text, due_at, status, created_at FROM reminders"
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY due_at ASC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [Reminder(r["id"], r["text"], r["due_at"], r["status"], r["created_at"]) for r in rows]

    def due_reminders(self, now: float | None = None) -> list[Reminder]:
        now = time.time() if now is None else now
        return [r for r in self.reminders("pending", limit=1000) if r.due_at <= now]

    def next_reminder_time(self) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT min(due_at) AS due FROM reminders WHERE status = 'pending'").fetchone()
        return row["due"] if row and row["due"] is not None else None

    def set_reminder_status(self, reminder_id: int, status: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE reminders SET status = ?, fired_at = ? WHERE id = ? AND status = 'pending'",
                (status, time.time(), reminder_id),
            )
            return cur.rowcount > 0

    # ---------------------------------------------------------------- calendar

    def add_event(self, title: str, start: float, end: float | None = None, all_day: bool = False, notes: str = "") -> dict[str, Any]:
        title = " ".join(title.split())[:200]
        if not title:
            raise ValueError("the event has no title")
        if end is not None and end < start:
            raise ValueError("the event ends before it starts")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(title, start, end, all_day, notes) VALUES (?, ?, ?, ?, ?)",
                (title, start, end, int(all_day), notes.strip()[:1000]),
            )
        return {"id": int(cur.lastrowid or 0), "title": title, "start": start, "end": end, "all_day": all_day, "notes": notes.strip()[:1000], "when": _event_when(start, end, all_day)}

    def events(self, start: float, end: float, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, start, end, all_day, notes FROM events WHERE start >= ? AND start < ? ORDER BY start LIMIT ?",
                (start, end, limit),
            ).fetchall()
        return [dict(r) | {"all_day": bool(r["all_day"]), "when": _event_when(r["start"], r["end"], bool(r["all_day"]))} for r in rows]

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0

    # -------------------------------------------------------------- transcript

    def add_transcript(self, conversation_id: str, role: str, text: str, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO transcript(conversation_id, ts, role, text, meta) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, time.time(), role, text, json.dumps(meta or {})),
            )

    def transcript(self, limit: int = 100, conversation_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT conversation_id, ts, role, text, meta FROM transcript"
        args: tuple[Any, ...] = ()
        if conversation_id:
            sql += " WHERE conversation_id = ?"
            args = (conversation_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [
            {"conversation_id": r["conversation_id"], "ts": r["ts"], "role": r["role"], "text": r["text"], "meta": json.loads(r["meta"])}
            for r in reversed(rows)
        ]

    def recap(self, exclude_conversation: str, max_chars: int = 1200, max_age_seconds: float = 12 * 3600) -> str:
        """A short text recap of the previous conversation, to carry context across a fresh start."""
        with self._lock:
            row = self._db.execute(
                "SELECT conversation_id FROM transcript WHERE conversation_id != ? AND ts >= ? ORDER BY id DESC LIMIT 1",
                (exclude_conversation, time.time() - max_age_seconds),
            ).fetchone()
        if not row:
            return ""
        lines: list[str] = []
        total = 0
        for entry in reversed(self.transcript(limit=12, conversation_id=row["conversation_id"])):
            who = "User" if entry["role"] == "user" else "Jarvis"
            line = f"{who}: {entry['text'][:300]}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(reversed(lines))

    # ------------------------------------------------------------------- audit

    def add_audit(
        self,
        *,
        tool: str,
        input: dict[str, Any],
        risk: str,
        decision: str,
        decided_by: str,
        ok: bool,
        summary: str,
        duration_ms: int,
        conversation_id: str = "",
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO audit(ts, conversation_id, tool, input, risk, decision, decided_by, ok, summary, duration_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), conversation_id, tool, _compact_json(input), risk, decision, decided_by, int(ok), summary[:500], duration_ms),
            )

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, tool, input, risk, decision, decided_by, ok, summary, duration_ms FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) | {"ok": bool(r["ok"])} for r in rows]


def _event_when(start: float, end: float | None, all_day: bool) -> str:
    begin = datetime.fromtimestamp(start)
    if all_day:
        return begin.strftime("%A %d %B %Y") + " (all day)"
    text = begin.strftime("%A %d %B %Y, %H:%M")
    return text + (datetime.fromtimestamp(end).strftime(" to %H:%M") if end else "")


def _compact_json(value: dict[str, Any], limit: int = 2000) -> str:
    """Audit entries record what was asked for, but not whole file bodies."""
    trimmed = {k: (v[:200] + f"... [{len(v)} chars]" if isinstance(v, str) and len(v) > 240 else v) for k, v in value.items()}
    return json.dumps(trimmed, default=str)[:limit]
