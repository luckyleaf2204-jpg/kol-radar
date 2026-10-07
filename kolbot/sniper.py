"""Sniper research (PAPER ONLY, pre-registered in docs/prereg_sniper_books.md, 2026-10-07).

S1 launch sniper: buy every NEW token seen on the stream ~3 s after its creation (our real latency: we are not
   a slot-0 bot), unless it shows a bad sign that is observable at that moment:
     - the creator holds > 10 % of the supply from its own buys,
     - >= 3 different non-creator wallets bought in the creation second (bundle / creator-funded sniping),
     - the creator's profile risk is HIGH RISK, REPEAT FAILURE or SUSPICIOUS / RUG HISTORY.
   Exit: take profit +50 %, stop loss -30 % (net sale value), 5 min max, migrated = last curve price.
S2 sniper copy (amended 2026-10-07: only the #1 of this list): the Top 10 non-KOL wallets with >= 100 resolved round trips, average hold < 60 s, CI of the mean
   ROI > 0 and not trading their own tokens; a buy >= 0.05 SOL -> paper buy 3 s later; exit 3 s after the
   wallet's first sell (24 h max).
Both reuse the paper engine's fill model unchanged (curve reserves, pump.fun fees, 1 % slippage, network + priority
fees), ~$50 per entry, at most 10 open, own database files. No real order exists anywhere in this code."""
from __future__ import annotations

import time

from kolbot.engine import LAMPORTS, Pending

DEV_MAX_SUPPLY_FRAC = 0.10
BUNDLE_MIN_WALLETS = 3
BAD_DEV_RISKS = ("HIGH RISK", "REPEAT FAILURE", "SUSPICIOUS / RUG HISTORY")
LAUNCH_DELAY_S = 3.0
RAW_SUPPLY = 1e15
CAND_TTL_S = 30
S1_CFG = {"take_profit_pct": 50.0, "stop_loss_pct": 30.0, "max_hold_s": 300.0}
S2_MIN_N, S2_MAX_HOLD_S, S2_TOP = 100, 60.0, 10


class LaunchSniper:
    """Entry logic of S1 on top of a SignalPaper book (its FollowEngine does fills, SL / TP / time exits)."""

    def __init__(self, book, dev_risk, clock=time.time):
        self.book, self.dev_risk, self.clock = book, dev_risk, clock
        self.cands: dict[str, dict] = {}

    def on_event(self, ev: dict) -> None:
        b = self.book
        if not (b.eng or b._try_start()):
            return
        e = b.eng
        e.on_signal_event(ev, None)                     # curve state, fills, SL / TP / time exits
        if ev["kind"] == "create":
            if len(self.cands) < 5000:
                self.cands[ev["mint"]] = {"ts": ev["ts"], "creator": ev.get("creator"), "dev_tok": 0,
                                          "early": set(), "wall_due": self.clock() + LAUNCH_DELAY_S}
            return
        if ev["kind"] != "trade":
            return
        c = self.cands.get(ev["mint"])
        if not c:
            return
        if ev["user"] == c["creator"]:
            c["dev_tok"] += ev["token"] if ev["is_buy"] else -ev["token"]
        elif ev["is_buy"] and ev["ts"] <= c["ts"]:
            c["early"].add(ev["user"])
        if ev["ts"] >= c["ts"] + LAUNCH_DELAY_S:
            self._decide(ev["mint"], ev["ts"], "after_trade")

    def tick(self) -> None:
        b = self.book
        if not b.eng:
            return
        now = self.clock()
        for m, c in list(self.cands.items()):
            if now >= c["wall_due"] + b.eng.cfg.fill_grace_s:
                if m in b.eng.curves:
                    self._decide(m, now, "quiet")
                else:
                    self.cands.pop(m, None)
            elif now - c["wall_due"] > CAND_TTL_S:
                self.cands.pop(m, None)

    def _skip(self, why: str) -> None:
        sk = self.book.eng.counts["skipped"]
        sk[why] = sk.get(why, 0) + 1

    def _decide(self, mint: str, ts: float, how: str) -> None:
        c = self.cands.pop(mint, None)
        e = self.book.eng
        if not c:
            return
        e.counts["kol_buys"] += 1                       # = launches considered
        if c["dev_tok"] > DEV_MAX_SUPPLY_FRAC * RAW_SUPPLY:
            return self._skip("dev_holds_gt_10pct")
        if len(c["early"]) >= BUNDLE_MIN_WALLETS:
            return self._skip("bundled_launch")
        if c["creator"] and self.dev_risk(c["creator"]) in BAD_DEV_RISKS:
            return self._skip("bad_dev_history")
        reason = e._gate(mint, 1.0)
        if reason:
            return self._skip(reason)
        curve = e.curves.get(mint)
        if curve is None:
            return self._skip("no_curve")
        p = Pending(mint, c["creator"] or "launch", c["ts"], 0.0, ts, ts)
        e.pending[mint] = p
        e._enter(p, curve, ts, how)                     # the engine's own fill on the current curve state


class SniperSources:
    """S2 sources: fast, profitable non-KOL wallets (see module doc), refreshed every minute."""

    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        self.sources: dict[str, dict] = {}
        self.last = 0.0

    def refresh(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last < 60:
            return
        self.last = now
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='sw_wallets'").fetchone():
            return
        rows = self.db.execute(
            "SELECT wallet, n, wins, hold_sum / n, ci_lo FROM sw_wallets WHERE n >= ? AND hold_sum / n < ? "
            "AND ci_lo > 0 AND self_trades * 1.0 / n < 0.3 AND wallet NOT IN (SELECT wallet FROM kol_roster) "
            "ORDER BY wins * 1.0 / n DESC, n DESC, wins DESC, wallet LIMIT ?", (S2_MIN_N, S2_MAX_HOLD_S, S2_TOP))
        self.sources = {w: {"source": "sniper", "rank": i, "n": n, "wins": wi, "avg_hold_s": h, "ci_lo": ci}
                        for i, (w, n, wi, h, ci) in enumerate(rows, 1)}

    def source_of(self, ev: dict) -> dict | None:
        if ev["kind"] != "trade" or not ev["is_buy"] or ev["sol"] < 0.05 * LAMPORTS:
            return None
        return self.sources.get(ev["user"])


PINNED_WALLET = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"   # S2 / S2b pinned (user, 2026-10-07, amendment 3)


def pinned_source_of(ev: dict, wallet: str = PINNED_WALLET) -> dict | None:
    """S2 / S2b: a buy >= 0.05 SOL by the pinned wallet, whatever the ranking says."""
    if ev["kind"] != "trade" or not ev["is_buy"] or ev["sol"] < 0.05 * LAMPORTS or ev["user"] != wallet:
        return None
    return {"source": "sniper", "rank": 1, "pinned": True}
