"""Paper book that follows the display-only buy signals ($500 capital, ~$50 per entry). PAPER ONLY: nothing is
sent anywhere. It reuses the paper engine's fill model unchanged (3 s delay, real curve reserves, pump.fun fees,
1 % extra slippage per side, network + priority fees) through a subclass, in its own database file, so the KOL
paper bot, its ledger and the research data are untouched.

Entry: one paper buy per signal (the signal engine already decided it is a Top 10 source buying >= 0.05 SOL).
Exit: 3 s after the SOURCE wallet sells that token (even if it has left the Top 10 since), else after 24 h; a
migrated token exits at its last curve price. At most 10 open positions; no daily loss limit (none requested)."""
from __future__ import annotations

import time
from pathlib import Path

from kolbot.config import Config
from kolbot.engine import Engine

START_USD = 500.0
TRADE_USD = 50.0
MAX_OPEN = 10


class FollowEngine(Engine):
    """Engine whose entries come from signals and whose exits follow the position's own source wallet."""

    def on_signal_event(self, ev: dict, sig: dict | None) -> None:
        super().on_event(ev)                     # curve state, pending fills, timed exits (kols set is empty)
        if ev["kind"] != "trade":
            return
        if not ev["is_buy"]:
            pos, p = self.positions.get(ev["mint"]), self.pending.get(ev["mint"])
            if (pos and pos.kol == ev["user"]) or (p and p.kol == ev["user"]):
                self._on_kol(ev)                 # the engine's own sell rule: exit 3 s later / cancel a pending buy
        elif sig:
            self._on_kol(ev)                     # the engine's own entry rule (gates, 3 s delay, curve fill)


class SignalPaper:
    def __init__(self, db_path: Path, get_sol_usd, clock=time.time, log=print):
        from kolbot.store import Store
        self.get_sol_usd, self.clock, self.log = get_sol_usd, clock, log
        self.store = Store(Path(db_path))
        self.store.db.execute("CREATE TABLE IF NOT EXISTS book_meta (k TEXT PRIMARY KEY, v REAL)")
        self.store.db.commit()
        self.eng: FollowEngine | None = None
        self._try_start()

    def _meta(self, k):
        r = self.store.db.execute("SELECT v FROM book_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def _try_start(self) -> bool:
        """The book is denominated in USD: it starts once the SOL price is known (then remembered forever)."""
        if self.eng:
            return True
        start_px = self._meta("start_sol_usd")
        px = start_px or self.get_sol_usd()
        if not px:
            return False
        if not start_px:
            self.store.db.executemany("INSERT OR REPLACE INTO book_meta VALUES (?, ?)",
                                      [("start_sol_usd", px), ("start_usd", START_USD), ("trade_usd", TRADE_USD),
                                       ("started_at", self.clock())])
            self.store.db.commit()
        cfg = Config(starting_sol=START_USD / px, position_sol=TRADE_USD / (self.get_sol_usd() or px),
                     max_open=MAX_OPEN, min_kol_buy_sol=0.05, daily_loss_limit_sol=1e12)
        self.eng = FollowEngine(cfg, set(), self.store, clock=self.clock, log=self.log)
        return True

    def on_event(self, ev: dict, sig: dict | None = None) -> None:
        if self.eng or self._try_start():
            self.eng.on_signal_event(ev, sig)

    def tick(self) -> None:
        if not (self.eng or self._try_start()):
            return
        px = self.get_sol_usd()
        if px:
            self.eng.cfg.position_sol = TRADE_USD / px     # ~$50 per entry at today's SOL price
        self.eng.tick()

    def report(self, symbols: dict | None = None) -> dict:
        from kolbot.report import summarize
        px = self.get_sol_usd()
        if not self.eng:
            return {"started": False, "start_usd": START_USD, "trade_usd": TRADE_USD}
        e, symbols = self.eng, symbols or {}
        s = summarize(e.closed)
        start_sol = e.cfg.starting_sol
        eq = e.equity()
        opens = []
        for p in e.positions.values():
            from kolbot.engine import Curve, sell_sol
            c = e.curves.get(p.mint) or (Curve(**p.curve) if p.curve else None)
            v = max(0.0, sell_sol(c, p.tokens, e.cfg)) if c else 0.0
            opens.append({"mint": p.mint, "symbol": symbols.get(p.mint), "source": p.kol, "entry_ts": p.entry_ts,
                          "spend_sol": p.spend_sol, "value_sol": v, "pct": 100 * (v / p.spend_sol - 1),
                          "exiting": p.sell_due_ts is not None})
        closed = [dict(c, symbol=symbols.get(c["mint"])) for c in e.closed[-50:]][::-1]
        usd = (lambda x: x * px) if px else (lambda x: None)
        return {"started": True, "start_usd": self._meta("start_usd"), "start_sol": start_sol,
                "start_sol_usd": self._meta("start_sol_usd"), "started_at": self._meta("started_at"),
                "trade_usd": TRADE_USD, "max_open": MAX_OPEN, "sol_usd": px,
                "cash_sol": e.cash, "equity_sol": eq, "equity_usd": usd(eq),
                "pnl_sol": eq - start_sol, "pnl_usd": usd(eq - start_sol),
                "pnl_pct": 100 * (eq / start_sol - 1) if start_sol else None,
                "realized_sol": s.get("pnl_sol", 0.0), "realized_usd": usd(s.get("pnl_sol", 0.0)),
                "summary": s, "open": opens, "closed": closed, "counts": e.counts}
