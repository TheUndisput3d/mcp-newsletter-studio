from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from newsletter_mcp.models import utc_now


class StateStore:
    """Operational metadata only. All newsletter artifact access is through filesystem MCP."""
    def __init__(self, workspace: Path):
        directory = workspace / ".state"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "studio.sqlite3"
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    provider TEXT NOT NULL, demo INTEGER NOT NULL, details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, phase TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stars (
                    url TEXT NOT NULL, observed_at TEXT NOT NULL, stars INTEGER NOT NULL,
                    PRIMARY KEY (url, observed_at)
                );
                CREATE TABLE IF NOT EXISTS schedule_claims (
                    key TEXT PRIMARY KEY, claimed_at TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite's own context manager handles transactions, not connection closure.
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, identifier: str, provider: str, demo: bool):
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,created_at,status,provider,demo) VALUES(?,?,?,?,?)",
                       (identifier, utc_now(), "starting", provider, int(demo)))

    def update(self, identifier: str, status: str, **details):
        with self.connect() as db:
            row = db.execute("SELECT details FROM runs WHERE id=?", (identifier,)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(details)
            db.execute("UPDATE runs SET status=?, details=? WHERE id=?", (status, json.dumps(value), identifier))

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) | {"demo": bool(row["demo"]), "details": json.loads(row["details"])} for row in rows]

    def get(self, identifier: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        return dict(row) | {"demo": bool(row["demo"]), "details": json.loads(row["details"])} if row else None

    def observe_stars(self, url: str, observed_at: str, stars: int) -> dict:
        with self.connect() as db:
            previous = db.execute("SELECT observed_at,stars FROM stars WHERE url=? AND observed_at<? ORDER BY observed_at DESC LIMIT 1",
                                  (url, observed_at)).fetchone()
            db.execute("INSERT OR IGNORE INTO stars(url,observed_at,stars) VALUES(?,?,?)", (url, observed_at, stars))
        return {"star_change_since_previous_observation": stars - previous["stars"] if previous else None,
                "previous_observed_at": previous["observed_at"] if previous else None}

    def claim_schedule(self, key: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO schedule_claims(key,claimed_at) VALUES(?,?)", (key, utc_now()))
            return cursor.rowcount == 1

    def event(self, identifier: str, phase: str, message: str):
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,created_at,phase,message) VALUES(?,?,?,?)",
                       (identifier, utc_now(), phase, message[:1600]))

    def events(self, identifier: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT created_at,phase,message FROM events WHERE run_id=? ORDER BY seq", (identifier,)).fetchall()
        return [dict(row) for row in rows]
