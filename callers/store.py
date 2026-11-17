"""callers.db — its own SQLite file (WAL) on the persistent disk. One row per caller, post, call (post x mint),
outcome (per method version), coin, signal, watchlist entry, plus the X API spend ledger (an estimate)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS callers (username TEXT PRIMARY KEY, platform TEXT, user_id TEXT, name TEXT,
    followers INTEGER, tier TEXT DEFAULT 'NEW', score REAL, metrics TEXT, source TEXT, first_seen REAL,
    updated REAL, watched INTEGER DEFAULT 0, backfilled INTEGER DEFAULT 0, since_id TEXT);
CREATE TABLE IF NOT EXISTS posts (post_id TEXT PRIMARY KEY, username TEXT, created_at REAL, text TEXT, url TEXT,
    fetched_at REAL, source TEXT);
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, post_id TEXT, username TEXT, mint TEXT, ticker TEXT,
    resolve_note TEXT, post_time REAL, detected_at REAL, source TEXT, live INTEGER DEFAULT 0,
    UNIQUE (post_id, mint));
CREATE INDEX IF NOT EXISTS ix_calls_user ON calls (username, post_time);
CREATE INDEX IF NOT EXISTS ix_calls_mint ON calls (mint, post_time);
CREATE TABLE IF NOT EXISTS outcomes (call_id INTEGER, method TEXT, status TEXT, data TEXT, final INTEGER DEFAULT 0,
    evaluated_at REAL, PRIMARY KEY (call_id, method));
CREATE TABLE IF NOT EXISTS coins (mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, snapshot TEXT, safety TEXT,
    updated REAL);
CREATE TABLE IF NOT EXISTS signals (call_id INTEGER PRIMARY KEY, mint TEXT, username TEXT, created_at REAL,
    score REAL, label TEXT, components TEXT, snapshot TEXT, alerted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS watchlist (kind TEXT, key TEXT, note TEXT, added_at REAL, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS x_usage (month TEXT PRIMARY KEY, posts INTEGER DEFAULT 0, users INTEGER DEFAULT 0,
    requests INTEGER DEFAULT 0, est_usd REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS discovery (username TEXT PRIMARY KEY, mints TEXT, refs INTEGER DEFAULT 0,
    first_seen REAL, last_seen REAL, promoted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.RLock()

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # --- callers / posts / calls -------------------------------------------------------------------------------------
    def add_caller(self, username: str, platform: str = "x", source: str = "manual", **kw) -> bool:
        cur = self.x("INSERT OR IGNORE INTO callers (username, platform, source, first_seen, updated, user_id, name, "
                     "followers, watched) VALUES (?,?,?,?,?,?,?,?,?)",
                     (username, platform, source, self.clock(), self.clock(), kw.get("user_id"), kw.get("name"),
                      kw.get("followers"), int(kw.get("watched", 0))))
        return bool(cur.rowcount)

    def add_post(self, post_id: str, username: str, created_at: float, text: str, url: str, source: str) -> bool:
        cur = self.x("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?)",
                     (post_id, username, created_at, text, url, self.clock(), source))
        return bool(cur.rowcount)

    def add_call(self, post_id: str, username: str, mint: str, post_time: float, source: str, ticker: str | None = None,
                 resolve_note: str = "mint_in_post", live: bool = False) -> int | None:
        cur = self.x("INSERT OR IGNORE INTO calls (post_id, username, mint, ticker, resolve_note, post_time, "
                     "detected_at, source, live) VALUES (?,?,?,?,?,?,?,?,?)",
                     (post_id, username, mint, ticker, resolve_note, post_time, self.clock(), source, int(live)))
        return cur.lastrowid if cur.rowcount else None

    def set_outcome(self, call_id: int, method: str, out: dict) -> None:
        self.x("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?)",
               (call_id, method, out["status"], json.dumps(out), int(out.get("final", 0)), self.clock()))

    def outcomes(self, method: str, username: str | None = None) -> list[dict]:
        sql = ("SELECT c.*, o.status AS o_status, o.data AS o_data, o.final AS o_final FROM calls c JOIN outcomes o "
               "ON o.call_id = c.id AND o.method = ?")
        args = [method]
        if username:
            sql += " WHERE c.username = ?"
            args.append(username)
        rows = self.q(sql + " ORDER BY c.post_time", args)
        for r in rows:
            r["o"] = json.loads(r.pop("o_data") or "{}")
        return rows

    # --- X spend ledger ----------------------------------------------------------------------------------------------
    def month(self) -> str:
        return time.strftime("%Y-%m", time.gmtime(self.clock()))

    def spent(self) -> float:
        r = self.q("SELECT est_usd FROM x_usage WHERE month=?", (self.month(),))
        return r[0]["est_usd"] if r else 0.0

    def charge(self, posts: int, users: int, usd: float) -> None:
        self.x("INSERT INTO x_usage VALUES (?,?,?,1,?) ON CONFLICT (month) DO UPDATE SET posts=posts+excluded.posts, "
               "users=users+excluded.users, requests=requests+1, est_usd=est_usd+excluded.est_usd",
               (self.month(), posts, users, usd))

    def kv(self, k: str, v=None):
        if v is None:
            r = self.q("SELECT v FROM kv WHERE k=?", (k,))
            return json.loads(r[0]["v"]) if r else None
        self.x("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        return v
