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
Exit: TP +50 %, SL -30 % (net sale value), 5 min max, migration = last curve price. Costs = the shared model.

Data integrity (prereg amendment 4):
  * Stream gaps (reported by the listener on reconnect, and process downtime found from the heartbeat at start) are
    stored. Any window whose span [creation receive, signal / fill time] overlaps a gap is GAP_INVALID: stored, never
    traded, never counted. There is no backfill of the pump.fun stream, so these stay invalid (a backfill would have
    to re-confirm every event of the window before a row could be revalidated).
  * No fill while the stream is silent > STREAM_STALE_S: the fill waits; if a gap is then reported it is invalidated.
  * Every gap is also given to the S3 book engines (gap_flag_s = 0 for S3): a trade whose holding period overlaps any
    gap is flagged and excluded from the results.
  * Pending fills are persisted (s3_pending). After a restart a pending fill is restored only if its fill time is
    still in the future and no gap overlaps it; otherwise INVALID_RESTART. A SIGNAL row with no persisted pending
    fill (written before this amendment) also becomes INVALID_RESTART."""
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
STREAM_STALE_S = 5.0              # no fill when no event has been received for this long (stream maybe down)
HEARTBEAT_GAP_S = 5.0             # heartbeat older than this at start = downtime gap
INVALID = ("INVALID_BWWK", "GAP_INVALID", "INVALID_RESTART")

SCHEMA = """
CREATE TABLE IF NOT EXISTS s3_signals (id INTEGER PRIMARY KEY, window_ms INTEGER, mint TEXT, creator TEXT,
    create_recv REAL, signal_ts REAL, status TEXT, buyers INTEGER, txs INTEGER, inflow_sol REAL, buy_share REAL,
    top_share REAL, dev_frac REAL, accel REAL, fill_ts REAL, skip_reason TEXT, bwwk_buy_recv REAL,
    UNIQUE (window_ms, mint));
CREATE TABLE IF NOT EXISTS s3_meta (k TEXT PRIMARY KEY, v REAL);
CREATE TABLE IF NOT EXISTS s3_pending (row_id INTEGER PRIMARY KEY, due REAL, window_ms INTEGER, mint TEXT,
    creator TEXT, signal_ts REAL);
CREATE TABLE IF NOT EXISTS s3_gaps (start REAL, end REAL, reason TEXT);
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
        self.last_recv: float | None = None
        self.gaps: list[tuple] = [(a, b) for a, b in self.db.execute("SELECT start, end FROM s3_gaps")]
        now = clock()
        hb = self._meta("heartbeat")
        if hb is not None and now - hb > HEARTBEAT_GAP_S:   # the process was down: nothing was observed
            self.on_gap(hb, now, reason="downtime")
        self._restore(now)

    def _meta(self, k):
        r = self.db.execute("SELECT v FROM s3_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    # --- gaps / restart -------------------------------------------------------------------------------------------
    def _overlaps(self, a: float, b: float) -> bool:
        return any(g0 <= b and g1 >= a for g0, g1 in self.gaps)

    def on_gap(self, start: float, end: float, reason: str = "stream") -> None:
        """The stream (or the process) was down: invalidate every window / pending fill that overlaps it."""
        if self.last_recv is not None and reason == "stream":
            start = min(start, self.last_recv)               # data was lost from the last event received
        self.gaps.append((start, end))
        self.db.execute("INSERT INTO s3_gaps VALUES (?,?,?)", (start, end, reason))
        for m, c in list(self.cands.items()):                # windows still open: evaluated on incomplete data
            if c["t0"] <= end and c["t0"] + WINDOWS_MS[-1] / 1000 >= start:
                for w in WINDOWS_MS:
                    if w not in c["done"]:
                        self.evaluated[w] += 1
                        self.db.execute("INSERT OR IGNORE INTO s3_signals (window_ms, mint, creator, create_recv, "
                                        "signal_ts, status, skip_reason) VALUES (?,?,?,?,?,'GAP_INVALID',?)",
                                        (w, m, c["creator"], c["t0"], end, f"{reason}_gap"))
                del self.cands[m]
        keep = []
        for item in self.pending:                            # signals not filled yet whose span overlaps the gap
            due, w, m, rid, creator = item
            sig = self.db.execute("SELECT create_recv FROM s3_signals WHERE id=?", (rid,)).fetchone()
            if sig and sig[0] <= end and due >= start:
                self._invalidate(rid, "GAP_INVALID", f"{reason}_gap")
            else:
                keep.append(item)
        self.pending = keep
        for rid, st, w, m, c0, fill in self.db.execute(
                "SELECT id, status, window_ms, mint, create_recv, fill_ts FROM s3_signals WHERE status IN "
                "('SIGNAL','TRADED') AND create_recv <= ? AND COALESCE(fill_ts, signal_ts) >= ?", (end, start)
        ).fetchall():
            self.db.execute("UPDATE s3_signals SET status = CASE status WHEN 'TRADED' THEN 'TRADED_GAP' ELSE "
                            "'GAP_INVALID' END, skip_reason=? WHERE id=?", (f"{reason}_gap", rid))
            b = self.books.get(w)
            if st == "TRADED" and fill is not None and b is not None and b.eng:
                # the data before the entry was incomplete: flag the trade itself (open -> flagged at exit)
                b.eng.on_gap(c0, fill + 1e-3)
                for c in b.eng.closed:
                    if c["mint"] == m and abs(c["trigger_ts"] - fill) < 1e-6:
                        c["gap"] = True
                        b.store.db.execute("UPDATE trades SET gap=1 WHERE mint=? AND trigger_ts=?", (m, c["trigger_ts"]))
                b.store.db.commit()
        for b in self.books.values():                        # trades holding through the gap are flagged
            if b.eng or b._try_start():
                b.eng.on_gap(start, end)
        self.db.commit()

    def _invalidate(self, rid: int, status: str, reason: str) -> None:
        self.db.execute("UPDATE s3_signals SET status=?, skip_reason=? WHERE id=?", (status, reason, rid))
        self.db.execute("DELETE FROM s3_pending WHERE row_id=?", (rid,))

    def _restore(self, now: float) -> None:
        """Pending fills survive a restart only if they can still be executed as registered."""
        for rid, due, w, m, creator, sig_ts in self.db.execute(
                "SELECT row_id, due, window_ms, mint, creator, signal_ts FROM s3_pending").fetchall():
            if due > now and not self._overlaps(sig_ts, due):
                self.pending.append((due, w, m, rid, creator))
                self.signalled.setdefault(m, sig_ts)
            else:
                self._invalidate(rid, "INVALID_RESTART", "pending_fill_not_restorable")
        live = {p[3] for p in self.pending}
        for (rid,) in self.db.execute("SELECT id FROM s3_signals WHERE status='SIGNAL'").fetchall():
            if rid not in live:
                self._invalidate(rid, "INVALID_RESTART", "no_persisted_pending_fill")
        self.db.commit()

    # --- stream ---------------------------------------------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        recv = ev.get("recv") or self.clock()
        self.last_recv = recv
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
                    self.db.execute("INSERT OR REPLACE INTO s3_pending VALUES (?,?,?,?,?,?)",
                                    (cur.lastrowid, now + FILL_DELAY_S, w, m, c["creator"], now))
                    self.signalled.setdefault(m, now)
            if len(c["done"]) == len(WINDOWS_MS):
                del self.cands[m]

    def tick(self) -> None:
        now = self.clock()
        self._evaluate(now)
        keep = []
        for due, w, m, rid, creator in self.pending:
            if now < due or self.last_recv is None or now - self.last_recv > STREAM_STALE_S:
                keep.append((due, w, m, rid, creator))          # not due, or stream silent: wait (gap may follow)
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
                self.db.execute("DELETE FROM s3_pending WHERE row_id=?", (rid,))
                continue
            e.counts["kol_buys"] += 1
            p = Pending(m, creator or "s3", now, 0.0, now, now)
            e.pending[m] = p
            e._enter(p, curve, now, "pre_sniper_signal")
            self.db.execute("UPDATE s3_signals SET status='TRADED', fill_ts=? WHERE id=?", (now, rid))
            self.db.execute("DELETE FROM s3_pending WHERE row_id=?", (rid,))
        self.pending = keep
        cut = now - LATENCY_TRACK_S
        self.signalled = {k: v for k, v in self.signalled.items() if v >= cut}
        for m in [m for m, c in self.cands.items() if now - c["t0"] > 10]:
            del self.cands[m]
        self.db.executemany("INSERT OR REPLACE INTO s3_meta VALUES (?, ?)",
                            [(f"evaluated_{w}", v) for w, v in self.evaluated.items()] + [("heartbeat", now)])
        self.db.commit()
        for b in self.books.values():
            b.tick()

    def report(self) -> dict:
        out = {}
        for w in WINDOWS_MS:
            rows = self.db.execute("SELECT status, signal_ts, bwwk_buy_recv FROM s3_signals WHERE window_ms=?",
                                   (w,)).fetchall()
            lat = [b - s for st, s, b in rows if st in ("TRADED", "SKIPPED", "SIGNAL") and b is not None and b > s]
            out[w] = {"evaluated": self.evaluated[w],
                      "signals": sum(1 for r in rows if r[0] not in INVALID + ("TRADED_GAP",)),
                      "gap_invalid": sum(1 for r in rows if r[0] in ("GAP_INVALID", "TRADED_GAP")),
                      "invalid_restart": sum(1 for r in rows if r[0] == "INVALID_RESTART"),
                      "traded": sum(1 for r in rows if r[0] == "TRADED"),
                      "invalid_bwwk": sum(1 for r in rows if r[0] == "INVALID_BWWK"),
                      "bwwk_after": len(lat),
                      "median_signal_to_bwwk_s": round(statistics.median(lat), 2) if lat else None}
        return out
