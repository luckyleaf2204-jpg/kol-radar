"""Gate B3 / B4: wallet history and PnL, per cohort (research only, read-only chain data, paper only).

Cohorts (B4): DEPTH (81) and STANK (39) are the 120 base wallets, read first (priority 0). INSIDERS (the channel's
52 holders + 29 successors) is a cohort of its own (priority 1), never merged into DEPTH / STANK. The old PnL of the
95 call coins is kept as a reference only (source=legacy_pnl_95coins); no label is ever derived from it. No wallet is
left out for a balance under 1 SOL.

Per wallet: every signature is paged back (1000 a call, WALLET_MAX_PAGES at most: past it the coverage is "partial"
and an explicit gap is recorded), each transaction is read once and decoded with watch.analyse (pool-side price),
and the PnL is computed per wallet x mint with an average cost basis:
  BUY      cost += SOL paid, tokens held += tokens
  SELL     realised += SOL received - average cost x tokens sold
  SEND / RECEIVE   tokens move with no SOL: never a sale or a buy. A send takes its share of the cost basis with it;
           received tokens have no known cost, so a pair that sells them is INSUFFICIENT_DATA (no guessed cost)
  fees     the network fee of the wallet's own transactions, recorded apart (never mixed into the trade SOL)
  unrealised  only with a current price from a named source; without one INSUFFICIENT_DATA
  2x-10x multiples need a price history: INSUFFICIENT_DATA in Gate B (no interpolation)
A wallet whose history is partial has every pair INSUFFICIENT_DATA (an earlier buy may be missing).

Budget: a token bucket of its own (WALLET_CALLS_PER_MIN, default 15) apart from the coin tracker, plus a daily and a
monthly cap on the calls metered in rpc_usage (worker "wallets"): reached = safe stop until the next day / month.
The worker is OFF unless INSIDER_WALLET_HISTORY=1, so nothing here spends a credit without that switch."""
from __future__ import annotations

import json
import os
import threading
import time

from insiders.track import Budget, _env_f
from insiders.watch import analyse

WALLET_CALLS_PER_MIN = _env_f("INSIDER_WALLET_CALLS_PER_MIN", 15)
WALLET_DAY_CAP = int(_env_f("INSIDER_WALLET_DAY_CAP", 20_000))
WALLET_MONTH_CAP = int(_env_f("INSIDER_WALLET_MONTH_CAP", 400_000))
WALLET_MAX_PAGES = int(_env_f("INSIDER_WALLET_MAX_PAGES", 10))
PRIORITY = {"DEPTH": 0, "STANK": 0, "INSIDERS": 1}
LEGACY_SOURCE = "legacy_pnl_95coins"
INSUFFICIENT = "INSUFFICIENT_DATA"
WORKER = "wallets"


def enabled_by_env() -> bool:
    return (os.environ.get("INSIDER_WALLET_HISTORY") or "0") == "1"


def cohorts(result: dict) -> list[dict]:
    """Every wallet of the lists with its cohort (no balance filter). A wallet in two lists keeps the first cohort by
    priority; the other sources are kept in `source`."""
    v = result.get("vip") or {}
    out: dict[str, dict] = {}

    def put(wallet, cohort, source, label, skip=False):
        x = out.get(wallet)
        if x is None:
            out[wallet] = {"wallet": wallet, "cohort": cohort, "source": [source], "label": label, "skip": skip}
            return
        x["source"].append(source)
        if PRIORITY.get(cohort, 9) < PRIORITY.get(x["cohort"], 9):
            x.update(cohort=cohort, label=label, skip=skip)
    for e in v.get("external", []):
        g = (e.get("group") or "").upper()
        if g in ("DEPTH", "STANK"):
            put(e["wallet"], g, f"external:{e.get('role') or ''}".rstrip(":"), e.get("role"))
    for h in v.get("holders", []):
        put(h["wallet"], "INSIDERS", "holder", "holder")
    for s in v.get("successors", []):
        kind = s.get("kind") or "?"
        # a deposit / exchange address is listed in the cohort but its history (an exchange's) is not read
        put(s["wallet"], "INSIDERS", f"successor:{kind}", f"successor ({kind})", skip=kind != "person")
    return list(out.values())


def legacy(result: dict) -> dict[str, dict]:
    """The old 95-coin PnL per wallet, as a reference (gross, no fees, held tokens not valued)."""
    p = result.get("pnl") or {}
    return {w["wallet"]: {"source": LEGACY_SOURCE, "net_sol": w.get("net_sol"), "spent_sol": w.get("spent_sol"),
                          "received_sol": w.get("received_sol"), "coins": w.get("coins"), "wins": w.get("wins"),
                          "note": "tham khảo: 95 coin call, gộp, chưa trừ phí, token còn giữ không định giá"}
            for w in p.get("wallets") or [] if w.get("wallet")}


class Capped(Exception):
    """The daily or monthly call cap is reached: safe stop."""


class NoBudget(Exception):
    pass


def pair_pnl(trades: list[dict], price=None) -> dict:
    """PnL of one wallet x mint from its decoded trades (oldest first). price: (SOL per raw token unit, source)."""
    r = {"buys": 0, "sells": 0, "sends": 0, "receives": 0, "sol_in": 0.0, "sol_out": 0.0, "fees_sol": 0.0,
         "fees_outside_sol": 0.0,
         "tok_bought": 0, "tok_sold": 0, "tok_sent": 0, "tok_received": 0, "cost_sold_sol": 0.0, "realized_sol": 0.0}
    costed, free, cost = 0, 0, 0.0                    # tokens with a known cost, tokens received (no cost), SOL basis
    unknown = []
    for t in sorted(trades, key=lambda t: (t.get("ts") or 0, t.get("sig") or "")):
        q, sol = int(t.get("tokens") or 0), float(t.get("sol") or 0)
        r["fees_sol"] += float(t.get("fee_sol") or 0)
        k = t["kind"]
        if not t.get("fee_in_sol"):
            r["fees_outside_sol"] += float(t.get("fee_sol") or 0)
        if k == "CREATE" and q == 0:
            continue                                  # the launch itself: no token moved
        if k in ("BUY", "CREATE") and (k == "BUY" or sol > 0):
            r["buys"] += 1
            r["sol_in"] += sol
            r["tok_bought"] += q
            costed += q
            cost += sol
        elif k in ("RECEIVE", "CREATE"):
            r["receives"] += 1
            r["tok_received"] += q
            free += q
        elif k == "SELL":
            r["sells"] += 1
            r["sol_out"] += sol
            r["tok_sold"] += q
            from_costed = min(q, costed)
            if free:                                   # which tokens were sold (bought or received) is not knowable
                unknown.append("bán khi đang giữ token nhận chuyển (không rõ giá vốn)")
            elif q > costed:
                unknown.append("bán nhiều hơn lượng đã mua thấy được")
            if q > costed:
                free = max(0, free - (q - from_costed))
            part = cost * from_costed / costed if costed else 0.0
            r["cost_sold_sol"] += part
            r["realized_sol"] += (sol * from_costed / q if q else 0.0) - part
            cost -= part
            costed -= from_costed
        elif k == "SEND":
            r["sends"] += 1
            r["tok_sent"] += q
            held = costed + free
            take_c = round(q * costed / held) if held else 0
            take_c = min(take_c, costed)
            cost -= cost * take_c / costed if costed else 0.0
            costed -= take_c
            free = max(0, free - (q - take_c))
    left = costed + free
    r["tok_left"] = left
    r["avg_cost_sol"] = (cost / costed) if costed else None
    r["price_sol"], r["price_src"], r["unrealized_sol"] = None, None, None
    reasons = list(dict.fromkeys(unknown))
    if left > 0:
        if price and price[0] is not None:
            r["price_sol"], r["price_src"] = price[0], price[1]
            if free:
                reasons.append("token nhận chuyển còn giữ: không rõ giá vốn")
            else:
                r["unrealized_sol"] = costed * price[0] - cost
        else:
            reasons.append("chưa có giá hiện tại")
    r["status"] = INSUFFICIENT if reasons else "OK"
    r["reason"] = "; ".join(reasons) or None
    # fees: when the trade SOL was read off the wallet's own balance the network fee is already inside it
    # (fee_in_sol); the other fees are taken off here, once
    r["realized_net_sol"] = r["realized_sol"] - r["fees_outside_sol"]
    for k in ("sol_in", "sol_out", "fees_sol", "fees_outside_sol", "cost_sold_sol", "realized_sol", "realized_net_sol"):
        r[k] = round(r[k], 9)
    return r


class WalletHistory:
    def __init__(self, store, rpc, now=time.time, budget: Budget | None = None, enabled: bool | None = None,
                 paused=lambda: False, price=None, day_cap: int | None = None, month_cap: int | None = None):
        self.s, self.rpc, self.now, self.paused = store, rpc, now, paused
        self.budget = budget or Budget(WALLET_CALLS_PER_MIN)
        self.enabled = enabled_by_env() if enabled is None else enabled
        self.price = price or (lambda mint: None)            # no price source by default: unrealised = INSUFFICIENT
        self.day_cap = WALLET_DAY_CAP if day_cap is None else day_cap
        self.month_cap = WALLET_MONTH_CAP if month_cap is None else month_cap
        self.stat = {"ticks": 0, "calls": 0, "errors": 0, "last_error": None, "safe_stop": None}

    # ------------------------------------------------------------ cohorts (local, no RPC)
    def seed(self, result: dict) -> int:
        """Adds the wallets of the lists once; an existing row (cohort, queue state, checkpoint) is never changed."""
        leg, n = legacy(result), 0
        for c in cohorts(result):
            pr = PRIORITY.get(c["cohort"], 9)
            n += self.s.x("INSERT OR IGNORE INTO wallets (wallet, cohort, source, label, priority, added, legacy) "
                          "VALUES (?,?,?,?,?,?,?)", (c["wallet"], c["cohort"], ",".join(c["source"]), c["label"], pr,
                                                     self.now(), json.dumps(leg[c["wallet"]]) if c["wallet"] in leg else None))
            self.s.x("INSERT OR IGNORE INTO wallet_queue (wallet, priority, state, updated) VALUES (?,?,?,?)",
                     (c["wallet"], pr, "skipped" if c["skip"] else "todo", self.now()))
        return n

    # ------------------------------------------------------------ calls
    def _used(self, prefix: str) -> int:
        return self.s.q("SELECT COALESCE(SUM(calls),0) n FROM rpc_usage WHERE worker=? AND day LIKE ?",
                        (WORKER, prefix + "%"))[0]["n"]

    def _call(self, method: str, params: list):
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        if self._used(day) >= self.day_cap:
            self.stat["safe_stop"] = f"đạt trần ngày {self.day_cap} lượt"
            raise Capped()
        if self._used(day[:7]) >= self.month_cap:
            self.stat["safe_stop"] = f"đạt trần tháng {self.month_cap} lượt"
            raise Capped()
        if not self.budget.take():
            raise NoBudget()
        self.stat["safe_stop"] = None
        self.stat["calls"] += 1
        try:
            res = self.rpc(method, params)
        except Exception:
            self.s.usage(WORKER, method, None, err=True)
            raise
        self.s.usage(WORKER, method, res)
        return res

    # ------------------------------------------------------------ steps
    def _next(self, states: tuple) -> dict | None:
        marks = ",".join("?" * len(states))
        r = self.s.q(f"SELECT * FROM wallet_queue WHERE state IN ({marks}) ORDER BY priority, wallet LIMIT 1", states)
        return r[0] if r else None

    def step_sigs(self) -> int:
        """One page of signatures of the first wallet still paging (DEPTH / STANK first)."""
        q = self._next(("todo", "paging"))
        if not q:
            return 0
        opts = {"limit": 1000}
        if q["before_sig"]:
            opts["before"] = q["before_sig"]
        sigs = self._call("getSignaturesForAddress", [q["wallet"], opts]) or []
        for x in sigs:
            self.s.x("INSERT OR IGNORE INTO wallet_sigs (wallet, sig, block_time, err) VALUES (?,?,?,?)",
                     (q["wallet"], x["signature"], x.get("blockTime"), 0 if x.get("err") is None else 1))
        upd = {"pages": (q["pages"] or 0) + 1, "sigs": (q["sigs"] or 0) + len(sigs), "updated": self.now()}
        if sigs:
            upd["before_sig"] = sigs[-1]["signature"]
            upd["oldest_ts"] = sigs[-1].get("blockTime")
            if not q["newest_sig"]:
                upd["newest_sig"], upd["newest_ts"] = sigs[0]["signature"], sigs[0].get("blockTime")
        if len(sigs) < 1000:
            upd.update(state="parsing", coverage="full")
        elif upd["pages"] >= WALLET_MAX_PAGES:
            upd.update(state="parsing", coverage=f"partial ({upd['pages']} trang x 1000)")
            self.s.open_gap("wallet", q["wallet"], upd["before_sig"], None,
                            f"lịch sử ví: dừng ở trần {WALLET_MAX_PAGES} trang", note="cũ hơn chưa đọc")
        else:
            upd["state"] = "paging"
        self._set(q["wallet"], **upd)
        return len(sigs)

    def step_parse(self, limit: int = 5) -> int:
        q = self._next(("parsing",))
        if not q:
            return 0
        # parsed: 0 to read, -k = k failed tries, 1 read, 2 given up (an open gap)
        rows = self.s.q("SELECT sig, parsed FROM wallet_sigs WHERE wallet=? AND parsed<=0 AND err=0 "
                        "ORDER BY parsed DESC, block_time LIMIT ?", (q["wallet"], limit))
        if not rows:
            missing = self.s.q("SELECT COUNT(*) n FROM wallet_sigs WHERE wallet=? AND parsed=2", (q["wallet"],))[0]["n"]
            full = (q["coverage"] or "") == "full"
            # "done" only with every signature read and no gap; otherwise "incomplete" (never shown as finished)
            self._set(q["wallet"], state="done" if full and not missing else "incomplete", updated=self.now())
            self.compute(q["wallet"])
            return 0
        n = 0
        for r in rows:
            why = None
            try:
                tx = self._call("getTransaction", [r["sig"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                              "maxSupportedTransactionVersion": 1}])
                if not tx:
                    why = "getTransaction không trả kết quả"
                else:
                    self.ingest(q["wallet"], tx)
            except (NoBudget, Capped):
                raise
            except Exception as e:
                from insiders.vip import redact
                why = redact(f"{type(e).__name__}: {str(e)[:80]}")
            if why is None:
                self.s.x("UPDATE wallet_sigs SET parsed=1 WHERE wallet=? AND sig=?", (q["wallet"], r["sig"]))
                n += 1
                continue
            tries = 1 - (r["parsed"] or 0)
            if tries >= 5:
                self.s.x("UPDATE wallet_sigs SET parsed=2 WHERE wallet=? AND sig=?", (q["wallet"], r["sig"]))
                self.s.open_gap("wallet-tx", q["wallet"], r["sig"], None, f"{why} (sau 5 lượt)")
            else:
                self.s.x("UPDATE wallet_sigs SET parsed=? WHERE wallet=? AND sig=?", (-tries, q["wallet"], r["sig"]))
            break                                     # one failed try per tick: an RPC outage does not burn the tries
        self._set(q["wallet"], parsed=(q["parsed"] or 0) + n, updated=self.now())
        return n

    def ingest(self, wallet: str, tx: dict) -> int:
        """The wallet's trades in one transaction; the network fee only when the wallet paid it (first signer)."""
        evs = [e for e in analyse(tx, wallet) if e["kind"] in ("BUY", "SELL", "SEND", "RECEIVE", "CREATE")]
        meta = tx.get("meta") or {}
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
        fee = (int(meta.get("fee") or 0) / 1e9) if keys[:1] == [wallet] else 0.0
        native = None
        if wallet in keys:
            i = keys.index(wallet)
            try:
                native = (int(meta["postBalances"][i]) - int(meta["preBalances"][i])) / 1e9
            except (KeyError, IndexError, TypeError, ValueError):
                native = None
        n = 0
        for i, e in enumerate(evs):
            f = fee if i == 0 else 0.0                # one fee per transaction, on its first trade
            sol = float(e.get("sol") or 0)
            # the trade SOL equals the wallet's own balance change (analyse rounds to 0.001): the fee is inside it
            inside = bool(f) and native is not None and sol > 0 and abs(sol - abs(native)) <= 0.0006
            n += self.s.x("INSERT OR IGNORE INTO wallet_trades (wallet, sig, mint, kind, tokens, sol, fee_sol, ts, fee_in_sol) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (wallet, e["sig"], e["mint"], e["kind"], str(e.get("tokens") or 0),
                                                         sol, f, e.get("ts"), 1 if inside else 0))
        return n

    def retry_missing(self, wallet: str) -> int:
        """Given-up transactions of a wallet are read again (after an RPC outage, say); their gaps are closed."""
        n = self.s.x("UPDATE wallet_sigs SET parsed=0 WHERE wallet=? AND parsed=2", (wallet,))
        if n:
            self.s.x("UPDATE gaps SET closed=?, note='đọc lại' WHERE scope='wallet-tx' AND address=? AND closed IS NULL",
                     (self.now(), wallet))
            self._set(wallet, state="parsing", updated=self.now())
        return n

    def compute(self, wallet: str) -> list[dict]:
        q = (self.s.q("SELECT * FROM wallet_queue WHERE wallet=?", (wallet,)) or [{}])[0]
        partial = (q.get("coverage") or "").startswith("partial")
        missing = self.s.q("SELECT COUNT(*) n FROM wallet_sigs WHERE wallet=? AND parsed=2", (wallet,))[0]["n"]
        by: dict[str, list] = {}
        for t in self.s.q("SELECT * FROM wallet_trades WHERE wallet=? ORDER BY ts", (wallet,)):
            by.setdefault(t["mint"], []).append(t)
        out = []
        for mint, ts in by.items():
            r = pair_pnl(ts, self.price(mint))
            extra = []
            if partial:
                extra.append("lịch sử ví chưa đầy đủ (quá trần trang)")
            if missing:
                extra.append(f"{missing} giao dịch chưa đọc được")
            if extra:
                r["status"] = INSUFFICIENT
                r["reason"] = "; ".join(([r["reason"]] if r["reason"] else []) + extra)
            r.update(wallet=wallet, mint=mint, first_ts=ts[0]["ts"], last_ts=ts[-1]["ts"], updated=self.now())
            cols = ",".join(r)
            self.s.x(f"INSERT OR REPLACE INTO wallet_pnl ({cols}) VALUES ({','.join('?' * len(r))})",
                     tuple(str(v) if k.startswith("tok_") else v for k, v in r.items()))
            out.append(r)
        return out

    def summary(self, wallet: str) -> dict | None:
        w = self.s.q("SELECT * FROM wallets WHERE wallet=?", (wallet,))
        if not w:
            return None
        w = w[0]
        q = (self.s.q("SELECT * FROM wallet_queue WHERE wallet=?", (wallet,)) or [{}])[0]
        pairs = self.s.q("SELECT * FROM wallet_pnl WHERE wallet=? ORDER BY last_ts DESC", (wallet,))
        ok = [p for p in pairs if p["status"] == "OK"]
        sold = [p for p in ok if p["sells"]]
        wins = sum(1 for p in sold if p["realized_sol"] > 0)
        losses = sum(1 for p in sold if p["realized_sol"] < 0)
        cost = sum(p["cost_sold_sol"] or 0 for p in sold)
        realized = sum(p["realized_sol"] or 0 for p in ok)
        done = q.get("state") == "done"               # "incomplete" (partial history / unread transactions): no totals
        return {"wallet": wallet, "cohort": w["cohort"], "source": w["source"], "label": w["label"],
                "state": q.get("state"), "coverage": q.get("coverage"), "sigs": q.get("sigs"), "parsed": q.get("parsed"),
                "pairs": len(pairs), "pairs_ok": len(ok), "pairs_insufficient": len(pairs) - len(ok),
                "realized_sol": round(realized, 6) if done and ok else INSUFFICIENT,
                "realized_net_sol": round(sum(p["realized_net_sol"] or 0 for p in ok), 6) if done and ok else INSUFFICIENT,
                "gaps_open": len(self.s.gaps(address=wallet)),
                "fees_sol": round(sum(p["fees_sol"] or 0 for p in pairs), 6),
                "roi": round(realized / cost, 4) if done and cost > 0 else INSUFFICIENT,
                "win_rate": round(wins / (wins + losses), 4) if done and wins + losses else INSUFFICIENT,
                "wins": wins, "losses": losses, "multiples_2x_10x": INSUFFICIENT,
                "legacy": json.loads(w["legacy"]) if w["legacy"] else None,
                "verified": False}                            # Gate B never labels a wallet as verified smart money

    def cohort_counts(self) -> dict:
        rows = self.s.q("SELECT w.cohort, q.state, COUNT(*) n FROM wallets w JOIN wallet_queue q ON q.wallet=w.wallet "
                        "GROUP BY w.cohort, q.state")
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r["cohort"], {})[r["state"]] = r["n"]
        return out

    def _set(self, wallet: str, **kw) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        self.s.x(f"UPDATE wallet_queue SET {cols} WHERE wallet=?", (*kw.values(), wallet))

    # ------------------------------------------------------------ loop
    def tick(self) -> None:
        self.stat["ticks"] += 1
        if not self.enabled or self.paused():
            return
        for step in (self.step_sigs, self.step_parse):
            try:
                step()
            except (NoBudget, Capped):
                return
            except Exception as e:
                from insiders.vip import redact
                self.stat["errors"] += 1
                self.stat["last_error"] = redact(f"{type(e).__name__}: {str(e)[:120]}")

    def run_forever(self, stop: threading.Event, every_s: float = 4.0) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(every_s)

    def status(self) -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        return {"enabled": self.enabled, "paused": bool(self.paused()), "per_min": self.budget.per_min,
                "day_cap": self.day_cap, "month_cap": self.month_cap, "calls_today": self._used(day),
                "calls_month": self._used(day[:7]), "cohorts": self.cohort_counts(), **self.stat}
