"""Paper books that follow the display-only buy signals (~$50 per entry, $500 reference capital). PAPER ONLY:
nothing is sent anywhere. The paper engine's fill model is reused unchanged (3 s delay, real curve reserves,
pump.fun fees, 1 % extra slippage per side, network + priority fees) through a subclass, each book in its own
database file, so the KOL paper bot, its ledger and the research data are untouched.

Entry rules (book.entry):
  signal      one paper buy per displayed Top 10 signal (the original book)
  topn        a buy >= 0.05 SOL by a source ranked <= top_n (both lists, or one list), 30 min (wallet, token) dedup
  confluence  H2: >= 2 DIFFERENT Top 10 sources bought the token within 10 min -> one paper buy
Exit rules (book.exit):
  first_sell  3 s after the source wallet's first sell of the token (the original rule)
  half_sold   H1 / H2: 3 s after the source wallet(s) together sold >= 50 % of the tokens they bought since the
              entry trigger (a pending buy is cancelled if that happens before it fills)
  always also: 24 h max hold; a migrated token exits at its last curve price.
Accounting (user decision 2026-10-06): every entry is ~$50 whatever the balance. When cash is short the book is
topped up and the top-up is recorded, so a test never stops for lack of paper money; results are judged per trade
(mean net %, CI), and "balance if it had only $500" + max drawdown are shown beside them. At most 10 open."""
from __future__ import annotations

import json
import time
from pathlib import Path

from kolbot.config import Config
from kolbot.engine import Engine

START_USD = 500.0
TRADE_USD = 50.0
MAX_OPEN = 10
CONFLUENCE_S = 600
HALF = 0.5


class FollowEngine(Engine):
    """Engine whose entries come from signals and whose exits follow the source wallet(s)."""

    exit_mode = "first_sell"

    def __init__(self, *a, **k):
        self.topped_up = 0.0
        self.track: dict[str, dict[str, list]] = {}      # half_sold: mint -> {wallet: [bought, sold]}
        super().__init__(*a, **k)

    # --- funding: ~$50 per entry whatever the balance (top-ups recorded) ------------------------------------------
    def _fund(self) -> None:
        need = self.cfg.position_sol - self.cash
        if need > 0:
            self.cash += need
            self.topped_up += need

    def _gate(self, mint, sol):
        self._fund()
        return super()._gate(mint, sol)

    def _enter(self, p, c, ts, how):
        self._fund()
        super()._enter(p, c, ts, how)

    # --- events --------------------------------------------------------------------------------------------------
    def on_signal_event(self, ev: dict, sig: dict | None, init_track: dict | None = None) -> None:
        super().on_event(ev)                     # curve state, pending fills, timed exits (kols set is empty)
        if ev["kind"] != "trade":
            return
        mint, user = ev["mint"], ev["user"]
        if self.exit_mode == "first_sell":
            if not ev["is_buy"]:
                pos, p = self.positions.get(mint), self.pending.get(mint)
                if (pos and pos.kol == user) or (p and p.kol == user):
                    self._on_kol(ev)             # the engine's own sell rule: exit 3 s later / cancel a pending buy
            elif sig:
                self._on_kol(ev)                 # the engine's own entry rule (gates, 3 s delay, curve fill)
            return
        t = self.track.get(mint)                 # half_sold
        if t is not None and user in t and (mint in self.positions or mint in self.pending):
            t[user][0 if ev["is_buy"] else 1] += ev["token"]
            bought, sold = sum(v[0] for v in t.values()), sum(v[1] for v in t.values())
            if not ev["is_buy"] and bought > 0 and sold >= HALF * bought:
                pos = self.positions.get(mint)
                if pos and pos.sell_due_ts is None:
                    now = self.clock()
                    pos.sell_due_ts, pos.sell_wall_due = ev["ts"] + self.cfg.delay_s, now + self.cfg.delay_s
                    self._save()
                elif mint in self.pending:
                    del self.pending[mint]
                    self.counts["skipped"]["source_sold_half_first"] = \
                        self.counts["skipped"].get("source_sold_half_first", 0) + 1
        elif ev["is_buy"] and sig:
            self._on_kol(ev)
            if mint in self.pending and self.pending[mint].kol == user:
                self.track[mint] = {w: [v[0], v[1]] for w, v in (init_track or {user: [ev["token"], 0]}).items()}

    def tick(self) -> None:
        super().tick()
        if self.track:
            for m in [m for m in self.track if m not in self.positions and m not in self.pending]:
                del self.track[m]


class SignalPaper:
    def __init__(self, db_path: Path, get_sol_usd, clock=time.time, log=print, top_n: int | None = None,
                 source: str | None = None, label: str | None = None, entry: str | None = None,
                 exit: str = "first_sell"):
        # entry: "signal" (top_n None), "topn" (top_n set) or "confluence"; source None = both lists
        self.entry = entry or ("signal" if top_n is None else "topn")
        self.exit, self.top_n, self.source = exit, top_n, source
        self.label = label or f"Top {top_n or 10}"
        self.last_trigger: dict = {}
        self.recent: dict[str, dict[str, tuple]] = {}     # confluence: mint -> {wallet: (ts, tokens)}
        from kolbot.store import Store
        self.get_sol_usd, self.clock, self.log = get_sol_usd, clock, log
        self.store = Store(Path(db_path))
        self.store.db.execute("CREATE TABLE IF NOT EXISTS book_meta (k TEXT PRIMARY KEY, v REAL)")
        self.store.db.execute("CREATE TABLE IF NOT EXISTS book_kv (k TEXT PRIMARY KEY, v TEXT)")
        self.store.db.commit()
        self.eng: FollowEngine | None = None
        self._kv_dirty = False
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
        eng = FollowEngine(cfg, set(), self.store, clock=self.clock, log=self.log)
        eng.exit_mode = self.exit
        eng.topped_up = self._meta("topped_up_sol") or 0.0
        r = self.store.db.execute("SELECT v FROM book_kv WHERE k='track'").fetchone()
        eng.track = json.loads(r[0]) if r else {}
        self.eng = eng
        return True

    def on_event(self, ev: dict, sig: dict | None = None, src: dict | None = None) -> None:
        if not (self.eng or self._try_start()):
            return
        init = None
        if self.entry == "topn":                          # its own trigger from the source ranking
            sig = None
            if src and src["rank"] <= self.top_n and (self.source is None or src["source"] == self.source):
                sig = self._dedup(ev, src)
        elif self.entry == "confluence":                  # H2: >= 2 different Top 10 sources within 10 min
            sig = None
            if src and src["rank"] <= 10 and self._dedup(ev, src):
                m = ev["mint"]
                rec = {w: v for w, v in self.recent.get(m, {}).items() if ev["ts"] - v[0] <= CONFLUENCE_S}
                rec[ev["user"]] = (ev["ts"], rec.get(ev["user"], (0, 0))[1] + ev["token"])
                self.recent[m] = rec
                if len(rec) >= 2 and m not in self.eng.positions and m not in self.eng.pending:
                    sig = {"rank": src["rank"], "source": src["source"], "confluence": len(rec)}
                    init = {w: [v[1], 0] for w, v in rec.items()}
                if len(self.recent) > 50_000:
                    self.recent = {k: v for k, v in self.recent.items()
                                   if any(ev["ts"] - x[0] <= CONFLUENCE_S for x in v.values())}
        before = len(self.eng.track)
        self.eng.on_signal_event(ev, sig, init)
        if self.exit == "half_sold" and (sig or len(self.eng.track) != before or ev["mint"] in self.eng.track):
            self._kv_dirty = True

    def _dedup(self, ev: dict, src: dict) -> dict | None:
        from kolbot.signals import DEDUP_S
        k = (ev["user"], ev["mint"])
        if ev["ts"] - self.last_trigger.get(k, -1e18) < DEDUP_S:
            return None
        self.last_trigger[k] = ev["ts"]
        if len(self.last_trigger) > 50_000:
            cut = ev["ts"] - DEDUP_S
            self.last_trigger = {a: b for a, b in self.last_trigger.items() if b >= cut}
        return {"rank": src["rank"], "source": src["source"]}

    def tick(self) -> None:
        if not (self.eng or self._try_start()):
            return
        px = self.get_sol_usd()
        if px:
            self.eng.cfg.position_sol = TRADE_USD / px     # ~$50 per entry at today's SOL price
        self.eng.tick()
        self.store.db.execute("INSERT OR REPLACE INTO book_meta VALUES ('topped_up_sol', ?)", (self.eng.topped_up,))
        if self._kv_dirty:
            self.store.db.execute("INSERT OR REPLACE INTO book_kv VALUES ('track', ?)", (json.dumps(self.eng.track),))
            self._kv_dirty = False
        self.store.db.commit()

    def report(self, symbols: dict | None = None) -> dict:
        from kolbot.report import summarize
        px = self.get_sol_usd()
        if not self.eng:
            return {"started": False, "start_usd": START_USD, "trade_usd": TRADE_USD, "label": self.label}
        e, symbols = self.eng, symbols or {}
        s = summarize(e.closed)
        start_sol = e.cfg.starting_sol
        eq = e.equity()
        sim = eq - e.topped_up                            # balance if the book had only its $500
        cum = peak = dd = 0.0                             # max drawdown of realized P&L, from the $500
        for c in e.closed:
            if c.get("gap"):
                continue
            cum += c["pnl_sol"]
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        opens = []
        from kolbot.engine import Curve, sell_sol
        for p in e.positions.values():
            c = e.curves.get(p.mint) or (Curve(**p.curve) if p.curve else None)
            v = max(0.0, sell_sol(c, p.tokens, e.cfg)) if c else 0.0
            opens.append({"mint": p.mint, "symbol": symbols.get(p.mint), "source": p.kol, "entry_ts": p.entry_ts,
                          "spend_sol": p.spend_sol, "value_sol": v, "pct": 100 * (v / p.spend_sol - 1),
                          "exiting": p.sell_due_ts is not None})
        closed = [dict(c, symbol=symbols.get(c["mint"])) for c in e.closed[-50:]][::-1]
        usd = (lambda x: x * px) if px else (lambda x: None)
        mean_pnl = s.get("pnl_sol", 0.0) / s["n"] if s.get("n") else None
        return {"started": True, "top_n": self.top_n or 10, "source": self.source, "label": self.label,
                "entry": self.entry, "exit": self.exit, "start_usd": self._meta("start_usd"), "start_sol": start_sol,
                "start_sol_usd": self._meta("start_sol_usd"), "started_at": self._meta("started_at"),
                "trade_usd": TRADE_USD, "max_open": MAX_OPEN, "sol_usd": px,
                "cash_sol": e.cash, "equity_sol": eq, "equity_usd": usd(eq),
                "topped_up_sol": e.topped_up, "topped_up_usd": usd(e.topped_up),
                "sim_balance_sol": sim, "sim_balance_usd": usd(sim), "busted": sim <= 0,
                "pnl_sol": sim - start_sol, "pnl_usd": usd(sim - start_sol),
                "pnl_pct": 100 * (sim / start_sol - 1) if start_sol else None,
                "max_drawdown_sol": dd, "max_drawdown_usd": usd(dd),
                "expectancy_sol": mean_pnl, "expectancy_usd": usd(mean_pnl) if mean_pnl is not None else None,
                "realized_sol": s.get("pnl_sol", 0.0), "realized_usd": usd(s.get("pnl_sol", 0.0)),
                "summary": s, "open": opens, "closed": closed, "counts": e.counts}
