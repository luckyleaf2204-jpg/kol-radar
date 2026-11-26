"""Coin watchlist + on-chain tracing for the Insiders page (READ-ONLY: public Solana data only; nothing is ever signed,
sent, swapped or bought; paper/shadow only records prices after a signal).

Pieces (all persisted in one SQLite file next to the watch state, schema versioned by MIGRATIONS):
  Store        watched mints, normalised events, graph edges (raw evidence), signal snapshots + outcomes, settings.
  parse_mint_events  every owner whose balance of a mint moved in one transaction -> BUY / SELL / RECEIVE / SEND / CREATE
               (token balance pre/post + SOL / pool side, not log text alone), with a confidence and parser version.
  Tracker      the worker: history backfill per mint (signature pages with a checkpoint, the EARLIEST buyers parsed),
               live tail per mint (signatures `until` the newest checkpoint), funding lookups of new buyers (SOL / token
               transfers INTO the buyer before its buy, a bounded number of hops), all under one Helius call budget;
               pauses with the page's "Tạm dừng".
  graph        directed multigraph of edges (source -> destination, asset, amount, time, signature); soft clusters
               (union of edges whose weight passes a threshold, exchanges / services never merged), evidence paths
               from a buyer back to a known group wallet, the fan-out ("dispersal") pattern.
  rank / bots  explainable features per wallet; a rank that is NOT the SOL balance alone; bot labels with reasons
               (labels never remove anything from the feed).
  outcomes     price after each signal at fixed horizons (DexScreener / Jupiter, no Helius), metrics per rule.

Wording: a wallet found through funding is a "candidate / related wallet": a hypothesis with on-chain evidence, never a
claim that it belongs to a team."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from insiders.scan import KNOWN_LABELS, b58decode, on_curve

PARSER_VERSION = "p2"
RANK_VERSION = "rank-v1"
BOT_VERSION = "bot-v1"
RULES_VERSION = "rules-v1"

def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


CALLS_PER_MIN = _env_f("INSIDER_TRACK_CALLS_PER_MIN", 40)      # Helius calls this worker may spend per minute
HISTORY_MAX_PAGES = int(_env_f("INSIDER_TRACK_HISTORY_PAGES", 20))   # x 1000 signatures paged back per mint
EARLY_N = int(_env_f("INSIDER_TRACK_EARLY_N", 120))            # earliest transactions parsed per mint
LIVE_MIN_S = _env_f("INSIDER_TRACK_LIVE_MIN_S", 20)            # a live mint is polled at most this often
LIVE_MAX_PER_POLL = int(_env_f("INSIDER_TRACK_LIVE_MAX", 40))  # unused since B2 (every signature is queued; kept for env compat)
QUEUE_MAX = int(_env_f("INSIDER_TRACK_QUEUE_MAX", 3000))
LIVE_MAX_PAGES = int(_env_f("INSIDER_TRACK_LIVE_PAGES", 5))   # x 1000 signatures read back to a live checkpoint
GAP_PAGES_PER_TICK = 1                                        # open gaps are filled one page a tick
FUND_HOPS = int(_env_f("INSIDER_TRACK_FUND_HOPS", 2))          # funding traced back this many hops from a buyer
FUND_SIGS = int(_env_f("INSIDER_TRACK_FUND_SIGS", 8))          # transactions read before the buy, per wallet and hop
FUND_WINDOW_S = _env_f("INSIDER_TRACK_FUND_WINDOW_H", 72) * 3600   # funding older than this before the buy is ignored
FUND_MIN_SOL = _env_f("INSIDER_TRACK_FUND_MIN_SOL", 0.1)
BUSY_TX_PER_MIN = 1.0                                          # >= 100 tx at >= 1/min: exchange / router / bot hub
CLUSTER_MIN_W = _env_f("INSIDER_TRACK_CLUSTER_MIN_W", 0.35)
DISPERSAL_MIN_LEAVES = int(_env_f("INSIDER_TRACK_DISPERSAL_LEAVES", 5))
DISPERSAL_WINDOW_S = _env_f("INSIDER_TRACK_DISPERSAL_WINDOW_S", 6 * 3600)
COORD_WINDOW_S = _env_f("INSIDER_TRACK_COORD_WINDOW_S", 600)
HORIZONS = [int(x) for x in (os.environ.get("INSIDER_SHADOW_HORIZONS") or "60,300,900,3600,21600,86400").split(",") if x.strip()]
BULK_MAX = 50
MAX_MINTS = int(_env_f("INSIDER_TRACK_MAX_MINTS", 200))
# Initial list (verified on chain 2026-11-25: token mint accounts). Each seed is added ONCE; a removed one stays removed.
SEED_MINTS = os.environ.get("INSIDER_SEED_MINTS", "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump:CHUCHU,"
                            "C5eGEXJZYvXEbKdyvPtasksjGNV4xvMowpKM92bVv47f:MACCA,GHWEB3FsfqDbNBt9QxVU7kXkUh1cX7kFk5NVDkgApump:SCINU")
# Coins named by the user whose mint is not known for sure: shown as "needs mint address", never guessed.
SEED_PENDING = [("FRENS", "8NAtYWbvPEjVsEqHxjfB8tb2gbKZB2NUketXkJktKpump", "địa chỉ đọc từ ảnh không hợp lệ (không ra 32 byte)"),
                ("STANK", "5wsjHxT5nW5jQuVpXUNnYszF3WoWCfGGk9LMcsCjZY8", "địa chỉ đọc từ ảnh không tồn tại trên chain"),
                ("MOONX", None, "chưa có địa chỉ mint"), ("COGE", None, "chưa có địa chỉ mint")]
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022"}
RANK_W = {"funding": 0.35, "early": 0.2, "coord": 0.25, "fresh": 0.1, "balance": 0.1}
STATUSES = ("queued", "scanning", "live", "paused", "error", "stale")
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
_B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# ---------------------------------------------------------------------------------------------------- persistence
MIGRATIONS = [
    # v1: the whole first schema
    """
    CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS mints (
        mint TEXT PRIMARY KEY, name TEXT, note TEXT, tags TEXT, history INTEGER, live INTEGER,
        status TEXT, added_ts REAL, updated_ts REAL, removed INTEGER DEFAULT 0,
        newest_sig TEXT, back_before TEXT, pages INTEGER DEFAULT 0, history_state TEXT, history_note TEXT,
        sigs_seen INTEGER DEFAULT 0, parsed INTEGER DEFAULT 0, gap INTEGER DEFAULT 0,
        last_poll REAL, last_event_ts REAL, last_error TEXT, errors INTEGER DEFAULT 0, first_ts REAL);
    CREATE TABLE IF NOT EXISTS queue (
        sig TEXT, mint TEXT, why TEXT, added REAL, PRIMARY KEY (sig, mint));
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sig TEXT, wallet TEXT, mint TEXT, kind TEXT, slot INTEGER,
        block_time REAL, token_delta TEXT, quote_sol REAL, confidence TEXT, parser TEXT, source TEXT,
        observed_at REAL, parsed_at REAL, signer INTEGER, UNIQUE (sig, wallet, mint, kind));
    CREATE INDEX IF NOT EXISTS ev_mint ON events (mint, block_time);
    CREATE INDEX IF NOT EXISTS ev_wallet ON events (wallet, block_time);
    CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT, asset TEXT, amount REAL, ts REAL, sig TEXT,
        kind TEXT, hidden INTEGER DEFAULT 0, UNIQUE (sig, src, dst, asset));
    CREATE INDEX IF NOT EXISTS ed_dst ON edges (dst, ts);
    CREATE INDEX IF NOT EXISTS ed_src ON edges (src, ts);
    CREATE TABLE IF NOT EXISTS fund_todo (
        wallet TEXT, before_sig TEXT, before_ts REAL, hop INTEGER, mint TEXT, added REAL, done INTEGER DEFAULT 0,
        PRIMARY KEY (wallet, mint));
    CREATE TABLE IF NOT EXISTS wallet_info (wallet TEXT PRIMARY KEY, busy INTEGER, checked REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, rules_version TEXT, ts REAL, block_time REAL, mint TEXT,
        wallets TEXT, ref_price REAL, price_src TEXT, coverage TEXT, evidence TEXT, latency_s REAL, dedup TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS sg_ts ON signals (ts);
    CREATE TABLE IF NOT EXISTS outcomes (
        signal_id INTEGER, horizon INTEGER, due REAL, done REAL, price REAL, change REAL, max_change REAL,
        min_change REAL, note TEXT, PRIMARY KEY (signal_id, horizon));
    CREATE INDEX IF NOT EXISTS oc_due ON outcomes (done, due);
    """,
    # v2: on-chain mint check + coins waiting for an address. Additive only: no row, history or checkpoint changes.
    """
    ALTER TABLE mints ADD COLUMN verified TEXT;
    ALTER TABLE mints ADD COLUMN token_program TEXT;
    ALTER TABLE mints ADD COLUMN decimals INTEGER;
    CREATE TABLE IF NOT EXISTS pending_coins (
        name TEXT PRIMARY KEY, read_mint TEXT, reason TEXT, added REAL, resolved_mint TEXT);
    """,
    # v3: the mint's supply (raw units, read with the mint check) for the market cap at a buy. Additive only.
    """
    ALTER TABLE mints ADD COLUMN supply TEXT;
    CREATE INDEX IF NOT EXISTS ev_kind_time ON events (kind, block_time);
    """,
    # v4 (Gate B): explicit gaps, RPC metering, wallet history + PnL. New tables only: nothing existing is touched.
    """
    CREATE TABLE IF NOT EXISTS gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, address TEXT, before_sig TEXT, until_sig TEXT,
        reason TEXT, opened REAL, closed REAL, filled INTEGER DEFAULT 0, note TEXT);
    CREATE INDEX IF NOT EXISTS gp_open ON gaps (closed, scope);
    CREATE TABLE IF NOT EXISTS rpc_usage (
        day TEXT, worker TEXT, method TEXT, calls INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
        PRIMARY KEY (day, worker, method));
    CREATE TABLE IF NOT EXISTS wallets (
        wallet TEXT PRIMARY KEY, cohort TEXT, source TEXT, label TEXT, priority INTEGER, added REAL, legacy TEXT);
    CREATE INDEX IF NOT EXISTS wl_cohort ON wallets (cohort);
    CREATE TABLE IF NOT EXISTS wallet_queue (
        wallet TEXT PRIMARY KEY, priority INTEGER, state TEXT, before_sig TEXT, newest_sig TEXT, pages INTEGER DEFAULT 0,
        sigs INTEGER DEFAULT 0, parsed INTEGER DEFAULT 0, coverage TEXT, oldest_ts REAL, newest_ts REAL, updated REAL,
        last_error TEXT, errors INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS wallet_sigs (
        wallet TEXT, sig TEXT, block_time REAL, err INTEGER, parsed INTEGER DEFAULT 0, PRIMARY KEY (wallet, sig));
    CREATE INDEX IF NOT EXISTS wsg_todo ON wallet_sigs (parsed, wallet);
    CREATE TABLE IF NOT EXISTS wallet_trades (
        wallet TEXT, sig TEXT, mint TEXT, kind TEXT, tokens TEXT, sol REAL, fee_sol REAL, ts REAL, fee_in_sol INTEGER,
        PRIMARY KEY (wallet, sig, mint, kind));
    CREATE INDEX IF NOT EXISTS wtr_pair ON wallet_trades (wallet, mint, ts);
    CREATE TABLE IF NOT EXISTS wallet_pnl (
        wallet TEXT, mint TEXT, buys INTEGER, sells INTEGER, sends INTEGER, receives INTEGER, sol_in REAL, sol_out REAL,
        fees_sol REAL, fees_outside_sol REAL, realized_net_sol REAL, tok_bought TEXT, tok_sold TEXT, tok_sent TEXT, tok_received TEXT, tok_left TEXT,
        avg_cost_sol REAL, cost_sold_sol REAL, realized_sol REAL, unrealized_sol REAL, price_sol REAL, price_src TEXT,
        status TEXT, reason TEXT, first_ts REAL, last_ts REAL, updated REAL, PRIMARY KEY (wallet, mint));
    """,
    # v5 (Early Smart Money, 120-minute window). New esm_* tables only: DEPTH / STANK / INSIDERS cohorts (wallets),
    # events, wallet history and every earlier row stay as they are. The after-2h table (esm_outcome) is never read
    # by the score (esm_rank).
    """
    CREATE TABLE IF NOT EXISTS esm_coin (
        mint TEXT PRIMARY KEY, symbol TEXT, verified TEXT, t_create REAL, creation_sig TEXT, creator TEXT, quote TEXT,
        p0 REAL, p_15m REAL, p_60m REAL, p_120m REAL, x_15m REAL, x_60m REAL, x_120m REAL, peak_x REAL, peak_min REAL,
        coverage TEXT, status TEXT, note TEXT, rules_version TEXT, source TEXT, analyzed_at REAL);
    CREATE TABLE IF NOT EXISTS esm_window_wallet (
        mint TEXT, wallet TEXT, bucket TEXT, first_buy_min REAL, first_buy_sig TEXT, buys INTEGER, sells INTEGER,
        sol_in REAL, sol_out REAL, stable_in REAL, stable_out REAL, avg_cost_sol REAL, rise_x REAL, roles TEXT,
        result_sol REAL, result_pct REAL, bot_flag INTEGER, status TEXT, insufficient TEXT, points INTEGER,
        rules_version TEXT, PRIMARY KEY (mint, wallet));
    CREATE INDEX IF NOT EXISTS esw_wallet ON esm_window_wallet (wallet);
    CREATE TABLE IF NOT EXISTS esm_link (
        a TEXT, b TEXT, signal TEXT, strength TEXT, coin TEXT, sig TEXT, evidence TEXT, rules_version TEXT,
        PRIMARY KEY (a, b, signal));
    CREATE TABLE IF NOT EXISTS esm_wallet_entity (
        wallet TEXT PRIMARY KEY, entity TEXT, class TEXT, buyer INTEGER, rules_version TEXT);
    CREATE INDEX IF NOT EXISTS esm_ent ON esm_wallet_entity (entity);
    CREATE TABLE IF NOT EXISTS esm_flag (
        wallet TEXT, flag TEXT, ref TEXT, evidence TEXT, added REAL, PRIMARY KEY (wallet, flag, ref));
    CREATE TABLE IF NOT EXISTS esm_outcome (
        mint TEXT, horizon TEXT, price REAL, x_vs_open REAL, x_vs_120m REAL, trades INTEGER, status TEXT, why TEXT,
        PRIMARY KEY (mint, horizon));
    CREATE TABLE IF NOT EXISTS esm_rank (
        rules_version TEXT, wallet TEXT, score INTEGER, coins TEXT, early_coins TEXT, avg_entry_min REAL, class TEXT,
        entity TEXT, flags TEXT, insufficient TEXT, computed_at REAL, PRIMARY KEY (rules_version, wallet));
    CREATE TABLE IF NOT EXISTS esm_pending_mint (
        symbol TEXT PRIMARY KEY, status TEXT, candidates TEXT, note TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS esm_queue (
        mint TEXT PRIMARY KEY, symbol TEXT, state TEXT, before_sig TEXT, pages INTEGER DEFAULT 0, oldest_sig TEXT,
        oldest_ts REAL, t_create REAL, creation_sig TEXT, creator TEXT, calls INTEGER DEFAULT 0, note TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS esm_sig (
        mint TEXT, sig TEXT, block_time REAL, err INTEGER, state TEXT DEFAULT 'todo', tries INTEGER DEFAULT 0,
        PRIMARY KEY (mint, sig));
    CREATE INDEX IF NOT EXISTS esg_todo ON esm_sig (mint, state, block_time);
    CREATE TABLE IF NOT EXISTS esm_event (
        mint TEXT, sig TEXT, wallet TEXT, kind TEXT, sol REAL, tokens TEXT, quote TEXT, quote_amt REAL, slot INTEGER,
        block_time REAL, payer TEXT, fee INTEGER, signer INTEGER, PRIMARY KEY (mint, sig, wallet, kind));
    """,
    # v6 (New Coins LIVE): coins seen at creation on the pump.fun log stream, their trades in the first 120 minutes,
    # signals. New tables only.
    """
    CREATE TABLE IF NOT EXISTS nc_coin (
        mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, uri TEXT, creator TEXT, bonding_curve TEXT, platform TEXT,
        t_create REAL, create_sig TEXT, create_slot INTEGER, seen_at REAL, price REAL, mc_sol REAL, liq_sol REAL,
        buyers INTEGER DEFAULT 0, smart INTEGER DEFAULT 0, buy_sol REAL DEFAULT 0, sell_sol REAL DEFAULT 0, trades INTEGER DEFAULT 0,
        dev_sol REAL DEFAULT 0, dev_tokens REAL DEFAULT 0, dev_sold INTEGER DEFAULT 0, first_buy_t REAL, first_buy_seen REAL,
        signals TEXT, updated REAL, closed INTEGER DEFAULT 0, source TEXT);
    CREATE INDEX IF NOT EXISTS ncc_t ON nc_coin (t_create);
    CREATE TABLE IF NOT EXISTS nc_trade (
        mint TEXT, sig TEXT, wallet TEXT, buy INTEGER, sol REAL, tokens REAL, price REAL, t REAL, slot INTEGER, seen_at REAL,
        dev INTEGER DEFAULT 0, smart INTEGER DEFAULT 0, PRIMARY KEY (sig, mint, wallet, buy));
    CREATE INDEX IF NOT EXISTS nct_mint ON nc_trade (mint, t);
    CREATE INDEX IF NOT EXISTS nct_wallet ON nc_trade (wallet, t);
    CREATE TABLE IF NOT EXISTS nc_signal (
        mint TEXT, kind TEXT, t REAL, seen_at REAL, minute REAL, stage TEXT, wallets TEXT, evidence TEXT, PRIMARY KEY (mint, kind));
    CREATE INDEX IF NOT EXISTS ncs_t ON nc_signal (seen_at);
    """,
    # v7 (observation round, approved 2026-11-25): per coin, snapshots at 15 min / 1 h / 2 h / 6 h / 24 h of price, the
    # running high / low and the buyer mix, to measure the signals later. Records only: no signal rule reads it.
    """
    CREATE TABLE IF NOT EXISTS nc_obs (
        mint TEXT, horizon TEXT, t REAL, taken_at REAL, status TEXT, price REAL, max_price REAL, min_price REAL, launch_price REAL,
        last_trade_t REAL, buyers INTEGER, early15 INTEGER, smart INTEGER, smart_groups INTEGER, in_tx INTEGER, in_slot INTEGER,
        snipe5 INTEGER, dev_sold INTEGER, buy_sol REAL, sell_sol REAL, signals TEXT, migrated INTEGER, PRIMARY KEY (mint, horizon));
    CREATE INDEX IF NOT EXISTS nco_h ON nc_obs (horizon, t);
    """,
]


WAL_LIMIT_BYTES = 64 * 1024 * 1024


def checkpoint(db) -> tuple | None:
    """Move the WAL into the database and truncate the WAL file (disk, G0). Busy readers just make it a no-op."""
    try:
        return tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    except sqlite3.Error:
        return None


class Store:
    """Thread-safe SQLite store; every schema change is a numbered migration (never a dropped table)."""

    def __init__(self, path: str | Path, now=time.time):
        self.path, self.now = str(path), now
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
                # a checkpoint truncates the WAL file back to 64 MB at most (a big delete once grew it by ~0.5 GB)
                self.db.execute(f"PRAGMA journal_size_limit={WAL_LIMIT_BYTES}")
            ver = self.db.execute("PRAGMA user_version").fetchone()[0]
            if 0 < ver < len(MIGRATIONS) and self.path != ":memory:":
                from insiders.backup import copy_db           # a copy before any schema change (rollback = restore it);
                self.db.commit()                              # the online backup API also takes pages still in the WAL
                copy_db(self.path, f"{self.path}.v{ver}.bak")
            for i, sql in enumerate(MIGRATIONS[ver:], start=ver + 1):
                try:
                    self.db.executescript("BEGIN;" + sql + f"PRAGMA user_version={i};COMMIT;")   # all or nothing
                except Exception:
                    if self.db.in_transaction:                # a failed step leaves the old schema and rows intact
                        self.db.rollback()
                    raise

    def version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    # -- B2 gaps / RPC metering
    def open_gap(self, scope: str, address: str, before_sig: str | None, until_sig: str | None, reason: str,
                 note: str | None = None) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO gaps (scope, address, before_sig, until_sig, reason, opened, note) "
                                  "VALUES (?,?,?,?,?,?,?)", (scope, address, before_sig, until_sig, reason, self.now(), note))
            self.db.commit()
            return cur.lastrowid

    def gaps(self, open_only: bool = True, scope: str | None = None, address: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM gaps WHERE 1=1", []
        if open_only:
            sql += " AND closed IS NULL"
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if address:
            sql += " AND address=?"
            args.append(address)
        return self.q(sql + " ORDER BY id", args)

    def usage(self, worker: str, method: str, res=None, err: bool = False) -> None:
        """One RPC call: counted per UTC day, worker and method, with the answer's size (credits are priced per
        method, so calls x method is what the bill is made of)."""
        try:
            n = len(json.dumps(res, separators=(",", ":"))) if res is not None else 0
        except (TypeError, ValueError):
            n = 0
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        self.x("INSERT INTO rpc_usage (day, worker, method, calls, bytes, errors) VALUES (?,?,?,1,?,?) "
               "ON CONFLICT(day, worker, method) DO UPDATE SET calls=calls+1, bytes=bytes+excluded.bytes, "
               "errors=errors+excluded.errors", (day, worker, method, n, 1 if err else 0))

    def usage_rows(self, days: int = 7) -> list[dict]:
        since = time.strftime("%Y-%m-%d", time.gmtime(self.now() - days * 86400))
        return self.q("SELECT * FROM rpc_usage WHERE day>=? ORDER BY day DESC, worker, calls DESC", (since,))

    def get(self, k: str, default=None):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, k: str, v) -> None:
        self.x("INSERT INTO settings (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # -- mints
    def mint(self, mint: str) -> dict | None:
        r = self.q("SELECT * FROM mints WHERE mint=?", (mint,))
        return r[0] if r else None

    def mints(self, include_removed: bool = False) -> list[dict]:
        return self.q("SELECT * FROM mints" + ("" if include_removed else " WHERE removed=0") + " ORDER BY added_ts DESC")

    def set_mint(self, mint: str, **kw) -> None:
        if not kw:
            return
        kw["updated_ts"] = self.now()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE mints SET {cols} WHERE mint=?", (*kw.values(), mint))

    # -- events / edges
    def add_event(self, ev: dict) -> bool:
        """Idempotent on (signature, wallet, mint, kind): a transaction seen by the socket AND the poller is one event."""
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO events (sig, wallet, mint, kind, slot, block_time, token_delta, quote_sol, "
                "confidence, parser, source, observed_at, parsed_at, signer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.get("sig"), ev.get("wallet"), ev.get("mint") or "", ev.get("kind"), ev.get("slot"), ev.get("ts"),
                 str(ev.get("tokens") or 0), ev.get("sol"), ev.get("confidence") or "medium", ev.get("parser") or PARSER_VERSION,
                 ev.get("source") or "?", ev.get("observed_at"), ev.get("parsed_at") or self.now(), int(bool(ev.get("signer")))))
            self.db.commit()
            new = cur.rowcount > 0
        if new and ev.get("mint"):
            self.x("UPDATE mints SET last_event_ts=MAX(COALESCE(last_event_ts,0), ?) WHERE mint=?", (ev.get("ts") or 0, ev["mint"]))
        return new

    def add_edge(self, src: str, dst: str, asset: str, amount: float, ts, sig: str, kind: str) -> bool:
        if not src or not dst or src == dst:
            return False
        return self.x("INSERT OR IGNORE INTO edges (src, dst, asset, amount, ts, sig, kind) VALUES (?,?,?,?,?,?,?)",
                      (src, dst, asset, float(amount or 0), ts, sig, kind)) > 0

    def edges(self, hidden: bool = False, research: bool = False) -> list[dict]:
        """Graph edges of the live tracker. The research edges imported by esm.import_research (kind research_sol /
        research_token) are left out unless research=True, so the tracker's signals and pages stay as they were."""
        w = [] if hidden else ["hidden=0"]
        if not research:
            w.append("kind NOT LIKE 'research_%'")
        return self.q("SELECT * FROM edges" + (" WHERE " + " AND ".join(w) if w else ""))


# ---------------------------------------------------------------------------------------------------- validation
def valid_mint(s: str) -> bool:
    """A base58 string of a 32-byte public key (format only; whether it is a token mint is checked by the worker)."""
    if not isinstance(s, str) or not _B58.fullmatch(s):
        return False
    try:
        return len(b58decode(s)) == 32
    except ValueError:
        return False


def parse_bulk(text: str) -> list[dict]:
    """One mint per line, or CSV `mint,name,note`; blank lines and `#` comments skipped."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,;\t]", line)]
        if parts and parts[0].lower() in ("mint", "address", "ca"):
            continue                                          # a CSV header row
        out.append({"mint": parts[0], "name": parts[1] if len(parts) > 1 else "", "note": parts[2] if len(parts) > 2 else ""})
    return out


# ---------------------------------------------------------------------------------------------------- parsing
def _keys(tx: dict) -> list[str]:
    ks = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in ks]


def _signers(tx: dict) -> set[str]:
    ks = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    out = {k.get("pubkey") for k in ks if isinstance(k, dict) and k.get("signer")}
    keys = _keys(tx)
    if keys:
        out.add(keys[0])                                      # the fee payer always signs
    return out


def parse_mint_events(tx: dict, mint: str | None = None) -> list[dict]:
    """Events of every owner whose token balance moved in this transaction (only `mint` when given). Uses the
    balance deltas + the SOL / pool side (watch.analyse); a failed transaction yields nothing."""
    from insiders.watch import analyse
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    owners = set()
    for rows in (meta.get("preTokenBalances"), meta.get("postTokenBalances")):
        for b in rows or []:
            if b.get("owner") and (mint is None or b.get("mint") == mint) and b.get("mint") not in (WSOL, *STABLES):
                owners.add(b["owner"])
    signers, out = _signers(tx), []
    for o in sorted(owners):
        if not on_curve(o):
            continue                                          # pools / curves / vaults: program accounts, not wallets
        for ev in analyse(tx, o, with_quote=True):
            if ev["kind"] == "FUND" or (mint and ev.get("mint") != mint):
                continue
            signer = o in signers
            if ev["kind"] in ("BUY", "SELL", "CREATE"):
                conf = "high" if signer else "medium"         # a priced swap; the owner signing it is the strongest case
            else:
                conf = "medium" if signer else "low"          # a transfer: no price paid
            out.append({**ev, "slot": tx.get("slot"), "signer": signer, "confidence": conf, "parser": PARSER_VERSION})
    return out


def sol_and_token_in(tx: dict, wallet: str) -> list[tuple[str, str, float]]:
    """(source, asset, amount) moved INTO `wallet` by plain transfers: SOL from system transfers (amount in SOL) and
    tokens by balance deltas against another owner (amount in raw units / 10^decimals when known)."""
    out = []
    msg = (tx.get("transaction") or {}).get("message") or {}
    ins = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ins += inner.get("instructions") or []
    for i in ins:
        p = i.get("parsed")
        if isinstance(p, dict) and p.get("type") in ("transfer", "createAccount", "transferWithSeed"):
            info = p.get("info") or {}
            dst = info.get("destination") or info.get("newAccount")
            lam = int(info.get("lamports") or 0)
            if dst == wallet and info.get("source") and info["source"] != wallet and lam:
                out.append((info["source"], "SOL", lam / 1e9))
    meta = tx.get("meta") or {}
    by: dict[tuple[str, str], float] = defaultdict(float)
    dec: dict[str, int] = {}
    for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
        for b in rows or []:
            if b.get("owner") and b.get("mint") not in (WSOL, None):
                amt = (b.get("uiTokenAmount") or {})
                dec[b["mint"]] = int(amt.get("decimals") or 0)
                by[(b["owner"], b["mint"])] += sign * int(amt.get("amount") or 0)
    for (owner, m), d in by.items():
        if owner == wallet and d > 0:
            src = max(((o, -dd) for (o, mm), dd in by.items() if mm == m and o != wallet and dd < 0), key=lambda x: x[1], default=(None, 0))
            if src[0] and on_curve(src[0]):                   # from a pool = a buy, not a transfer
                out.append((src[0], m, d / 10 ** dec.get(m, 0)))
    return out


def token_moves(tx: dict, min_raw: int = 1) -> list[dict]:
    """Token transfers of one transaction, sender and receiver taken from the OWNER of the token accounts (pre / post
    token balances), not from the fee payer or the signer. Per mint: owners whose balance fell = senders, rose =
    receivers. A side that is a program account (pool, curve, vault) makes it a swap, not a transfer (skipped).
    Pairing: one sender -> each receiver, or each sender -> one receiver; several on both sides = src/dst None
    ('ambiguous'), never guessed. WSOL is SOL, not a token (skipped)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = _keys(tx)
    by: dict[tuple[str, str], int] = defaultdict(int)
    acct: dict[tuple[str, str], str] = {}
    dec: dict[str, int] = {}
    for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
        for b in rows or []:
            m, o = b.get("mint"), b.get("owner")
            if not m or not o or m == WSOL:
                continue
            amt = b.get("uiTokenAmount") or {}
            dec[m] = int(amt.get("decimals") or 0)
            by[(o, m)] += sign * int(amt.get("amount") or 0)
            i = b.get("accountIndex")
            if isinstance(i, int) and i < len(keys):
                acct[(o, m)] = keys[i]
    out = []
    for m in sorted({m for _, m in by}):
        snd = [(o, -d) for (o, mm), d in by.items() if mm == m and d <= -min_raw]
        rcv = [(o, d) for (o, mm), d in by.items() if mm == m and d >= min_raw]
        if not snd or not rcv or any(not on_curve(o) for o, _ in snd + rcv):
            continue                                          # a mint / burn, or a swap against a pool
        if len(snd) == 1:
            pairs = [(snd[0][0], o, d) for o, d in rcv]
        elif len(rcv) == 1:
            pairs = [(o, rcv[0][0], d) for o, d in snd]
        else:
            pairs = [(None, None, sum(d for _, d in rcv))]
        for s, d, raw in pairs:
            out.append({"src": s, "dst": d, "mint": m, "raw": raw, "decimals": dec.get(m, 0),
                        "src_account": acct.get((s, m)), "dst_account": acct.get((d, m)),
                        "verified": "owner" if s and d else "ambiguous"})
    return out


# ---------------------------------------------------------------------------------------------------- graph
def pass_through(edges: list[dict], max_gap_s: float = 900, max_loss_sol: float = 0.02, max_loss_pct: float = 0.02,
                 min_sol: float = 0.05, busy: set | None = None) -> list[dict]:
    """Near 1:1 relays: SOL arrives at a wallet and (almost) the same amount leaves it soon after to someone else
    (in -> out within max_gap_s, out <= in, the difference <= max(max_loss_sol, max_loss_pct x in): fees only).
    A relay wallet hides who really pays whom. Raw edges (one per transfer), SOL only; services never count."""
    ins: dict[str, list[dict]] = defaultdict(list)
    outs: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        if e.get("asset") != "SOL" or (e.get("amount") or 0) < min_sol or e.get("ts") is None:
            continue
        ins[e["dst"]].append(e)
        outs[e["src"]].append(e)
    rows = []
    for node, es in ins.items():
        if is_service(node, busy) or node not in outs:
            continue
        used = set()
        for a in sorted(es, key=lambda e: e["ts"]):
            for b in sorted(outs[node], key=lambda e: e["ts"]):
                gap = b["ts"] - a["ts"]
                if id(b) in used or gap < 0 or gap > max_gap_s or b["dst"] == a["src"]:
                    continue
                loss = a["amount"] - b["amount"]
                if 0 <= loss <= max(max_loss_sol, max_loss_pct * a["amount"]):
                    used.add(id(b))
                    rows.append({"relay": node, "src": a["src"], "dst": b["dst"], "src_program": not on_curve(a["src"]),
                                 "dst_program": not on_curve(b["dst"]), "sol_in": round(a["amount"], 6),
                                 "sol_out": round(b["amount"], 6), "loss_sol": round(loss, 6), "gap_s": round(gap, 1),
                                 "in_sig": a.get("sig"), "out_sig": b.get("sig"), "ts": a["ts"]})
                    break
    return sorted(rows, key=lambda r: (r["relay"], r["ts"]))


def batch_splits(edges: list[dict], min_sol: float = 0.05, busy: set | None = None) -> list[dict]:
    """One transaction, one source, SOL to >= 2 recipients (each >= min_sol): a payout split. The same recipient set
    paid again in another transaction = a recurring split (an operator paying its own wallets, or a fee split).
    persons = every recipient is a wallet (on the curve); a split into program accounts (vaults, fee accounts of a
    trading bot) is a payment to a service and links nobody."""
    per: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    ts: dict[str, float] = {}
    for e in edges:
        if e.get("asset") == "SOL" and (e.get("amount") or 0) >= min_sol and e.get("sig") and not is_service(e["src"], busy) \
                and not is_service(e["dst"], busy):
            d = per[(e["sig"], e["src"])]
            d[e["dst"]] = d.get(e["dst"], 0) + e["amount"]
            ts[e["sig"]] = e.get("ts")
    groups: dict[tuple, list] = defaultdict(list)
    for (sig, src), d in per.items():
        if len(d) >= 2:
            groups[(src, tuple(sorted(d)))].append({"sig": sig, "ts": ts.get(sig), "amounts": {k: round(v, 6) for k, v in d.items()}})
    rows = [{"source": src, "recipients": list(dst), "n_tx": len(txs), "recurring": len(txs) >= 2,
             "persons": all(on_curve(d) for d in dst), "program_recipients": [d for d in dst if not on_curve(d)],
             "sol": round(sum(sum(t["amounts"].values()) for t in txs), 6), "txs": sorted(txs, key=lambda t: t["ts"] or 0)}
            for (src, dst), txs in groups.items()]
    return sorted(rows, key=lambda r: (-r["n_tx"], -r["sol"]))


def shared_counterparties(edges: list[dict], wallets: set[str], side: str, min_sol: float = 0.05,
                          busy: set | None = None) -> dict[str, dict]:
    """side='in': funders that sent SOL to >= 2 of `wallets`; side='out': recipients that got SOL from >= 2 of them.
    Services / busy hubs and the wallets themselves (that is a direct transfer) are left out."""
    hit: dict[str, dict] = defaultdict(lambda: {"wallets": set(), "sol": 0.0, "sigs": []})
    for e in edges:
        if e.get("asset") != "SOL" or (e.get("amount") or 0) < min_sol:
            continue
        me, other = (e["dst"], e["src"]) if side == "in" else (e["src"], e["dst"])
        if me not in wallets or other in wallets or is_service(other, busy) or not on_curve(other):
            continue
        h = hit[other]
        h["wallets"].add(me)
        h["sol"] += e["amount"]
        if len(h["sigs"]) < 8:
            h["sigs"].append(e.get("sig"))
    return {a: {"wallets": sorted(h["wallets"]), "sol": round(h["sol"], 6), "sigs": h["sigs"]}
            for a, h in hit.items() if len(h["wallets"]) >= 2}


def edge_weight(e: dict, now: float, buy_ts: float | None = None, inflow_sol: float | None = None) -> float:
    """0..1: amount (SOL; tokens count as medium), share of what the wallet received, recency, and how close to the buy."""
    amt = float(e.get("amount") or 0)
    if e.get("asset") == "SOL":
        size = min(1.0, amt / 2.0)
        share = min(1.0, amt / inflow_sol) if inflow_sol else 0.5
    else:
        size, share = 0.5, 0.5
    age_d = max(0.0, (now - (e.get("ts") or now)) / 86400)
    recency = math.exp(-age_d / 30)
    near = 1.0
    if buy_ts and e.get("ts"):
        gap_h = max(0.0, (buy_ts - e["ts"]) / 3600)
        near = math.exp(-gap_h / 24)
    rep = min(1.0, 0.6 + 0.2 * (e.get("n") or 1))
    return round(min(1.0, (0.35 * size + 0.25 * share + 0.2 * recency + 0.2 * near) * rep), 3)


def is_service(addr: str, busy: set | None = None) -> bool:
    return KNOWN_LABELS.get(addr, (None, None))[1] in ("exchange", "fee") or addr in (busy or set())


def merge_edges(edges: list[dict]) -> dict[tuple, dict]:
    """(src, dst, asset) -> {amount, n, first, last, sigs}: repeated transfers are one weighted edge with all evidence."""
    out: dict[tuple, dict] = {}
    for e in edges:
        k = (e["src"], e["dst"], e["asset"])
        m = out.setdefault(k, {"src": e["src"], "dst": e["dst"], "asset": e["asset"], "amount": 0.0, "n": 0,
                               "first": e.get("ts"), "ts": e.get("ts"), "sigs": [], "ids": [], "kind": e.get("kind")})
        m["amount"] += float(e.get("amount") or 0)
        m["n"] += 1
        m["first"] = min(x for x in (m["first"], e.get("ts")) if x is not None) if (m["first"] or e.get("ts")) else None
        m["ts"] = max(x for x in (m["ts"], e.get("ts")) if x is not None) if (m["ts"] or e.get("ts")) else None
        if len(m["sigs"]) < 5:
            m["sigs"].append(e.get("sig"))
        m["ids"].append(e.get("id"))
    return out


def clusters(edges: list[dict], now: float, busy: set | None = None, min_w: float = CLUSTER_MIN_W) -> dict[str, int]:
    """Soft clusters: wallets joined by an edge whose weight passes min_w; services / exchanges never join anything."""
    parent: dict[str, str] = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for m in merge_edges(edges).values():
        if is_service(m["src"], busy) or is_service(m["dst"], busy):
            continue
        if edge_weight(m, now) >= min_w:
            ra, rb = find(m["src"]), find(m["dst"])
            if ra != rb:
                parent[ra] = rb
    roots: dict[str, int] = {}
    out = {}
    for a in list(parent):
        r = find(a)
        out[a] = roots.setdefault(r, len(roots) + 1)
    sizes = defaultdict(int)
    for c in out.values():
        sizes[c] += 1
    return {a: c for a, c in out.items() if sizes[c] >= 2}


def evidence_paths(edges: list[dict], start: str, known: set[str], max_hops: int = 3, before_ts: float | None = None,
                   window_s: float = 30 * 86400, busy: set | None = None, limit: int = 3) -> list[list[dict]]:
    """Paths known -> ... -> start along funding edges (followed backwards from `start`), each edge earlier than the
    one after it and inside the window before the buy; a service on the way ends the path. Shortest first."""
    into: dict[str, list[dict]] = defaultdict(list)
    for m in merge_edges(edges).values():
        into[m["dst"]].append(m)
    out, frontier = [], deque([(start, [], before_ts)])
    seen = {start}
    while frontier and len(out) < limit:
        node, path, t = frontier.popleft()
        if len(path) >= max_hops:
            continue
        for e in sorted(into.get(node, []), key=lambda e: -(e["amount"] or 0)):
            src = e["src"]
            if is_service(src, busy):
                continue
            if t is not None and e.get("first") is not None and (e["first"] > t or t - e["first"] > window_s):
                continue
            p = [e] + path
            if src in known:
                out.append(p)
                if len(out) >= limit:
                    break
            elif src not in seen:
                seen.add(src)
                frontier.append((src, p, e.get("first")))
    return out


def dispersal(edges: list[dict], buys: dict[str, float], min_leaves: int = DISPERSAL_MIN_LEAVES,
              window_s: float = DISPERSAL_WINDOW_S, busy: set | None = None) -> list[dict]:
    """Fan-out pattern: one source -> (intermediaries ->) many leaf wallets that then buy the same coin within the window.
    buys = wallet -> its first buy time of that coin. Returns one row per source with the leaves that bought."""
    outs: dict[str, list[dict]] = defaultdict(list)
    for m in merge_edges(edges).values():
        if not is_service(m["src"], busy):
            outs[m["src"]].append(m)
    rows = []
    for src, es in outs.items():
        leaves, total, hops = {}, 0.0, {}
        stack = [(e, 1) for e in es]
        seen = {src}
        while stack:
            e, h = stack.pop()
            d = e["dst"]
            if d in seen or h > 3:
                continue
            seen.add(d)
            if d in buys and e.get("first") is not None and 0 <= buys[d] - e["first"] <= window_s:
                leaves[d] = buys[d]
                hops[d] = h
                total += e["amount"] if e["asset"] == "SOL" else 0
            for e2 in outs.get(d, []):
                stack.append((e2, h + 1))
        if len(leaves) >= min_leaves:
            ts = sorted(leaves.values())
            rows.append({"source": src, "leaves": sorted(leaves), "n_leaves": len(leaves), "sol_to_leaves": round(total, 3),
                         "max_hops": max(hops.values()), "first_buy": ts[0], "last_buy": ts[-1]})
    return sorted(rows, key=lambda r: -r["n_leaves"])


# ---------------------------------------------------------------------------------------------------- bots / rank
def bot_features(evs: list[dict], first_by_mint: dict[str, float] | None = None) -> dict:
    """Explainable features of one wallet from its recorded events, and a label with reasons + confidence."""
    trades = sorted((e for e in evs if e["kind"] in ("BUY", "SELL", "CREATE")), key=lambda e: e.get("block_time") or 0)
    n = len(trades)
    ts = [e["block_time"] for e in trades if e.get("block_time")]
    span_d = max(1 / 24, (ts[-1] - ts[0]) / 86400) if len(ts) >= 2 else 1.0
    mints = {e["mint"] for e in trades}
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b >= a]
    buys = defaultdict(list)
    sells = defaultdict(list)
    for e in trades:
        (buys if e["kind"] in ("BUY", "CREATE") else sells)[e["mint"]].append(e.get("block_time") or 0)
    holds = [min(sells[m]) - min(b) for m, b in buys.items() if sells.get(m) and min(sells[m]) >= min(b)]
    sizes = [round(float(e.get("quote_sol") or 0), 2) for e in trades if e["kind"] == "BUY" and e.get("quote_sol")]
    top_size = max((sizes.count(s) for s in set(sizes)), default=0)
    entry = []
    for m, b in buys.items():
        f = (first_by_mint or {}).get(m)
        if f is not None:
            entry.append(max(0.0, min(b) - f))
    f = {"trades": n, "mints": len(mints), "mints_per_day": round(len(mints) / span_d, 2),
         "median_gap_s": round(statistics.median(gaps), 1) if gaps else None,
         "median_hold_s": round(statistics.median(holds), 1) if holds else None,
         "repeat_size_share": round(top_size / len(sizes), 2) if sizes else None,
         "median_entry_delay_s": round(statistics.median(entry), 1) if entry else None}
    why, label = [], "unknown"
    if n >= 3:
        if f["mints_per_day"] >= 8:
            why.append(f"{f['mints_per_day']:g} coin/ngày")
        if f["median_gap_s"] is not None and f["median_gap_s"] < 20 and n >= 10:
            why.append(f"khoảng cách lệnh trung vị {f['median_gap_s']:g}s")
        if f["median_entry_delay_s"] is not None and f["median_entry_delay_s"] < 5 and len(entry) >= 3:
            why.append(f"vào lệnh {f['median_entry_delay_s']:g}s sau giao dịch đầu của coin")
        if why:
            label = "bot-like"
        else:
            if f["mints_per_day"] >= 4:
                why.append(f"{f['mints_per_day']:g} coin/ngày")
            if f["repeat_size_share"] is not None and f["repeat_size_share"] >= 0.6 and len(sizes) >= 5:
                why.append(f"{int(f['repeat_size_share'] * 100)}% lệnh cùng một cỡ")
            if f["median_hold_s"] is not None and f["median_hold_s"] < 120:
                why.append(f"giữ trung vị {f['median_hold_s']:g}s")
            label = "automation-possible" if why else ("human-like" if n >= 5 else "unknown")
            if label == "human-like":
                why.append("nhịp giao dịch chậm, ít coin")
    conf = "low" if n < 5 else ("medium" if n < 15 else "high")
    if label == "unknown":
        why = why or [f"mới có {n} lệnh"]
    return {"label": label, "confidence": conf, "why": why, "features": f, "version": BOT_VERSION}


def rank_wallet(feat: dict) -> dict:
    """Score 0..1 from explainable parts (RANK_W); balance is one small part, never a gate."""
    parts = {"funding": feat.get("funding", 0.0), "early": min(1.0, feat.get("early_buys", 0) / 3),
             "coord": min(1.0, feat.get("coord_mints", 0) / 3), "fresh": feat.get("fresh", 0.0),
             "balance": min(1.0, (feat.get("balance") or 0) / 5)}
    score = sum(RANK_W[k] * v for k, v in parts.items())
    why = []
    if parts["funding"]:
        why.append(f"liên hệ funding {parts['funding']:.2f}" + (f" ({feat.get('hops')} bước)" if feat.get("hops") else ""))
    if feat.get("early_buys"):
        why.append(f"mua sớm {feat['early_buys']} coin theo dõi")
    if feat.get("coord_mints"):
        why.append(f"mua cùng ví nhóm ở {feat['coord_mints']} coin")
    if feat.get("balance") is not None:
        why.append(f"số dư {feat['balance']:g} SOL")
    certainty = "low" if feat.get("n_events", 0) < 3 and not feat.get("hops") else "medium" if feat.get("n_events", 0) < 10 else "high"
    return {"score": round(score, 3), "parts": {k: round(v, 3) for k, v in parts.items()}, "why": why,
            "certainty": certainty, "version": RANK_VERSION}


# ---------------------------------------------------------------------------------------------------- the worker
class Budget:
    """Token bucket of Helius calls per minute shared by the worker's steps."""

    def __init__(self, per_min: float, now=time.monotonic):
        self.per_min, self.now = per_min, now
        self.tokens, self.t = per_min, now()
        self.spent = 0

    def take(self, n: int = 1) -> bool:
        t = self.now()
        self.tokens = min(self.per_min, self.tokens + (t - self.t) * self.per_min / 60)
        self.t = t
        if self.tokens < n:
            return False
        self.tokens -= n
        self.spent += n
        return True


class Tracker:
    def __init__(self, store: Store, rpc, known=lambda: {}, group=lambda w: None, meta=None, paused=lambda: False,
                 now=time.time, budget: Budget | None = None):
        self.s, self.rpc, self.now = store, rpc, now
        self.known, self.group, self.meta, self.paused = known, group, meta, paused
        self.budget = budget or Budget(CALLS_PER_MIN)
        self.stat = {"ticks": 0, "calls": 0, "errors": 0, "last_error": None, "last_tick": None, "parsed": 0, "funding": 0}
        self.lock = threading.Lock()
        self.px: dict[str, tuple[float, float | None, str | None]] = {}
        self.rank_cache: dict[str, dict] = {}
        self.rank_ts = 0.0
        self.sol_usd = lambda: None                           # SOL price (the stream's Jupiter quote), set by the service
        self.coin_facts = lambda mint: {}                     # {symbol, name, supply_ui, decimals} known elsewhere

    # ------------------------------------------------------------ watchlist
    def seed(self) -> None:
        """Initial list (INSIDER_SEED_MINTS, `mint:name,...`): each mint added once, never re-added after a removal,
        never touching a coin already tracked; named coins without a sure mint go to pending_coins."""
        done = set(self.s.get("seeded_mints", []))
        if self.s.get("seeded"):                              # an earlier build seeded CHUCHU only
            done.add("6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump")
        items = []
        for part in (SEED_MINTS or "").split(","):
            m, _, name = part.strip().partition(":")
            if m.strip() and m.strip() not in done:
                items.append({"mint": m.strip(), "name": name.strip(), "note": "danh sách ban đầu", "history": True, "live": True})
        if items:
            self.add(items)
        self.s.put("seeded_mints", sorted(done | {x["mint"] for x in items}))
        for name, read, why in SEED_PENDING:
            self.s.x("INSERT OR IGNORE INTO pending_coins (name, read_mint, reason, added) VALUES (?,?,?,?)",
                     (name, read, why, self.now()))

    def pending(self) -> list[dict]:
        return self.s.q("SELECT * FROM pending_coins WHERE resolved_mint IS NULL ORDER BY added, name")

    def add(self, items: list[dict]) -> list[dict]:
        """Per item: added / already tracked / invalid / failed; a bad line never drops the good ones."""
        out = []
        if len(items) > BULK_MAX:
            return [{"mint": (x.get("mint") or "")[:60], "result": "failed", "error": f"tối đa {BULK_MAX} mint mỗi lần"} for x in items]
        n_now = len(self.s.mints())
        seen = set()
        for x in items:
            m = (x.get("mint") or "").strip()
            if not valid_mint(m):
                out.append({"mint": m[:60], "result": "invalid", "error": "không phải địa chỉ Solana hợp lệ"})
                continue
            if m in seen:
                out.append({"mint": m, "result": "already tracked", "error": "trùng trong danh sách gửi"})
                continue
            seen.add(m)
            try:
                old = self.s.mint(m)
                if old and not old["removed"]:
                    out.append({"mint": m, "result": "already tracked"})
                    continue
                if n_now >= MAX_MINTS:
                    out.append({"mint": m, "result": "failed", "error": f"đã đạt tối đa {MAX_MINTS} mint"})
                    continue
                hist, live = bool(x.get("history", True)), bool(x.get("live", True))
                tags = ",".join(t.strip().upper()[:20] for t in (x.get("tags") or []) if t.strip())[:200] \
                    if isinstance(x.get("tags"), list) else str(x.get("tags") or "")[:200]
                row = (str(x.get("name") or "")[:40], str(x.get("note") or "")[:300], tags, int(hist), int(live),
                       "queued", self.now(), self.now(), "todo" if hist else "skipped")
                if old:                                       # re-added after a removal: start over, events are kept
                    self.s.x("UPDATE mints SET name=?, note=?, tags=?, history=?, live=?, status=?, added_ts=?, updated_ts=?, "
                             "history_state=?, removed=0, newest_sig=NULL, back_before=NULL, pages=0, last_error=NULL, gap=0 "
                             "WHERE mint=?", (*row, m))
                else:
                    self.s.x("INSERT INTO mints (name, note, tags, history, live, status, added_ts, updated_ts, history_state, mint) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)", (*row, m))
                n_now += 1
                if x.get("name"):                             # a coin that was waiting for its address
                    self.s.x("UPDATE pending_coins SET resolved_mint=? WHERE UPPER(name)=UPPER(?) AND resolved_mint IS NULL",
                             (m, str(x["name"]).strip()))
                out.append({"mint": m, "result": "added"})
            except Exception as e:                            # one row failing never stops the others
                out.append({"mint": m, "result": "failed", "error": type(e).__name__})
        return out

    def action(self, mint: str, op: str) -> dict | None:
        m = self.s.mint(mint)
        if not m or (m["removed"] and op != "retry"):
            return None
        if op == "pause":
            self.s.set_mint(mint, status="paused")
        elif op == "resume":
            self.s.set_mint(mint, status="queued" if m["history_state"] in ("todo", "paging", "parsing") else "live", last_error=None)
        elif op == "retry":
            self.s.set_mint(mint, status="queued", last_error=None, errors=0, removed=0,
                            verified=None if m["verified"] in ("missing", "not_mint") else m["verified"])
        elif op == "remove":                                  # the watch stops; events / edges / wallets stay
            self.s.set_mint(mint, removed=1, status="paused")
            self.s.x("DELETE FROM queue WHERE mint=?", (mint,))
        else:
            return None
        return self.s.mint(mint)

    # ------------------------------------------------------------ RPC
    def _call(self, method: str, params: list):
        if not self.budget.take():
            raise _NoBudget()
        self.stat["calls"] += 1
        try:
            res = self.rpc(method, params)
        except Exception:
            self.s.usage("track", method, None, err=True)
            raise
        self.s.usage("track", method, res)
        return res

    def _tx(self, sig: str):
        return self._call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                   "maxSupportedTransactionVersion": 1}])

    # ------------------------------------------------------------ steps
    def step_verify(self, m: dict) -> bool:
        """The address must be a token mint on chain (owner = a token program, parsed type 'mint'); otherwise the coin
        stays listed with status error and the reason (nothing guessed)."""
        res = self._call("getAccountInfo", [m["mint"], {"encoding": "jsonParsed"}]) or {}
        v = res.get("value") if isinstance(res, dict) else None
        if not v:
            self.s.set_mint(m["mint"], verified="missing", status="error", last_error="địa chỉ không tồn tại trên chain")
            return False
        data = v.get("data") if isinstance(v.get("data"), dict) else {}
        p = data.get("parsed") or {}
        prog = TOKEN_PROGRAMS.get(v.get("owner"))
        if not prog or p.get("type") != "mint":
            self.s.set_mint(m["mint"], verified="not_mint", status="error", last_error="địa chỉ không phải token mint")
            return False
        info = p.get("info") or {}
        self.s.set_mint(m["mint"], verified="mint", token_program=prog, decimals=info.get("decimals"),
                        supply=str(info["supply"]) if info.get("supply") is not None else None)
        return True

    def step_history(self, m: dict) -> bool:
        """Page signatures backwards from the newest (checkpoint `back_before`); the oldest page holds the earliest
        transactions, whose EARLY_N oldest are queued for parsing. Bounded by HISTORY_MAX_PAGES (then 'partial')."""
        mint = m["mint"]
        opts = {"limit": 1000}
        if m["back_before"]:
            opts["before"] = m["back_before"]
        sigs = self._call("getSignaturesForAddress", [mint, opts]) or []
        upd = {"pages": (m["pages"] or 0) + 1, "sigs_seen": (m["sigs_seen"] or 0) + len(sigs), "status": "scanning",
               "history_state": "paging"}
        if not m["newest_sig"] and sigs:
            upd["newest_sig"] = sigs[0]["signature"]          # the live tail starts here: nothing between is lost
        if sigs:
            upd["back_before"] = sigs[-1]["signature"]
            oldest = [x for x in sigs if x.get("err") is None][::-1][:EARLY_N]
            self.s.put(f"early:{mint}", [[x["signature"], x.get("blockTime")] for x in oldest])
            upd["first_ts"] = sigs[-1].get("blockTime")
        done = len(sigs) < 1000
        if done or upd["pages"] >= HISTORY_MAX_PAGES:
            early = self.s.get(f"early:{mint}", [])
            for sig, _ in early:
                self.s.x("INSERT OR IGNORE INTO queue (sig, mint, why, added) VALUES (?,?,?,?)", (sig, mint, "history", self.now()))
            upd["history_state"] = "parsing"
            upd["history_note"] = ("đầy đủ từ giao dịch đầu tiên" if done else
                                   f"một phần: đã lùi {upd['pages']} trang ({upd['sigs_seen']} chữ ký), chưa tới giao dịch đầu")
        self.s.set_mint(mint, **upd)
        return True

    def _sigs_back(self, address: str, until: str | None, before: str | None, pages: int):
        """Signatures newer than `until` (older than `before`), newest first, 1000 a page, at most `pages` pages.
        Returns (signatures, complete). The budget running out mid-way ends the read as incomplete."""
        got: list[dict] = []
        for _ in range(max(1, pages)):
            opts = {"limit": 1000}
            if until:
                opts["until"] = until
            if before:
                opts["before"] = before
            try:
                sigs = self._call("getSignaturesForAddress", [address, opts]) or []
            except Exception:                                 # budget spent / RPC down after its retries
                if not got:
                    raise
                return got, False
            got += sigs
            if len(sigs) < 1000:
                return got, True
            before = sigs[-1]["signature"]
        return got, False

    def _queue(self, sigs: list[dict], mint: str, why: str) -> int:
        now, n = self.now(), 0
        for x in reversed(sigs):                              # oldest first
            if x.get("err") is None:
                n += self.s.x("INSERT OR IGNORE INTO queue (sig, mint, why, added) VALUES (?,?,?,?)",
                              (x["signature"], mint, why, now))
        return n

    def step_live(self, m: dict) -> int:
        """New signatures of a live mint back to its checkpoint (B2: paged, every one queued). Past LIVE_MAX_PAGES
        the checkpoint still moves to the newest, and the unread part is an open gap filled page by page."""
        mint, cp = m["mint"], m["newest_sig"]
        now = self.now()
        upd = {"last_poll": now}
        if not cp:                                            # first poll only sets the checkpoint
            sigs = self._call("getSignaturesForAddress", [mint, {"limit": 1000}]) or []
        else:
            sigs, complete = self._sigs_back(mint, cp, None, LIVE_MAX_PAGES)
            if sigs and not complete:
                self.s.open_gap("mint", mint, sigs[-1]["signature"], cp, f"live: quá {LIVE_MAX_PAGES} trang")
                upd["gap"] = (m["gap"] or 0) + 1
            self._queue(sigs, mint, "live")
        if sigs:
            upd["newest_sig"] = sigs[0]["signature"]
        if m["status"] in ("queued", "scanning") and m["history_state"] in ("done", "skipped"):
            upd["status"] = "live"
        upd["errors"] = 0                                     # a good poll: past transient errors do not add up
        self.s.set_mint(mint, **upd)
        return len(sigs)

    def step_gaps(self) -> int:
        """One page of the oldest open gap; single-transaction gaps (a full queue) go back into the queue."""
        room = QUEUE_MAX - self.s.q("SELECT COUNT(*) n FROM queue")[0]["n"]
        for g in self.s.gaps(scope="mint-tx"):
            if room <= QUEUE_MAX // 2:
                break
            if g["note"]:
                continue                                      # given up: stays open and visible
            if self.s.q("SELECT 1 FROM queue WHERE sig=? AND mint=?", (g["before_sig"], g["address"])):
                continue                                      # already waiting in the queue
            tries = self.s.q("SELECT COUNT(*) n FROM gaps WHERE scope='mint-tx' AND before_sig=?", (g["before_sig"],))[0]["n"]
            if tries >= 5:
                self.s.x("UPDATE gaps SET note=? WHERE id=?", ("bỏ thử lại sau 5 lượt", g["id"]))
                continue
            self.s.x("INSERT OR IGNORE INTO queue (sig, mint, why, added) VALUES (?,?,?,?)",
                     (g["before_sig"], g["address"], "gap", self.now()))
            self.s.x("UPDATE gaps SET closed=?, filled=1 WHERE id=?", (self.now(), g["id"]))
            room -= 1
        n = 0
        for g in self.s.gaps(scope="mint")[:GAP_PAGES_PER_TICK]:
            m = self.s.mint(g["address"])
            if not m or m["removed"] or m["status"] == "paused":
                continue
            sigs, complete = self._sigs_back(g["address"], g["until_sig"], g["before_sig"], 1)
            n += self._queue(sigs, g["address"], "gap")
            if complete:
                self.s.x("UPDATE gaps SET closed=?, filled=filled+? WHERE id=?", (self.now(), len(sigs), g["id"]))
            elif sigs:
                self.s.x("UPDATE gaps SET before_sig=?, filled=filled+? WHERE id=?", (sigs[-1]["signature"], len(sigs), g["id"]))
        return n

    def step_parse(self, limit: int = 10) -> int:
        rows = self.s.q("SELECT q.sig, q.mint, q.why FROM queue q JOIN mints m ON m.mint=q.mint WHERE m.removed=0 "
                        "AND m.status!='paused' ORDER BY (q.why='history') DESC, q.added LIMIT ?", (limit,))
        n = 0
        for r in rows:
            try:
                tx = self._tx(r["sig"])
            except _NoBudget:
                raise
            except Exception as e:                            # RPC down / final error: out of the queue, into a gap
                from insiders.vip import redact                # (tried again by step_gaps, 5 times at most)
                self.s.x("DELETE FROM queue WHERE sig=? AND mint=?", (r["sig"], r["mint"]))
                self.s.open_gap("mint-tx", r["mint"], r["sig"], None, redact(f"getTransaction lỗi: {type(e).__name__}"))
                n += 1
                break                                         # one failure a tick: an outage does not empty the queue
            self.s.x("DELETE FROM queue WHERE sig=? AND mint=?", (r["sig"], r["mint"]))
            n += 1
            if not tx:                                        # not served: an explicit one-transaction gap
                self.s.open_gap("mint-tx", r["mint"], r["sig"], None, "getTransaction không trả kết quả")
                continue
            src = {"history": "history", "gap": "backfill"}.get(r["why"], "mint-poll")
            try:
                self.ingest(tx, r["mint"], source=src, observed=self.now())
            except Exception as e:                            # a decoding bug: recorded, not silently dropped
                self.s.open_gap("mint-tx", r["mint"], r["sig"], None, f"lỗi giải mã: {type(e).__name__}")
                self._err(None, e)
        for m in self.s.q("SELECT mint FROM mints WHERE history_state='parsing'"):
            if not self.s.q("SELECT 1 FROM queue WHERE mint=? AND why='history' LIMIT 1", (m["mint"],)):
                self.s.set_mint(m["mint"], history_state="done")
        self.stat["parsed"] += n
        return n

    def ingest(self, tx: dict, mint: str | None, source: str, observed: float | None) -> list[dict]:
        """Store the events of a transaction; known wallets get their group; new buyers get a funding lookup."""
        evs = parse_mint_events(tx, mint)
        known = self.known()
        sig = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
        new = []
        for ev in evs:
            ev.update(sig=ev.get("sig") or sig, source=source, observed_at=observed, parsed_at=self.now())
            if self.s.add_event(ev):
                new.append(ev)
                if ev["kind"] in ("BUY", "CREATE", "RECEIVE") and ev["wallet"] not in known:
                    self.s.x("INSERT OR IGNORE INTO fund_todo (wallet, before_sig, before_ts, hop, mint, added) VALUES (?,?,?,?,?,?)",
                             (ev["wallet"], ev["sig"], ev.get("ts"), 1, ev["mint"], self.now()))
        if new:
            self._signals_for(new, known)
        return new

    def step_funding(self, limit: int = 2) -> int:
        """For a new buyer: read up to FUND_SIGS transactions before its buy and record transfers INTO it as edges;
        sources that are not services and not yet known are queued one hop further (up to FUND_HOPS)."""
        rows = self.s.q("SELECT * FROM fund_todo WHERE done=0 ORDER BY hop, added LIMIT ?", (limit,))
        n = 0
        for r in rows:
            w = r["wallet"]
            self.s.x("UPDATE fund_todo SET done=1 WHERE wallet=? AND mint=?", (w, r["mint"]))
            busy = self._busy(w)
            if busy:
                continue
            sigs = self._call("getSignaturesForAddress", [w, {"limit": FUND_SIGS, "before": r["before_sig"]}]) or []
            for x in sigs:
                if x.get("err") is not None:
                    continue
                if r["before_ts"] and x.get("blockTime") and r["before_ts"] - x["blockTime"] > FUND_WINDOW_S:
                    break
                tx = self._tx(x["signature"])
                if not tx:
                    continue
                for src, asset, amt in sol_and_token_in(tx, w):
                    if asset == "SOL" and amt < FUND_MIN_SOL:
                        continue
                    if not on_curve(src):
                        continue
                    if self.s.add_edge(src, w, asset, amt, tx.get("blockTime"), x["signature"], "fund" if asset == "SOL" else "token"):
                        n += 1
                    if r["hop"] < FUND_HOPS and src not in self.known() and not is_service(src):
                        self.s.x("INSERT OR IGNORE INTO fund_todo (wallet, before_sig, before_ts, hop, mint, added) VALUES (?,?,?,?,?,?)",
                                 (src, x["signature"], tx.get("blockTime"), r["hop"] + 1, r["mint"], self.now()))
        self.stat["funding"] += n
        return n

    def _busy(self, w: str) -> bool:
        """A hub (exchange / router / bot): >= 100 transactions at >= 1 per minute. Checked once per wallet."""
        if is_service(w):
            return True
        r = self.s.q("SELECT busy FROM wallet_info WHERE wallet=?", (w,))
        if r:
            return bool(r[0]["busy"])
        sigs = self._call("getSignaturesForAddress", [w, {"limit": 100}]) or []
        ts = [x.get("blockTime") for x in sigs if x.get("blockTime")]
        busy = len(ts) >= 100 and (max(ts) - min(ts)) / 60 <= len(ts) / BUSY_TX_PER_MIN
        self.s.x("INSERT OR REPLACE INTO wallet_info (wallet, busy, checked) VALUES (?,?,?)", (w, int(busy), self.now()))
        return busy

    # ------------------------------------------------------------ signals + outcomes
    def shadow_on(self) -> bool:
        return bool(self.s.get("shadow_on", True))

    def set_shadow(self, on: bool) -> bool:
        self.s.put("shadow_on", bool(on))
        return self.shadow_on()

    def _price(self, mint: str) -> tuple[float | None, str | None]:
        hit = self.px.get(mint)
        if hit and self.now() - hit[0] < 20:
            return hit[1], hit[2]
        info = {}
        try:
            info = self.meta(mint) if self.meta else {}
        except Exception:
            info = {}
        p, src = (info or {}).get("price_usd"), (info or {}).get("src")
        self.px[mint] = (self.now(), float(p) if p else None, src)
        if len(self.px) > 3000:
            for k in sorted(self.px, key=lambda k: self.px[k][0])[:1000]:
                del self.px[k]
        return self.px[mint][1], src

    def record_signal(self, rule: str, mint: str | None, wallets: list[str], block_time, evidence: dict,
                      dedup: str | None = None) -> int | None:
        """One snapshot per signal: rule + version, time, mint, wallets, reference price + source, coverage, evidence;
        outcome rows are scheduled at each horizon when measurement is on."""
        now = self.now()
        price, src = (self._price(mint) if mint and self.shadow_on() else (None, None))
        cov = {"price": price is not None, "block_time": block_time is not None, "measured": self.shadow_on()}
        try:
            with self.s.lock:
                cur = self.s.db.execute(
                    "INSERT OR IGNORE INTO signals (rule, rules_version, ts, block_time, mint, wallets, ref_price, price_src, "
                    "coverage, evidence, latency_s, dedup) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rule, RULES_VERSION, now, block_time, mint, json.dumps(wallets[:20]), price, src, json.dumps(cov),
                     json.dumps(evidence)[:20000], (now - block_time) if block_time else None, dedup))
                self.s.db.commit()
                sid = cur.lastrowid if cur.rowcount else None
        except sqlite3.Error:
            return None
        if sid and mint and self.shadow_on():
            for h in HORIZONS:
                self.s.x("INSERT OR IGNORE INTO outcomes (signal_id, horizon, due) VALUES (?,?,?)", (sid, h, now + h))
        return sid

    def _signals_for(self, new: list[dict], known: dict) -> None:
        """KNOWN_BUY: a known group wallet bought a tracked coin. LINKED_BUY: a buyer with an evidence path to a known
        wallet. DISPERSAL: the fan-out pattern on that coin. All are observations, not advice."""
        for ev in new:
            if ev["kind"] not in ("BUY", "CREATE"):
                continue
            if ev["wallet"] in known:
                self.record_signal("KNOWN_BUY", ev["mint"], [ev["wallet"]], ev.get("ts"),
                                   {"sig": ev["sig"], "group": self.group(ev["wallet"]), "sol": ev.get("sol"),
                                    "confidence": ev.get("confidence")}, dedup=f"KB|{ev['mint']}|{ev['wallet']}")

    def check_links(self, mint: str) -> int:
        """After funding lookups landed: buyers of `mint` with an evidence path to a known wallet -> LINKED_BUY,
        and the dispersal pattern -> DISPERSAL (each once per coin / wallet)."""
        known = set(self.known())
        edges = self.s.edges()
        if not edges:
            return 0
        busy = {r["wallet"] for r in self.s.q("SELECT wallet FROM wallet_info WHERE busy=1")}
        buys = {}
        for r in self.s.q("SELECT wallet, MIN(block_time) t, MIN(sig) sig FROM events WHERE mint=? AND kind IN ('BUY','CREATE') GROUP BY wallet", (mint,)):
            buys[r["wallet"]] = (r["t"], r["sig"])
        n = 0
        for w, (t, sig) in buys.items():
            if w in known:
                continue
            paths = evidence_paths(edges, w, known, max_hops=FUND_HOPS + 1, before_ts=t, window_s=FUND_WINDOW_S, busy=busy)
            if paths:
                p = paths[0]
                ev = {"buy_sig": sig, "paths": [[{"src": e["src"], "dst": e["dst"], "asset": e["asset"], "amount": round(e["amount"], 4),
                                                  "ts": e["first"], "sigs": e["sigs"]} for e in pp] for pp in paths],
                      "independent_paths": len(paths), "hops": len(p), "known": p[0]["src"], "group": self.group(p[0]["src"])}
                if self.record_signal("LINKED_BUY", mint, [w, p[0]["src"]], t, ev, dedup=f"LB|{mint}|{w}"):
                    n += 1
        for row in dispersal(edges, {w: t for w, (t, _) in buys.items()}, busy=busy):
            if self.record_signal("DISPERSAL", mint, [row["source"]] + row["leaves"][:15], row["last_buy"], row,
                                  dedup=f"DS|{mint}|{row['source']}"):
                n += 1
        return n

    def step_outcomes(self, limit: int = 20) -> int:
        """Prices at each due horizon. Missing price = a note, never an interpolated value."""
        rows = self.s.q("SELECT o.signal_id, o.horizon, s.mint, s.ref_price, s.ts FROM outcomes o JOIN signals s ON s.id=o.signal_id "
                        "WHERE o.done IS NULL AND o.due<=? ORDER BY o.due LIMIT ?", (self.now(), limit))
        for r in rows:
            p, _ = self._price(r["mint"])
            ref = r["ref_price"]
            if p is None or not ref:
                self.s.x("UPDATE outcomes SET done=?, note=? WHERE signal_id=? AND horizon=?",
                         (self.now(), "không có giá" if p is None else "không có giá lúc phát", r["signal_id"], r["horizon"]))
                continue
            ch = p / ref - 1
            prev = self.s.q("SELECT MAX(max_change) mx, MIN(min_change) mn FROM outcomes WHERE signal_id=? AND done IS NOT NULL", (r["signal_id"],))[0]
            mx = max(ch, prev["mx"] if prev["mx"] is not None else ch)
            mn = min(ch, prev["mn"] if prev["mn"] is not None else ch)
            late = self.now() - (r["ts"] + r["horizon"])
            self.s.x("UPDATE outcomes SET done=?, price=?, change=?, max_change=?, min_change=?, note=? WHERE signal_id=? AND horizon=?",
                     (self.now(), p, ch, mx, mn, f"đo trễ {int(late)}s" if late > 120 else None, r["signal_id"], r["horizon"]))
        return len(rows)

    # ------------------------------------------------------------ views
    def metrics(self) -> dict:
        """Per rule: signals, coverage, latency median / p90, share with a price, outcome distribution per horizon.
        Past results do not promise future ones."""
        out = []
        for rule in [r["rule"] for r in self.s.q("SELECT DISTINCT rule FROM signals")]:
            sg = self.s.q("SELECT id, ref_price, latency_s FROM signals WHERE rule=?", (rule,))
            lat = sorted(x["latency_s"] for x in sg if x["latency_s"] is not None)
            hz = []
            for h in HORIZONS:
                oc = self.s.q("SELECT o.change FROM outcomes o JOIN signals s ON s.id=o.signal_id WHERE s.rule=? AND o.horizon=? AND o.done IS NOT NULL", (rule, h))
                ch = sorted(x["change"] for x in oc if x["change"] is not None)
                hz.append({"horizon": h, "measured": len(oc), "with_price": len(ch),
                           "median": round(statistics.median(ch), 4) if ch else None,
                           "p10": round(ch[int(0.1 * (len(ch) - 1))], 4) if ch else None,
                           "p90": round(ch[int(0.9 * (len(ch) - 1))], 4) if ch else None,
                           "up": sum(1 for c in ch if c > 0)})
            out.append({"rule": rule, "signals": len(sg), "with_ref_price": sum(1 for x in sg if x["ref_price"]),
                        "latency_median_s": round(statistics.median(lat), 1) if lat else None,
                        "latency_p90_s": round(lat[int(0.9 * (len(lat) - 1))], 1) if lat else None, "horizons": hz})
        return {"on": self.shadow_on(), "rules_version": RULES_VERSION, "horizons": HORIZONS, "rules": out}

    def latency(self) -> dict:
        """block time -> seen (socket / poll), seen -> parsed, per source, over the last 500 events."""
        rows = self.s.q("SELECT source, block_time, observed_at, parsed_at FROM events WHERE block_time IS NOT NULL "
                        "AND observed_at IS NOT NULL ORDER BY id DESC LIMIT 500")
        by = defaultdict(lambda: {"seen": [], "parse": []})
        for r in rows:
            by[r["source"]]["seen"].append(max(0.0, r["observed_at"] - r["block_time"]))
            if r["parsed_at"]:
                by[r["source"]]["parse"].append(max(0.0, r["parsed_at"] - r["observed_at"]))

        def q(xs, p):
            xs = sorted(xs)
            return round(xs[int(p * (len(xs) - 1))], 1) if xs else None
        return {src: {"n": len(v["seen"]), "seen_median_s": q(v["seen"], 0.5), "seen_p90_s": q(v["seen"], 0.9),
                      "parse_median_s": q(v["parse"], 0.5)} for src, v in by.items()}

    def coins(self) -> list[dict]:
        out = []
        for m in self.s.mints():
            c = self.s.q("SELECT COUNT(DISTINCT wallet) n, COUNT(*) e FROM events WHERE mint=? AND kind IN ('BUY','CREATE')", (m["mint"],))[0]
            qn = self.s.q("SELECT COUNT(*) n FROM queue WHERE mint=?", (m["mint"],))[0]["n"]
            status = m["status"]
            if status == "live" and m["last_poll"] and self.now() - m["last_poll"] > max(300, 10 * LIVE_MIN_S):
                status = "stale"
            out.append({**{k: m[k] for k in ("mint", "name", "note", "tags", "history", "live", "added_ts", "updated_ts",
                                            "history_state", "history_note", "pages", "sigs_seen", "gap", "last_poll",
                                            "last_event_ts", "last_error", "first_ts", "newest_sig", "back_before",
                                            "verified", "token_program", "decimals")},
                        "status": status, "buyers": c["n"], "buys": c["e"], "queue": qn,
                        # open gaps: the coin's data is not complete while this is > 0 (never shown as finished)
                        "gaps_open": self.s.q("SELECT COUNT(*) n FROM gaps WHERE closed IS NULL AND address=? AND "
                                              "scope IN ('mint','mint-tx')", (m["mint"],))[0]["n"]})
        return out

    def coin_detail(self, mint: str) -> dict | None:
        m = next((c for c in self.coins() if c["mint"] == mint), None)
        evs = self.s.q("SELECT * FROM events WHERE mint=? ORDER BY block_time ASC LIMIT 2000", (mint,))
        if m is None and not evs:
            return None
        known = self.known()
        first: dict[str, dict] = {}
        for e in evs:
            if e["kind"] in ("BUY", "CREATE") and e["wallet"] not in first:
                first[e["wallet"]] = e
        sigs = self.s.q("SELECT id, rule, ts, wallets, evidence FROM signals WHERE mint=? ORDER BY ts DESC LIMIT 100", (mint,))
        linked = {}
        for s in sigs:
            if s["rule"] == "LINKED_BUY":
                ws = json.loads(s["wallets"])
                linked[ws[0]] = json.loads(s["evidence"])
        bots = self.bot_labels([w for w in first][:300])
        buyers = []
        for rank, (w, e) in enumerate(sorted(first.items(), key=lambda kv: kv[1]["block_time"] or 0), 1):
            buyers.append({"wallet": w, "rank": rank, "ts": e["block_time"], "sol": e["quote_sol"], "sig": e["sig"],
                           "confidence": e["confidence"], "known": w in known, "group": self.group(w) if w in known else None,
                           "candidate": w in linked, "evidence": linked.get(w), "bot": bots.get(w)})
        return {"coin": m, "events": evs[-500:][::-1], "buyers": buyers[:300],
                "signals": [{**s, "wallets": json.loads(s["wallets"]), "evidence": json.loads(s["evidence"])} for s in sigs],
                "graph": self.graph(mint=mint)}

    def events(self, mint=None, kind=None, wallet=None, since=None, limit=300) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if mint:
            sql += " AND mint=?"
            args.append(mint)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if wallet:
            sql += " AND wallet=?"
            args.append(wallet)
        if since:
            sql += " AND block_time>=?"
            args.append(since)
        sql += " ORDER BY COALESCE(block_time, parsed_at) DESC LIMIT ?"
        args.append(min(int(limit), 1000))
        rows = self.s.q(sql, args)
        known = self.known()
        for r in rows:
            r["group"] = self.group(r["wallet"]) if r["wallet"] in known else None
        return rows

    def graph(self, mint: str | None = None, wallet: str | None = None, max_nodes: int = 150) -> dict:
        """Nodes + merged edges around a coin's buyers (or one wallet), capped; every edge keeps its signatures."""
        edges = self.s.edges()
        known = self.known()
        busy = {r["wallet"] for r in self.s.q("SELECT wallet FROM wallet_info WHERE busy=1")}
        focus: set[str] = set()
        if mint:
            focus |= {r["wallet"] for r in self.s.q("SELECT DISTINCT wallet FROM events WHERE mint=? AND kind IN ('BUY','CREATE') LIMIT 400", (mint,))}
        if wallet:
            focus.add(wallet)
        merged = list(merge_edges(edges).values())
        if focus:
            keep, frontier = set(focus), set(focus)
            for _ in range(FUND_HOPS + 1):
                nxt = {m["src"] for m in merged if m["dst"] in frontier} | {m["dst"] for m in merged if m["src"] in frontier and wallet}
                nxt -= keep
                keep |= nxt
                frontier = nxt
            merged = [m for m in merged if m["src"] in keep and m["dst"] in keep]
        now = self.now()
        merged.sort(key=lambda m: -edge_weight(m, now))
        nodes: dict[str, dict] = {}
        out_edges = []
        for m in merged:
            if len(nodes) >= max_nodes and (m["src"] not in nodes or m["dst"] not in nodes):
                continue
            for a in (m["src"], m["dst"]):
                if a not in nodes:
                    nodes[a] = {"id": a, "known": a in known, "group": self.group(a) if a in known else None,
                                "service": is_service(a, busy), "label": KNOWN_LABELS.get(a, (None,))[0],
                                "buyer": a in focus and bool(mint)}
            out_edges.append({"src": m["src"], "dst": m["dst"], "asset": m["asset"], "amount": round(m["amount"], 4),
                              "n": m["n"], "first": m["first"], "last": m["ts"], "sigs": m["sigs"], "ids": m["ids"][:20],
                              "w": edge_weight(m, now), "kind": m["kind"]})
        cl = clusters(edges, now, busy)
        for a, n in nodes.items():
            n["cluster"] = cl.get(a)
        return {"nodes": list(nodes.values()), "edges": out_edges, "truncated": len(nodes) >= max_nodes,
                "dispersal": dispersal(edges, {w: t for w, t in ((r["wallet"], r["t"]) for r in self.s.q(
                    "SELECT wallet, MIN(block_time) t FROM events WHERE mint=? AND kind IN ('BUY','CREATE') GROUP BY wallet", (mint,)))}, busy=busy)
                if mint else []}

    def _facts(self, mint: str, cache: dict) -> dict:
        """Name / symbol / decimals / supply of a coin: the watchlist row first, then what the stream knows."""
        if mint in cache:
            return cache[mint]
        f = {}
        try:
            f = dict(self.coin_facts(mint) or {})
        except Exception:
            f = {}
        m = self.s.mint(mint)
        if m:
            f["name"] = f.get("name") or m.get("name")
            if m.get("decimals") is not None:
                f["decimals"] = m["decimals"]
            if m.get("supply") and m.get("decimals") is not None:
                f["supply_ui"] = int(m["supply"]) / 10 ** m["decimals"]
            f["tracked"] = not m["removed"]
        cache[mint] = f
        return f

    def radar(self, window_s: float = 86400, limit: int = 150, kinds=("BUY", "CREATE")) -> dict:
        """Newest buys seen (followed wallets on any coin + every buyer of a watched coin): wallet, CA, SOL paid, market
        cap at the buy (price paid x supply; null when supply / decimals are unknown: never guessed), rank or
        evidence, where the wallet's SOL came from, latency block -> seen. Coverage is stated, not implied."""
        since = self.now() - window_s
        q = ",".join("?" * len(kinds))
        rows = self.s.q(f"SELECT * FROM events WHERE kind IN ({q}) AND block_time>=? ORDER BY block_time DESC LIMIT ?",
                        (*kinds, since, min(int(limit), 500)))
        known = self.known()
        sol_usd = None
        try:
            sol_usd = self.sol_usd()
        except Exception:
            pass
        linked = {}
        for s in self.s.q("SELECT wallets, evidence FROM signals WHERE rule='LINKED_BUY' AND ts>=?", (since - 7 * 86400,)):
            ws = json.loads(s["wallets"])
            if ws:
                linked[ws[0]] = json.loads(s["evidence"])
        wallets = sorted({r["wallet"] for r in rows})
        bots = self.bot_labels(wallets[:200])
        facts: dict = {}
        out = []
        for r in rows:
            f = self._facts(r["mint"], facts)
            ui = None
            if f.get("decimals") is not None:
                try:
                    ui = int(float(r["token_delta"] or 0)) / 10 ** f["decimals"]
                except ValueError:
                    ui = None
            mc = None
            if ui and r["quote_sol"] and f.get("supply_ui") and sol_usd:
                mc = round(r["quote_sol"] / ui * sol_usd * f["supply_ui"])
            fund = self.s.q("SELECT src, asset, amount, ts, sig FROM edges WHERE dst=? AND hidden=0 AND kind NOT LIKE 'research_%' AND (ts IS NULL OR ts<=?) "
                            "ORDER BY (asset='SOL') DESC, amount DESC LIMIT 1", (r["wallet"], r["block_time"] or self.now()))
            src = fund[0] if fund else None
            if src:
                src = {**src, "known": src["src"] in known, "group": self.group(src["src"]) if src["src"] in known else None,
                       "label": KNOWN_LABELS.get(src["src"], (None,))[0]}
            rk = self.rank_cache.get(r["wallet"])
            out.append({"sig": r["sig"], "ts": r["block_time"], "kind": r["kind"], "wallet": r["wallet"], "mint": r["mint"],
                        "symbol": f.get("symbol"), "name": f.get("name"), "tracked_coin": bool(f.get("tracked")),
                        "sol": r["quote_sol"], "usd": round(r["quote_sol"] * sol_usd, 2) if r["quote_sol"] and sol_usd else None,
                        "tokens_ui": ui, "mc_usd": mc, "confidence": r["confidence"], "source": r["source"],
                        "latency_s": round(r["observed_at"] - r["block_time"], 1) if r["observed_at"] and r["block_time"] else None,
                        "known": r["wallet"] in known, "group": self.group(r["wallet"]) if r["wallet"] in known else None,
                        "rank": {k: rk[k] for k in ("score", "why", "certainty")} if rk else None,
                        "candidate": linked.get(r["wallet"]), "funding": src, "bot": bots.get(r["wallet"])})
        return {"buys": out, "window_s": window_s, "sol_usd": sol_usd, "now": self.now(),
                "coverage": {"mode": "per-wallet + per-coin",
                             "note": "Chỉ thấy lệnh mua của ví đang theo dõi (mọi coin) và mọi người mua của coin trong danh "
                                     "sách. Chưa quét toàn bộ chương trình / pool: ví lạ mua coin ngoài danh sách thì không thấy."}}

    def hide_edge(self, edge_id: int, hidden: bool) -> bool:
        return self.s.x("UPDATE edges SET hidden=? WHERE id=?", (int(hidden), int(edge_id))) > 0

    def bot_labels(self, wallets: list[str]) -> dict[str, dict]:
        out = {}
        if not wallets:
            return out
        first = {r["mint"]: r["t"] for r in self.s.q("SELECT mint, MIN(block_time) t FROM events GROUP BY mint")}
        for w in wallets:
            evs = self.s.q("SELECT kind, mint, block_time, quote_sol FROM events WHERE wallet=? ORDER BY block_time DESC LIMIT 400", (w,))
            out[w] = bot_features(evs, first)
        return out

    def rank_children(self, vip) -> dict[str, dict]:
        """Rank of every known parent / child wallet of the stream (features + reasons): funding relevance from the
        adoption link (SOL, hops), early buys of tracked coins, buys alongside group wallets, freshness, balance."""
        kids = (vip.state.get("children") or {}) if vip is not None else {}
        seen = (vip.state.get("last_seen") or {}) if vip is not None else {}
        known = set(getattr(vip, "base", {}) or {}) if vip is not None else set()
        early = defaultdict(int)
        for r in self.s.q("SELECT mint FROM mints"):
            rows = self.s.q("SELECT wallet, MIN(block_time) t FROM events WHERE mint=? AND kind IN ('BUY','CREATE') "
                            "GROUP BY wallet ORDER BY t LIMIT 50", (r["mint"],))
            for x in rows:
                early[x["wallet"]] += 1
        coord = defaultdict(set)
        rows = self.s.q("SELECT wallet, mint, block_time FROM events WHERE kind IN ('BUY','CREATE') AND block_time IS NOT NULL "
                        "ORDER BY block_time DESC LIMIT 20000")
        by_mint = defaultdict(list)
        for x in rows:
            by_mint[x["mint"]].append(x)
        for m, xs in by_mint.items():
            kt = [x["block_time"] for x in xs if x["wallet"] in known]
            if not kt:
                continue
            for x in xs:
                if x["wallet"] not in known and any(abs(x["block_time"] - t) <= COORD_WINDOW_S for t in kt):
                    coord[x["wallet"]].add(m)
        nev = defaultdict(int)
        for x in rows:
            nev[x["wallet"]] += 1
        now, out = self.now(), {}
        for a, v in kids.items():
            hops, w = 1, a
            seen_w = set()
            while w in kids and kids[w].get("parent") not in known and kids[w].get("parent") in kids and w not in seen_w:
                seen_w.add(w)
                hops += 1
                w = kids[w]["parent"]
            sol = float(v.get("sol") or 0)
            funding = min(1.0, (0.4 + 0.6 * min(1.0, sol / 5)) * (0.7 ** (hops - 1))) if v.get("parent") else 0.0
            last = max(v.get("since") or 0, seen.get(a) or 0)
            feat = {"funding": funding, "hops": hops, "early_buys": early.get(a, 0), "coord_mints": len(coord.get(a, ())),
                    "fresh": math.exp(-max(0.0, now - last) / (72 * 3600)) if last else 0.0, "balance": v.get("bal"),
                    "n_events": nev.get(a, 0)}
            out[a] = {**rank_wallet(feat), "features": feat}
        self.rank_cache, self.rank_ts = out, now
        return out

    def status(self) -> dict:
        ms = self.coins()
        by = defaultdict(int)
        for m in ms:
            by[m["status"]] += 1
        last = self.s.q("SELECT MAX(block_time) t, MAX(parsed_at) p FROM events")[0]
        return {"mints": len(ms), "by_status": dict(by), "queue": self.s.q("SELECT COUNT(*) n FROM queue")[0]["n"],
                "funding_todo": self.s.q("SELECT COUNT(*) n FROM fund_todo WHERE done=0")[0]["n"],
                "events": self.s.q("SELECT COUNT(*) n FROM events")[0]["n"], "edges": self.s.q("SELECT COUNT(*) n FROM edges WHERE kind NOT LIKE 'research_%'")[0]["n"],
                "signals": self.s.q("SELECT COUNT(*) n FROM signals")[0]["n"], "latest_block_time": last["t"],
                "latest_parsed": last["p"], "budget_per_min": self.budget.per_min, "calls": self.stat["calls"],
                "paused": bool(self.paused()), "shadow_on": self.shadow_on(), "schema": self.s.version(),
                "last_tick": self.stat["last_tick"], "last_error": self.stat["last_error"], "latency": self.latency(),
                "gaps_open": self.s.q("SELECT COUNT(*) n FROM gaps WHERE closed IS NULL")[0]["n"],
                "rpc_today": self.s.q("SELECT COALESCE(SUM(calls),0) n FROM rpc_usage WHERE day=?",
                                      (time.strftime("%Y-%m-%d", time.gmtime(self.now())),))[0]["n"]}

    # ------------------------------------------------------------ loop
    def tick(self) -> None:
        """One bounded round: outcomes (no Helius), then live tails due, parsing, history paging, funding."""
        self.stat["ticks"] += 1
        self.stat["last_tick"] = self.now()
        try:
            self.step_outcomes()
        except Exception as e:
            self._err(None, e)
        if self.paused():
            return
        ms = [m for m in self.s.mints() if m["status"] not in ("paused", "error")]
        for m in [m for m in ms if not m["verified"]][:2]:
            self._guard(m, self.step_verify, m)
        ms = [m for m in self.s.mints() if m["status"] not in ("paused", "error") and m["verified"] == "mint"]
        live = [m for m in ms if m["live"]]
        every = max(LIVE_MIN_S, len(live) * 60 / max(1.0, self.budget.per_min * 0.4))
        for m in sorted(live, key=lambda m: m["last_poll"] or 0):
            if m["last_poll"] and self.now() - m["last_poll"] < every:
                continue
            if m["history_state"] in ("todo", "paging") and m["newest_sig"] is None and m["history"]:
                continue                                      # the history pass sets the checkpoint first
            self._guard(m, self.step_live, m)
        try:
            self.step_parse(limit=8)
        except _NoBudget:
            pass
        except Exception as e:
            self._err(None, e)
        for m in ms:
            if m["history"] and m["history_state"] in ("todo", "paging"):
                self._guard(m, self.step_history, m)
                break                                         # one page per tick: history never starves the live tail
            if not m["history"] and m["status"] == "queued":
                self.s.set_mint(m["mint"], status="live", history_state="skipped")
        try:
            if self.step_funding(limit=1):
                for m in ms:
                    self.check_links(m["mint"])
        except _NoBudget:
            pass
        except Exception as e:
            self._err(None, e)
        try:
            self.step_gaps()
        except _NoBudget:
            pass
        except Exception as e:
            self._err(None, e)
        self.trim_queue()

    def trim_queue(self) -> int:
        over = self.s.q("SELECT COUNT(*) n FROM queue")[0]["n"] - QUEUE_MAX
        if over > 0:                                          # bounded: the oldest live items leave the queue, each one
            for r in self.s.q("SELECT sig, mint FROM queue WHERE why='live' ORDER BY added LIMIT ?", (over,)):
                self.s.x("DELETE FROM queue WHERE sig=? AND mint=?", (r["sig"], r["mint"]))   # an open gap re-queued
                self.s.x("UPDATE mints SET gap=COALESCE(gap,0)+1 WHERE mint=?", (r["mint"],))  # when there is room
                self.s.open_gap("mint-tx", r["mint"], r["sig"], None, "hàng đợi đầy")
        return max(0, over)

    def _guard(self, m: dict, fn, *a):
        try:
            return fn(*a)
        except _NoBudget:
            return None
        except Exception as e:
            self._err(m, e)

    def _err(self, m: dict | None, e: Exception) -> None:
        from insiders.vip import redact
        msg = redact(f"{type(e).__name__}: {str(e)[:120]}")
        self.stat["errors"] += 1
        self.stat["last_error"] = msg
        if m is not None:
            errs = (m.get("errors") or 0) + 1
            self.s.set_mint(m["mint"], last_error=msg, errors=errs, status="error" if errs >= 5 else m["status"])

    def run_forever(self, stop: threading.Event, every_s: float = 2.0, vip=None) -> None:
        self.seed()
        while not stop.is_set():
            self.tick()
            if vip is not None and self.now() - self.rank_ts > 120:
                try:
                    vip.rank = self.rank_children(vip)
                except Exception as e:
                    self._err(None, e)
            stop.wait(every_s)


class _NoBudget(Exception):
    """The minute's Helius budget is spent: the step waits for the next tick."""
