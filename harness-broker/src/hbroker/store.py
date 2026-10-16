"""Execution history and broker state.

Every event is appended, unabridged, to a JSON-lines file per day
(events/YYYY-MM-DD.jsonl); SQLite holds a searchable index of the same events
plus the session and message tables. The JSONL files are the record of truth
and are never rewritten.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT,
    thread_id TEXT,
    message_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS events_thread ON events(thread_id, seq);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    harness TEXT,
    profile_id TEXT,
    profile_version TEXT,
    state TEXT NOT NULL,
    parent_id TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_token ON sessions(token_hash);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    reply_to TEXT,
    body TEXT NOT NULL,
    idempotency_key TEXT,
    delivery TEXT NOT NULL,
    delivered_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_idem ON messages(from_id, idempotency_key);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages(to_id, delivered_at, ts);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class HistoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "events").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._secrets: dict[str, str] = {}

    # --- redaction -------------------------------------------------------
    def add_secret(self, name: str, value: str) -> None:
        """Values registered here are replaced by [REDACTED:<name>] before any
        event reaches disk."""
        if value and len(value) >= 6:
            self._secrets[value] = name

    def _redact(self, text: str) -> str:
        for value, name in self._secrets.items():
            if value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
        return text

    # --- events -------------------------------------------------------------
    def record(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        **payload: Any,
    ) -> int:
        ts = now()
        body = self._redact(json.dumps(payload, ensure_ascii=False, default=str))
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events(ts, kind, session_id, thread_id, message_id, payload) VALUES (?,?,?,?,?,?)",
                (ts, kind, session_id, thread_id, message_id, body),
            )
            seq = int(cur.lastrowid)
            line = json.dumps(
                {
                    "seq": seq,
                    "ts": ts,
                    "kind": kind,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "payload": json.loads(body),
                },
                ensure_ascii=False,
            )
            with open(self.root / "events" / f"{ts[:10]}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return seq

    def events(
        self,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        kinds: Iterable[str] | None = None,
        after_seq: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where, args = [], []
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        if thread_id:
            where.append("thread_id = ?")
            args.append(thread_id)
        if since:
            where.append("ts >= ?")
            args.append(since)
        if until:
            where.append("ts < ?")
            args.append(until)
        kinds = list(kinds or [])
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if after_seq is not None:
            where.append("seq > ?")
            args.append(after_seq)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # --- sessions -----------------------------------------------------------
    def upsert_session(self, row: dict[str, Any]) -> None:
        cols = list(row)
        with self._lock:
            self.db.execute(
                f"INSERT INTO sessions({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'id')}",
                [json.dumps(row[c]) if c == "meta" else row[c] for c in cols],
            )

    def mark_stale_sessions(self, live_states: Iterable[str]) -> list[str]:
        """Sessions a previous broker run left live can no longer be reached."""
        states = list(live_states)
        with self._lock:
            rows = self.db.execute(
                f"SELECT id FROM sessions WHERE state IN ({','.join('?' * len(states))})", states
            ).fetchall()
            ids = [r["id"] for r in rows]
            self.db.execute(
                f"UPDATE sessions SET state = 'lost', ended_at = ?, end_reason = 'broker restarted' "
                f"WHERE state IN ({','.join('?' * len(states))})",
                [now(), *states],
            )
        return ids

    def participates(self, session_id: str, thread_id: str) -> bool:
        with self._lock:
            r = self.db.execute(
                "SELECT 1 FROM messages WHERE thread_id = ? AND (from_id = ? OR to_id = ?) LIMIT 1",
                (thread_id, session_id, session_id),
            ).fetchone()
        return r is not None

    def session_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]

    # --- messages -----------------------------------------------------------
    def find_idempotent(self, from_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM messages WHERE from_id = ? AND idempotency_key = ?", (from_id, key)
            ).fetchone()
        return dict(r) if r else None

    def insert_message(self, msg: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO messages(id, ts, from_id, to_id, thread_id, reply_to, body, idempotency_key, delivery) "
                "VALUES (:id, :ts, :from_id, :to_id, :thread_id, :reply_to, :body, :idempotency_key, :delivery)",
                msg,
            )

    def message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(r) if r else None

    def mark_delivered(self, message_id: str) -> None:
        with self._lock:
            self.db.execute("UPDATE messages SET delivered_at = ? WHERE id = ?", (now(), message_id))

    def undelivered(self, to_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE to_id = ? AND delivered_at IS NULL ORDER BY ts", (to_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.db.close()
