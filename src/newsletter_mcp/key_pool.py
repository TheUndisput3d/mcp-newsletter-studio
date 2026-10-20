"""Quota-aware credential pooling. Additional keys do not create additional provider quota.

Only independently authorized quota groups may have independent budgets. By default,
ALL keys for a provider share one group. State survives restarts in a local SQLite DB.
No secret key material is persisted in the quota database or returned by snapshot().
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field

from newsletter_mcp.config import QuotaConfig, Settings


class PoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credential:
    provider: str
    label: str
    group: str
    fingerprint: str
    secret: str = field(repr=False)


def credentials(settings: Settings, provider: str) -> list[Credential]:
    result, seen = [], set()
    entries = []
    if provider in settings.llm.key_pools:
        for item in settings.llm.key_pools[provider]:
            entries.append((item.label or item.key_env, item.quota_group, os.getenv(item.key_env, "").strip()))
    else:
        prefix = "ANTHROPIC" if provider == "claude" else provider.upper()
        raw = os.getenv(prefix + "_API_KEYS", "").strip()
        if raw.startswith("["):
            try:
                values = json.loads(raw)
            except ValueError as exc:
                raise PoolError(f"{prefix}_API_KEYS must be a JSON list or comma/newline-separated keys") from exc
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise PoolError(f"{prefix}_API_KEYS must contain only strings")
        else:
            values = re.split(r"[,\n]+", raw) if raw else []
        single = os.getenv(prefix + "_API_KEY", "")
        if single:
            values.append(single)
        for index, value in enumerate(values, 1):
            entries.append((f"{provider}-key-{index:02d}", "default", value.strip()))
    for label, group, secret in entries:
        if not secret or secret in seen:
            continue
        if any(c in secret for c in "\r\n"):
            raise PoolError("API keys must be single-line values")
        seen.add(secret)
        fingerprint = hashlib.sha256((provider + ":" + secret).encode()).hexdigest()
        result.append(Credential(provider, label, group, fingerprint, secret))
    return result


@dataclass
class Lease:
    id: str
    credential: Credential
    estimated_tokens: int


class KeyPool:
    def __init__(self, settings: Settings):
        self.settings = settings
        directory = settings.workspace / ".state"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "quotas.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pool_keys (
                    fingerprint TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
                    quota_group TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
                    disabled TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS pool_groups (
                    provider TEXT NOT NULL, quota_group TEXT NOT NULL,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(provider,quota_group)
                );
                CREATE TABLE IF NOT EXISTS pool_requests (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, provider TEXT NOT NULL,
                    quota_group TEXT NOT NULL, model TEXT NOT NULL, started REAL NOT NULL,
                    expires REAL NOT NULL, tokens INTEGER NOT NULL, status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pool_window ON pool_requests(provider,quota_group,started);
            """)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        # Close handles after committing/rolling back, including cancellation paths.
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _register(self, db, entries):
        for key in entries:
            db.execute("""INSERT INTO pool_keys(fingerprint,provider,label,quota_group) VALUES(?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET label=excluded.label,quota_group=excluded.quota_group""",
                       (key.fingerprint, key.provider, key.label, key.group))
            db.execute("INSERT OR IGNORE INTO pool_groups(provider,quota_group) VALUES(?,?)", (key.provider, key.group))

    def _try_acquire(self, provider: str, model: str, estimated_tokens: int,
                     lease_seconds: float) -> tuple[Lease | None, float]:
        entries = credentials(self.settings, provider)
        if not entries:
            raise PoolError(f"No {provider} API keys configured. Add keys in Settings or .env.")
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._register(db, entries)
            db.execute("UPDATE pool_requests SET status='interrupted' WHERE status='pending' AND expires<?", (now,))
            db.execute("DELETE FROM pool_requests WHERE started<? AND status!='pending'", (now - 7 * 86400,))
            states = {row["fingerprint"]: dict(row) for row in db.execute("SELECT * FROM pool_keys WHERE provider=?", (provider,))}
            candidates, waits, oversized = [], [], []
            for key in entries:
                state = states[key.fingerprint]
                if state["disabled"]:
                    continue
                quota = self.settings.llm.quotas.get(f"{provider}/{key.group}", QuotaConfig())
                if (quota.tpm and estimated_tokens > quota.tpm) or (quota.tpd and estimated_tokens > quota.tpd):
                    oversized.append(key.group)
                    continue
                group = db.execute("SELECT cooldown_until FROM pool_groups WHERE provider=? AND quota_group=?",
                                   (provider, key.group)).fetchone()
                wait = max(0, group[0] - now)
                rows = list(db.execute("SELECT started,tokens,status,expires FROM pool_requests WHERE provider=? AND quota_group=? AND started>? ORDER BY started",
                                       (provider, key.group, now - 86400)))
                pending = [r for r in rows if r["status"] == "pending"]
                if len(pending) >= quota.max_concurrency:
                    wait = max(wait, min(1.0, max(0.05, min(r["expires"] for r in pending) - now)))
                for window, request_limit, token_limit in ((60, quota.rpm, quota.tpm), (86400, quota.rpd, quota.tpd)):
                    recent = [r for r in rows if r["started"] > now - window]
                    requests, tokens = len(recent), sum(r["tokens"] for r in recent)
                    # Find the first expiry at which BOTH budgets have enough headroom.
                    if (request_limit and requests >= request_limit) or (token_limit and tokens + estimated_tokens > token_limit):
                        for row in recent:
                            requests -= 1
                            tokens -= row["tokens"]
                            if (not request_limit or requests < request_limit) and (not token_limit or tokens + estimated_tokens <= token_limit):
                                wait = max(wait, row["started"] + window - now + 0.02)
                                break
                if wait > 0:
                    waits.append(wait)
                else:
                    candidates.append(key)
            if candidates:
                chosen = min(candidates, key=lambda k: (states[k.fingerprint]["tokens"], states[k.fingerprint]["requests"], states[k.fingerprint]["last_used"]))
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO pool_requests VALUES(?,?,?,?,?,?,?,?,?)",
                           (identifier, chosen.fingerprint, provider, chosen.group, model, now, now + lease_seconds,
                            estimated_tokens, "pending"))
                db.execute("UPDATE pool_keys SET requests=requests+1,tokens=tokens+?,last_used=? WHERE fingerprint=?",
                           (estimated_tokens, now, chosen.fingerprint))
                return Lease(identifier, chosen, estimated_tokens), 0
            if waits:
                return None, min(waits)
            if oversized:
                raise PoolError("Request exceeds configured token quota. Reduce prompt/output budgets or configure an authorized larger quota; rotating keys cannot fix this.")
            raise PoolError(f"All {provider} credentials failed authorization. Replace or reauthorize them in Settings.")

    async def acquire(self, provider: str, model: str, estimated_tokens: int, lease_seconds: float) -> Lease:
        deadline = time.monotonic() + self.settings.llm.max_rate_wait_seconds
        while True:
            lease, wait = self._try_acquire(provider, model, estimated_tokens, lease_seconds)
            if lease:
                return lease
            remaining = deadline - time.monotonic()
            if wait > remaining or remaining <= 0:
                raise PoolError(f"{provider} quota is cooling down (at least {int(wait)+1}s). No quota was bypassed. Retry this stage later.")
            await asyncio.sleep(max(0.05, min(wait, 2)))

    def finish(self, lease: Lease, *, status: str = "ok", actual_tokens: int | None = None):
        tokens = max(0, actual_tokens) if actual_tokens is not None else lease.estimated_tokens
        with self.db() as db:
            row = db.execute("SELECT status FROM pool_requests WHERE id=?", (lease.id,)).fetchone()
            if row and row[0] == "pending":
                db.execute("UPDATE pool_requests SET status=?,tokens=? WHERE id=?", (status, tokens, lease.id))
                db.execute("UPDATE pool_keys SET tokens=MAX(0,tokens+?) WHERE fingerprint=?",
                           (tokens - lease.estimated_tokens, lease.credential.fingerprint))

    def cool_group(self, lease: Lease, seconds: float):
        # A 429 on one key applies to the ENTIRE known shared quota group.
        with self.db() as db:
            db.execute("UPDATE pool_groups SET cooldown_until=MAX(cooldown_until,?) WHERE provider=? AND quota_group=?",
                       (time.time() + max(0, seconds), lease.credential.provider, lease.credential.group))

    def disable_key(self, lease: Lease, reason: str):
        with self.db() as db:
            db.execute("UPDATE pool_keys SET disabled=? WHERE fingerprint=?", (reason[:100], lease.credential.fingerprint))

    def reset_authorization_failures(self, provider: str):
        # Explicitly resaving credentials permits an authorization re-test, NOT a quota reset.
        with self.db() as db:
            db.execute("UPDATE pool_keys SET disabled='' WHERE provider=?", (provider,))

    def snapshot(self) -> dict:
        now = time.time()
        output = {"providers": {}, "quota_note": "Keys in one Groq organization or Gemini project share quota. Zero configured limits mean unknown, not unlimited."}
        with self.db() as db:
            for provider in ("groq", "gemini", "openai", "claude"):
                entries = credentials(self.settings, provider)
                self._register(db, entries)
                records = []
                for key in entries:
                    row = dict(db.execute("SELECT * FROM pool_keys WHERE fingerprint=?", (key.fingerprint,)).fetchone())
                    cooldown = db.execute("SELECT cooldown_until FROM pool_groups WHERE provider=? AND quota_group=?",
                                          (provider, key.group)).fetchone()[0]
                    records.append({"label": key.label, "quota_group": key.group, "requests": row["requests"],
                                    "tokens": row["tokens"], "state": "authorization_failed" if row["disabled"] else "cooling_down" if cooldown > now else "ready",
                                    "cooldown_seconds": max(0, round(cooldown - now)),
                                    "limits": self.settings.llm.quotas.get(f"{provider}/{key.group}", QuotaConfig()).model_dump()})
                output["providers"][provider] = records
        return output
