"""The state a place runs on: the session and message tables (SQLite).

agora keeps no history of its own. record() hands each event, with secrets
redacted, to a sink: the telemetry, which sends it to the place's trace
store (see telemetry.py).

The message table is also every resident's mailbox: a message is unread until
delivered_at is set, either when it starts a turn ("turn") or when the
recipient takes it with receive ("receive"). from_id is the sender's full
address (id@place); to_id is a resident's id, or the full address of someone
elsewhere for the copies this place keeps of what its residents sent out.
Every message carries its sender's signature (sig).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    harness TEXT,
    module_id TEXT,
    module_version TEXT,
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
    delivery TEXT NOT NULL,
    delivered_at TEXT,
    from_name TEXT,
    sig TEXT
);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages(to_id, delivered_at, ts);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StateStore:
    def __init__(self, root: str | Path, sink: Callable[..., None] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sink = sink  # (kind, session_id, thread_id, message_id, payload) -> None
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self._secrets: dict[str, str] = {}

    def _migrate(self) -> None:
        """Columns added after the first release."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        for col in ("from_name", "sig"):
            if col not in have:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        # v0.8: sessions run modules (the old columns stay unused in old indexes).
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for col in ("module_id", "module_version"):
            if col not in have:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")

    # --- redaction -------------------------------------------------------
    def add_secret(self, name: str, value: str) -> None:
        """Values registered here are replaced by [REDACTED:<name>] before any
        event leaves agora."""
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
    ) -> None:
        """Hand one event to the sink (the trace store), secrets redacted."""
        if self.sink is None:
            return
        body = json.loads(self._redact(json.dumps(payload, ensure_ascii=False, default=str)))
        self.sink(kind, session_id, thread_id, message_id, body)

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

    def session_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]

    # --- messages -----------------------------------------------------------
    def insert_message(self, msg: dict[str, Any]) -> None:
        row = {"from_name": None, "sig": None, **msg}
        with self._lock:
            self.db.execute(
                "INSERT INTO messages(id, ts, from_id, from_name, to_id, thread_id, reply_to, body, delivery, sig) "
                "VALUES (:id, :ts, :from_id, :from_name, :to_id, :thread_id, :reply_to, :body, :delivery, :sig)",
                row,
            )

    def message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(r) if r else None

    def mark_delivered(self, message_id: str, via: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE messages SET delivered_at = ?, delivery = ? WHERE id = ?", (now(), via, message_id)
            )

    def unread(
        self, to_id: str, *, from_id: str | None = None, reply_to: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE to_id = ? AND delivered_at IS NULL"
        args: list[Any] = [to_id]
        if from_id:
            sql += " AND from_id = ?"
            args.append(from_id)
        if reply_to:
            sql += " AND reply_to = ?"
            args.append(reply_to)
        sql += " ORDER BY ts, rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def unread_by_sender(self, to_id: str) -> list[tuple[str, str | None, int]]:
        """[(sender address, sender name, count)] of unread messages, oldest sender first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT from_id, MAX(from_name) AS name, COUNT(*) AS n, MIN(ts) AS first FROM messages "
                "WHERE to_id = ? AND delivered_at IS NULL GROUP BY from_id ORDER BY first",
                (to_id,),
            ).fetchall()
        return [(r["from_id"], r["name"], int(r["n"])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.db.close()
