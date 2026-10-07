"""PAPER copy engine: KOL buys a pump.fun token -> we "buy" on the bonding curve a few seconds later; the KOL sells
-> we "sell" a few seconds later. Fills are simulated on the real curve reserves carried by every TradeEvent, with
pump.fun fees, extra slippage and transaction fees charged. No wallet, no key, no transaction is ever sent."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from kolbot.config import Config

LAMPORTS = 1_000_000_000


@dataclass
class Curve:
    vsol: int
    vtok: int
    fee_bps: int
    ts: int


@dataclass
class Pending:
    mint: str
    kol: str
    trigger_ts: int
    kol_sol: float
    due_ts: float               # block time
    wall_due: float             # local clock


@dataclass
class Position:
    id: int
    mint: str
    kol: str
    trigger_ts: int
    kol_sol: float
    entry_ts: float
    entry_how: str              # "after_trade" (first trade >= due) | "quiet" (no trade by due + grace)
    spend_sol: float
    tokens: float
    entry_px: float             # SOL per raw token unit, effective (fees included)
    curve: dict = field(default_factory=dict)       # last curve state seen for this token
    sell_due_ts: float | None = None
    sell_wall_due: float | None = None


def buy_tokens(c: Curve, spend_sol: float, cfg: Config) -> float:
    """Raw tokens received for spend_sol (transaction fees, pump.fun fee and extra slippage charged)."""
    budget = spend_sol - cfg.priority_fee_sol - cfg.network_fee_sol
    if budget <= 0:
        return 0.0
    net_in = budget * LAMPORTS / (1 + c.fee_bps / 10_000)
    out = c.vtok - c.vsol * c.vtok / (c.vsol + net_in)
    return out * (1 - cfg.extra_slippage_pct / 100)


def sell_sol(c: Curve, tokens: float, cfg: Config) -> float:
    """SOL received for selling tokens (same charges)."""
    gross = (c.vsol - c.vsol * c.vtok / (c.vtok + tokens)) / LAMPORTS
    return gross * (1 - c.fee_bps / 10_000) * (1 - cfg.extra_slippage_pct / 100) \
        - cfg.priority_fee_sol - cfg.network_fee_sol


class Engine:
    def __init__(self, cfg: Config, kols: set[str], store=None, clock=time.time, log=print):
        self.cfg, self.kols, self.store, self.clock, self.log = cfg, kols, store, clock, log
        self.cash = cfg.starting_sol
        self.curves: dict[str, Curve] = {}
        self.completed: set[str] = set()
        self.pending: dict[str, Pending] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[dict] = []
        self.gaps: list[tuple[float, float]] = []
        self.next_id = 1
        self.day, self.day_loss = "", 0.0
        self.counts = {"kol_buys": 0, "copied": 0, "skipped": {}}
        if store:
            store.restore(self)

    # --- events -------------------------------------------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        if ev["kind"] not in ("trade", "complete"):        # create events: dev history only, not trading
            return
        if ev["kind"] == "complete":
            self._on_complete(ev)
            return
        mint, ts = ev["mint"], ev["ts"]
        c = self.curves.get(mint)
        fee = ev["fee_bps"] if ev.get("fee_bps") else (c.fee_bps if c else self.cfg.default_fee_bps)
        c = self.curves[mint] = Curve(ev["vsol"], ev["vtok"], fee, ts)
        p = self.pending.get(mint)
        if p and ts >= p.due_ts:
            self._enter(p, c, ts, "after_trade")
        pos = self.positions.get(mint)
        if pos:
            pos.curve = asdict(c)
            if pos.sell_due_ts is not None and ts >= pos.sell_due_ts:
                self._exit(pos, c, ts, "kol_sold")
            else:
                self._check_sl_tp(pos, c, ts)
        if ev["user"] in self.kols:
            self._on_kol(ev)

    def _on_kol(self, ev: dict) -> None:
        mint, kol, now = ev["mint"], ev["user"], self.clock()
        if ev["is_buy"] and getattr(self, "stopped", False):     # stopped: no new copies, sells still exit
            return
        if ev["is_buy"]:
            self.counts["kol_buys"] += 1
            sol = ev["sol"] / LAMPORTS
            reason = self._gate(mint, sol)
            if reason:
                self.counts["skipped"][reason] = self.counts["skipped"].get(reason, 0) + 1
                return
            self.pending[mint] = Pending(mint, kol, ev["ts"], sol, ev["ts"] + self.cfg.delay_s, now + self.cfg.delay_s)
            self.log(f"[kol] COPY {kol[:6]} bought {sol:.2f} SOL of {mint[:8]}..")
            return
        pos = self.positions.get(mint)
        if pos and pos.kol == kol and pos.sell_due_ts is None:
            pos.sell_due_ts, pos.sell_wall_due = ev["ts"] + self.cfg.delay_s, now + self.cfg.delay_s
            self._save()
        p = self.pending.get(mint)
        if p and p.kol == kol:                        # KOL already selling before our buy landed: do not buy
            del self.pending[mint]
            self.counts["skipped"]["kol_sold_first"] = self.counts["skipped"].get("kol_sold_first", 0) + 1

    def _gate(self, mint: str, sol: float) -> str | None:
        self._roll_day()
        if sol < self.cfg.min_kol_buy_sol:
            return "small_buy"
        if mint in self.positions or mint in self.pending:
            return "already_in"
        if mint in self.completed:
            return "completed"
        if len(self.positions) + len(self.pending) >= self.cfg.max_open:
            return "max_open"
        if self.cash < self.cfg.position_sol:
            return "no_cash"
        if self.day_loss >= self.cfg.daily_loss_limit_sol:
            return "daily_loss_limit"
        return None

    def _on_complete(self, ev: dict) -> None:
        mint = ev["mint"]
        self.completed.add(mint)
        self.pending.pop(mint, None)
        pos = self.positions.get(mint)
        c = self.curves.get(mint)
        if pos and c:
            self._exit(pos, c, ev["ts"], "migrated")     # last curve price; a real bot would sell on PumpSwap

    # --- wall-clock timers ----------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        for p in list(self.pending.values()):
            if now >= p.wall_due + self.cfg.fill_grace_s:
                c = self.curves.get(p.mint)
                if c is None:
                    del self.pending[p.mint]
                else:
                    self._enter(p, c, now, "quiet")
        for pos in list(self.positions.values()):
            c = self.curves.get(pos.mint) or (Curve(**pos.curve) if pos.curve else None)
            if c is None:
                continue
            if pos.sell_wall_due is not None and now >= pos.sell_wall_due + self.cfg.fill_grace_s:
                self._exit(pos, c, now, "kol_sold")
            elif now - pos.entry_ts >= self.cfg.max_hold_s:
                self._exit(pos, c, now, "max_hold")
        cutoff = now - 2 * 3600                           # forget idle tokens we are not in
        for m in [m for m, c in self.curves.items() if c.ts < cutoff and m not in self.positions
                  and m not in self.pending]:
            del self.curves[m]

    def on_gap(self, start: float, end: float) -> None:
        self.gaps.append((start, end))
        if self.store:
            self.store.gap(start, end)
        self.log(f"[kol] stream gap {end - start:.0f}s")

    # --- fills ----------------------------------------------------------------------------------------------------
    def _enter(self, p: Pending, c: Curve, ts: float, how: str) -> None:
        del self.pending[p.mint]
        spend = self.cfg.position_sol
        tokens = buy_tokens(c, spend, self.cfg)
        if tokens <= 0 or self.cash < spend:
            return
        self.cash -= spend
        pos = Position(self.next_id, p.mint, p.kol, p.trigger_ts, p.kol_sol, ts, how, spend, tokens, spend / tokens,
                       asdict(c))
        self.next_id += 1
        self.positions[p.mint] = pos
        self.counts["copied"] += 1
        self._save()

    def _check_sl_tp(self, pos: Position, c: Curve, ts: float) -> None:
        sl, tp = self.cfg.stop_loss_pct, self.cfg.take_profit_pct
        if sl is None and tp is None:
            return
        pct = 100 * (sell_sol(c, pos.tokens, self.cfg) / pos.spend_sol - 1)
        if sl is not None and pct <= -abs(sl):
            self._exit(pos, c, ts, "stop_loss")
        elif tp is not None and pct >= tp:
            self._exit(pos, c, ts, "take_profit")

    def _exit(self, pos: Position, c: Curve, ts: float, kind: str) -> None:
        self.positions.pop(pos.mint, None)
        proceeds = max(0.0, sell_sol(c, pos.tokens, self.cfg))
        self.cash += proceeds
        pnl = proceeds - pos.spend_sol
        self._roll_day()
        if pnl < 0:
            self.day_loss += -pnl
        gap = any(a < ts and b > pos.entry_ts and b - a > self.cfg.gap_flag_s for a, b in self.gaps)
        row = {"id": pos.id, "mint": pos.mint, "kol": pos.kol, "kol_sol": pos.kol_sol, "trigger_ts": pos.trigger_ts,
               "entry_ts": pos.entry_ts, "entry_how": pos.entry_how, "exit_ts": ts, "exit_kind": kind,
               "spend_sol": pos.spend_sol, "proceeds_sol": proceeds, "pnl_sol": pnl,
               "net_pct": 100 * pnl / pos.spend_sol, "gap": gap}
        self.closed.append(row)
        if self.store:
            self.store.closed(row)
        self._save()
        self.log(f"[kol] EXIT {pos.mint[:8]}.. {kind} {row['net_pct']:+.1f}% ({pnl:+.4f} SOL) cash {self.cash:.3f}")

    def _roll_day(self) -> None:
        d = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if d != self.day:
            self.day, self.day_loss = d, 0.0

    def _save(self) -> None:
        if self.store:
            self.store.save(self)

    def equity(self) -> float:
        """Cash + open positions valued at what a sale would return now."""
        v = self.cash
        for pos in self.positions.values():
            c = self.curves.get(pos.mint) or (Curve(**pos.curve) if pos.curve else None)
            v += max(0.0, sell_sol(c, pos.tokens, self.cfg)) if c else 0.0
        return v
