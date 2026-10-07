"""S3 PRE_SNIPER_SIGNAL (pre-registered in docs/prereg_sniper_books.md, section S3). RESEARCH / PAPER ONLY.

Question: before the sniper wallet BwWK17cb buys, does the early transaction flow of a new token show a fingerprint
that predicts sniper money? S3 never waits for, never looks at and never learns from BwWK17cb:
  * BwWK17cb's transactions are dropped before any feature is computed (they do not exist for S3);
  * a fingerprint is only evaluated on events RECEIVED up to the end of its window (creation receive time + W);
  * if a BwWK17cb buy of that token was received before the signal time, the signal is INVALID_BWWK (stored, never
    traded, never counted); BwWK17cb's later buy time is stored only for the post-hoc latency metric.
Clock: the bot's receive clock (ms) — the clock a live bot decides on; on-chain block times only have 1 s resolution.

Windows (one independent experiment / paper book each, all reported): 100, 250, 500, 1000, 2000, 3000 ms.
Fingerprint at the end of the window (thresholds fixed a priori, identical for every window):
  early buyers (distinct non-creator buying wallets) >= 3; SOL inflow from them >= 1.0 SOL; buy share
  (buy SOL / (buy + sell SOL) of non-creators) >= 0.8; top buyer share of that inflow <= 0.6; creator holds
  <= 10 % of the supply; creator's stored risk not HIGH RISK / REPEAT FAILURE / SUSPICIOUS / RUG HISTORY.
Entry: paper buy ~$50 on the curve state 1 s after the signal (S2b's latency). One entry per token per window.
Exit: TP +50 %, SL -30 % (net sale value), 5 min max, migration = last curve price. Costs = the shared model."""
from __future__ import annotations

import sqlite3
import statistics
import time
from pathlib import Path

from kolbot.engine import LAMPORTS, Pending
from kolbot.sniper import BAD_DEV_RISKS, PINNED_WALLET

WINDOWS_MS = (100, 250, 500, 1000, 2000, 3000)
MIN_BUYERS = 3
MIN_INFLOW_SOL = 1.0
MIN_BUY_SHARE = 0.8
MAX_TOP_SHARE = 0.6
MAX_DEV_FRAC = 0.10
FILL_DELAY_S = 1.0
RAW_SUPPLY = 1e15
MAX_CANDS = 5000
LATENCY_TRACK_S = 1800            # keep watching a signalled token for a later BwWK buy (post-hoc metric only)

SCHEMA = """
CREATE TABLE IF NOT EXISTS s3_signals (id INTEGER PRIMARY KEY, window_ms INTEGER, mint TEXT, creator TEXT,
    create_recv REAL, signal_ts REAL, status TEXT, buyers INTEGER, txs INTEGER, inflow_sol REAL, buy_share REAL,
    top_share REAL, dev_frac REAL, accel REAL, fill_ts REAL, skip_reason TEXT, bwwk_buy_recv REAL,
    UNIQUE (window_ms, mint));
CREATE TABLE IF NOT EXISTS s3_meta (k TEXT PRIMARY KEY, v REAL);
"""


def features(events: list[tuple], creator: str | None, t_end: float) -> dict:
    """Fingerprint features from events received <= t_end. events: (recv, user, is_buy, sol, token)."""
    seen = [e for e in events if e[0] <= t_end]
    buys: dict[str, float] = {}
    buy_sol = sell_sol = dev_tok = 0.0
    for recv, user, is_buy, sol, tok in seen:
        if user == creator:
            dev_tok += tok if is_buy else -tok
            continue
        if is_buy:
            buys[user] = buys.get(user, 0.0) + sol
            buy_sol += sol
        else:
            sell_sol += sol
    first = min((e[0] for e in seen), default=t_end)
    mid = first + (t_end - first) / 2
    early = sum(1 for e in seen if e[0] <= mid and e[1] != creator)
    late = sum(1 for e in seen if e[0] > mid and e[1] != creator)
    return {"buyers": len(buys), "txs": sum(1 for e in seen if e[1] != creator),
            "inflow_sol": buy_sol, "buy_share": buy_sol / (buy_sol + sell_sol) if buy_sol + sell_sol else 0.0,
            "top_share": max(buys.values()) / buy_sol if buy_sol else 1.0,
            "dev_frac": max(0.0, dev_tok) / RAW_SUPPLY, "accel": (late + 1) / (early + 1)}


def fingerprint_ok(f: dict) -> bool:
    return (f["buyers"] >= MIN_BUYERS and f["inflow_sol"] >= MIN_INFLOW_SOL and f["buy_share"] >= MIN_BUY_SHARE
            and f["top_share"] <= MAX_TOP_SHARE and f["dev_frac"] <= MAX_DEV_FRAC)


class PreSniper:
    def __init__(self, db_path: Path, books: dict, dev_risk, sniper: str = PINNED_WALLET, clock=time.time):
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.books, self.dev_risk, self.sniper, self.clock = books, dev_risk, sniper, clock
        self.cands: dict[str, dict] = {}
        self.pending: list[tuple] = []                 # (due, window_ms, mint, row_id)
        self.signalled: dict[str, float] = {}          # mint -> first signal time (latency metric)
        self.evaluated = {w: int(self._meta(f"evaluated_{w}") or 0) for w in WINDOWS_MS}

    def _meta(self, k):
        r = self.db.execute("SELECT v FROM s3_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    # --- stream ---------------------------------------------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        recv = ev.get("recv") or self.clock()
        for b in self.books.values():                  # curve state, SL / TP / time / migration exits
            if b.eng or b._try_start():
                b.eng.on_signal_event(ev, None)
        m = ev["mint"]
        if ev["kind"] == "create":
            if len(self.cands) < MAX_CANDS:
                self.cands[m] = {"t0": recv, "creator": ev.get("creator"), "events": [], "done": set(),
                                 "sniper_recv": None}
        elif ev["kind"] == "trade":
            if ev["user"] == self.sniper:              # S3 never sees the sniper: only its buy time is noted
                if ev["is_buy"]:
                    c = self.cands.get(m)
                    if c is not None and c["sniper_recv"] is None:
                        c["sniper_recv"] = recv
                    if m in self.signalled:
                        self.db.execute("UPDATE s3_signals SET bwwk_buy_recv = ? WHERE mint = ? AND "
                                        "bwwk_buy_recv IS NULL AND signal_ts < ?", (recv, m, recv))
                        self.signalled.pop(m, None)
            else:
                c = self.cands.get(m)
                if c is not None and recv <= c["t0"] + WINDOWS_MS[-1] / 1000:
                    c["events"].append((recv, ev["user"], ev["is_buy"], ev["sol"] / LAMPORTS, ev["token"]))
        self._evaluate(recv)

    def _evaluate(self, now: float) -> None:
        for m, c in list(self.cands.items()):
            for w in WINDOWS_MS:
                if w in c["done"] or now < c["t0"] + w / 1000:
                    continue
                c["done"].add(w)
                self.evaluated[w] += 1
                f = features(c["events"], c["creator"], c["t0"] + w / 1000)
                if not fingerprint_ok(f):
                    continue
                if c["creator"] and self.dev_risk(c["creator"]) in BAD_DEV_RISKS:
                    continue
                status = "INVALID_BWWK" if c["sniper_recv"] is not None and c["sniper_recv"] <= now else "SIGNAL"
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO s3_signals (window_ms, mint, creator, create_recv, signal_ts, status, "
                    "buyers, txs, inflow_sol, buy_share, top_share, dev_frac, accel, bwwk_buy_recv) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (w, m, c["creator"], c["t0"], now, status, f["buyers"], f["txs"], f["inflow_sol"],
                     f["buy_share"], f["top_share"], f["dev_frac"], f["accel"],
                     c["sniper_recv"] if status == "INVALID_BWWK" else None))
                if cur.rowcount and status == "SIGNAL":
                    self.pending.append((now + FILL_DELAY_S, w, m, cur.lastrowid, c["creator"]))
                    self.signalled.setdefault(m, now)
            if len(c["done"]) == len(WINDOWS_MS):
                del self.cands[m]

    def tick(self) -> None:
        now = self.clock()
        self._evaluate(now)
        keep = []
        for due, w, m, rid, creator in self.pending:
            if now < due:
                keep.append((due, w, m, rid, creator))
                continue
            b = self.books[w]
            if not (b.eng or b._try_start()):
                keep.append((due, w, m, rid, creator))
                continue
            e = b.eng
            reason = e._gate(m, 1.0)
            curve = e.curves.get(m)
            if not reason and curve is None:
                reason = "no_curve"
            if reason:
                e.counts["skipped"][reason] = e.counts["skipped"].get(reason, 0) + 1
                self.db.execute("UPDATE s3_signals SET status='SKIPPED', skip_reason=? WHERE id=?", (reason, rid))
                continue
            e.counts["kol_buys"] += 1
            p = Pending(m, creator or "s3", now, 0.0, now, now)
            e.pending[m] = p
            e._enter(p, curve, now, "pre_sniper_signal")
            self.db.execute("UPDATE s3_signals SET status='TRADED', fill_ts=? WHERE id=?", (now, rid))
        self.pending = keep
        cut = now - LATENCY_TRACK_S
        self.signalled = {k: v for k, v in self.signalled.items() if v >= cut}
        for m in [m for m, c in self.cands.items() if now - c["t0"] > 10]:
            del self.cands[m]
        self.db.executemany("INSERT OR REPLACE INTO s3_meta VALUES (?, ?)",
                            [(f"evaluated_{w}", v) for w, v in self.evaluated.items()])
        self.db.commit()
        for b in self.books.values():
            b.tick()

    def report(self) -> dict:
        out = {}
        for w in WINDOWS_MS:
            rows = self.db.execute("SELECT status, signal_ts, bwwk_buy_recv FROM s3_signals WHERE window_ms=?",
                                   (w,)).fetchall()
            lat = [b - s for st, s, b in rows if st in ("TRADED", "SKIPPED", "SIGNAL") and b is not None and b > s]
            out[w] = {"evaluated": self.evaluated[w], "signals": sum(1 for r in rows if r[0] != "INVALID_BWWK"),
                      "traded": sum(1 for r in rows if r[0] == "TRADED"),
                      "invalid_bwwk": sum(1 for r in rows if r[0] == "INVALID_BWWK"),
                      "bwwk_after": len(lat),
                      "median_signal_to_bwwk_s": round(statistics.median(lat), 2) if lat else None}
        return out
