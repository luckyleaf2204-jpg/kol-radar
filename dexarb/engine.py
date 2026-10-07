"""Opportunity detection and paper execution.

Detection (one cycle = quote asset -> token on DEX A -> quote asset on DEX B, same chain):
  1. buy quote on A for the exact size; 2. sell quote on B for EXACTLY the token amount A returned; every ordered
  pair (A, B) is a separate cycle, so the reverse direction is tested separately; 3. costs at that size:
  swap / pool fees are inside the quotes (INCLUDED_IN_QUOTE), network cost of two transactions, one-time setup
  (EVM approvals of the input token on each router not yet approved in the paper account; Solana token-account rent
  for a token not yet held), other known costs = 0;
  net_profit = amount_out - amount_in - gas_and_priority - setup - other  (swap fees already in amount_out)
  uncertainty_buffer = BUFFER_BPS of size + BUFFER_COST_SHARE x (gas + setup);
  net_profit_after_buffer = net_profit - buffer. All in the chain's quote asset (no USD conversion).
  Checks in order, the first failing one is the rejection reason: quote_failed / no_route, chain_mismatch,
  token_mismatch, quote_stale, context_mismatch, impact_high, cost_unknown, negative_spread, below_threshold.

Paper model A (sequential wallet swaps, two signatures; the default model). Per arm (L1, L2):
  * at detection + L1: fresh buy and sell quotes; if the fresh net after buffer <= threshold -> ABORTED (no cost);
  * leg 1 executes at that moment: min-out = detection buy quote x (1 - tol); fresh quote below min-out -> REVERTED
    (gas paid, cycle FAILED); else filled at the fresh quote; quote asset and gas debited, token credited;
  * leg 2 decision at leg-1 fill: sell quote then -> min-out; executes at + L2 with a NEW quote for the exact token
    balance (the earlier quotes are never reused). Below min-out -> REVERTED (gas paid), retried every RETRY_S;
    failed quote -> retried; the position is OPEN_EXPOSURE meanwhile. After EXIT_ANY_VENUE_AFTER_S any SUPPORTED
    venue may be used. Never closed by assumption: an unsellable position stays open (marked UNKNOWN value).
Paper model B (atomic): NOT_SUPPORTED for every connector (registry.ATOMIC) - not simulated, not reported as A.
No look-ahead: a decision only uses quotes fetched at or before it; stored rows are never rewritten by later quotes
(fill results are stored in new leg rows)."""
from __future__ import annotations

import hashlib
import json
import random
import time

from dexarb import config as C
from dexarb.fees import Cost
from dexarb.registry import CHAINS, Asset


def raw(amount: float, a: Asset) -> int:
    return int(round(amount * 10 ** a.decimals))


def human(n: int | None, a: Asset) -> float | None:
    return None if n is None else n / 10 ** a.decimals


def evaluate_cycle(chain: str, token: Asset, size: float, buy_q, sell_q, gas_buy: Cost, gas_sell: Cost,
                   setup: Cost, now: float) -> dict:
    ch = CHAINS[chain]
    qa = ch.quote_asset
    r = {"chain": chain, "chain_id": str(ch.chain_id), "token": token.symbol, "size": size,
         "buy_protocol": buy_q.protocol if buy_q else None, "sell_protocol": sell_q.protocol if sell_q else None,
         "detected_at": now, "amount_in": size, "tokens_mid": None, "amount_out": None, "gross_spread": None,
         "gas_cost": None, "setup_cost": None, "other_cost": 0.0, "estimated_costs": None,
         "net_profit_estimate": None, "uncertainty_buffer": None, "net_profit_after_buffer": None,
         "buy_context": getattr(buy_q, "context", None), "sell_context": getattr(sell_q, "context", None),
         "impact_buy": getattr(buy_q, "impact", None), "impact_sell": getattr(sell_q, "impact", None),
         "swap_fees": json.dumps([getattr(buy_q, "fee_note", None), getattr(sell_q, "fee_note", None)]),
         "quote_age_s": None, "cost_status": None, "cost_detail": None}

    def done(status, reason):
        r["status"], r["reason"] = status, reason
        return r
    if buy_q is None or not buy_q.ok:
        return done("REJECTED", "no_route" if buy_q is not None and buy_q.status == "NO_ROUTE" else "quote_failed")
    r["tokens_mid"] = human(buy_q.amount_out, token)
    if sell_q is None or not sell_q.ok:
        return done("REJECTED", "no_route" if sell_q is not None and sell_q.status == "NO_ROUTE" else "quote_failed")
    if buy_q.chain != chain or sell_q.chain != chain:
        return done("REJECTED", "chain_mismatch")
    if (buy_q.token_in, buy_q.token_out, sell_q.token_in, sell_q.token_out) != (qa.symbol, token.symbol,
                                                                                 token.symbol, qa.symbol) \
            or sell_q.amount_in != buy_q.amount_out:
        return done("REJECTED", "token_mismatch")
    r["amount_out"] = human(sell_q.amount_out, qa)
    r["gross_spread"] = r["amount_out"] - size
    r["quote_age_s"] = now - min(buy_q.fetched_at, sell_q.fetched_at)
    if r["quote_age_s"] > C.MAX_QUOTE_AGE_S or abs(buy_q.fetched_at - sell_q.fetched_at) > C.MAX_QUOTE_AGE_S:
        return done("REJECTED", "quote_stale")
    if buy_q.context is not None and sell_q.context is not None and sell_q.context < buy_q.context - 1:
        return done("REJECTED", "context_mismatch")        # sell read on an older chain state (node lag / reorg)
    if max(buy_q.impact or 0, sell_q.impact or 0) > C.MAX_IMPACT:
        return done("REJECTED", "impact_high")
    r["cost_status"] = json.dumps({"gas_buy": gas_buy.status, "gas_sell": gas_sell.status, "setup": setup.status})
    r["cost_detail"] = json.dumps({"gas_buy": gas_buy.source, "gas_sell": gas_sell.source, "setup": setup.source})
    if not (gas_buy.known and gas_sell.known and setup.known):
        return done("REJECTED", "cost_unknown")
    r["gas_cost"] = gas_buy.native + gas_sell.native
    r["setup_cost"] = setup.native
    r["estimated_costs"] = r["gas_cost"] + r["setup_cost"] + r["other_cost"]
    r["net_profit_estimate"] = r["gross_spread"] - r["estimated_costs"]
    r["uncertainty_buffer"] = size * C.BUFFER_BPS / 1e4 + C.BUFFER_COST_SHARE * (r["gas_cost"] + r["setup_cost"])
    r["net_profit_after_buffer"] = r["net_profit_estimate"] - r["uncertainty_buffer"]
    if r["gross_spread"] <= 0:
        return done("REJECTED", "negative_spread")
    if r["net_profit_after_buffer"] <= size * C.MIN_NET_BPS / 1e4:
        return done("REJECTED", "below_threshold")
    return done("CANDIDATE", "")


def opp_uid(r: dict, buy_q, sell_q) -> str:
    return hashlib.sha256(f"{r['chain']}|{r['token']}|{r['size']}|{r['buy_protocol']}|{r['sell_protocol']}|"
                          f"{buy_q.context}|{sell_q.context}|{buy_q.amount_out}|{sell_q.amount_out}".encode()
                          ).hexdigest()[:32]


def keep_raw(r: dict, uid: str) -> bool:
    """Candidates and every evaluation with a positive gross spread are kept raw; all other rejections are counted
    in the rollups and a deterministic 1 % hash sample of them is kept raw for audit."""
    if r["status"] == "CANDIDATE" or (r.get("gross_spread") is not None and r["gross_spread"] > 0):
        return True
    return int(uid[:8], 16) / 0xFFFFFFFF < C.NEG_SAMPLE


class PaperA:
    """Sequential wallet model. quote(chain, proto, a_in, a_out, raw_amount) -> Quote (fresh, read-only);
    costs(chain, proto, a_in, a_out, quote) -> (gas Cost in quote units, gas Cost in native, setup Cost in quote
    units) for that leg given the paper account's approvals / token accounts."""

    def __init__(self, store, quote, costs, arm: str, clock=time.time, rng: random.Random | None = None):
        self.store, self.quote, self.costs, self.arm, self.clock = store, quote, costs, arm, clock
        self.l1, self.l2 = C.ARMS[arm]
        self.account = f"A:{arm}"
        self.rng = rng or random.Random(arm)
        self.events: list[tuple] = []          # (due, seq, kind, cycle_id)
        self.seq = 0
        self.setup_done: set[tuple] = set()
        self.venues: dict[str, list[str]] = {}   # chain -> SUPPORTED protocol keys (set by the app)
        self._pending2: dict[int, tuple] = {}
        db = store.db
        db.execute("INSERT OR IGNORE INTO paper_accounts VALUES (?,?,?,?,?)",
                   (self.account, "sequential_wallet", arm, clock(),
                    json.dumps({"L1": self.l1, "L2": self.l2, "capital": C.CAPITAL, "tol_bps": C.SLIPPAGE_TOL_BPS})))
        for r in db.execute("SELECT kind, detail FROM audit_events WHERE kind='setup_done'"):
            d = json.loads(r[1])
            if d.get("account") == self.account:
                self.setup_done.add(tuple(d["key"]))
        now = clock()
        for cid, st in db.execute("SELECT id, status FROM paper_cycles WHERE account=? AND status IN ('PENDING','LEG1',"
                                  "'OPEN_EXPOSURE')", (self.account,)).fetchall():
            if st == "PENDING":                  # leg 1 time passed while down: never filled by assumption
                self._close(cid, "ABORTED", "restart_before_leg1", now)
            else:                                # holding tokens: a new leg-2 decision with a fresh quote
                self._at(now, "leg2_decide", cid)
        store.commit()

    # --- balances -------------------------------------------------------------------------------------------------
    def bal(self, chain: str, token: str) -> float:
        r = self.store.db.execute("SELECT amount FROM paper_balances WHERE account=? AND chain=? AND token=?",
                                  (self.account, chain, token)).fetchone()
        return r[0] if r else 0.0

    def move(self, chain: str, token: str, delta: float) -> None:
        self.store.db.execute(
            "INSERT INTO paper_balances VALUES (?,?,?,?,?) ON CONFLICT (account, chain, token) DO UPDATE SET "
            "amount = amount + excluded.amount, updated_at = excluded.updated_at",
            (self.account, chain, token, delta, self.clock()))

    def fund(self, chain: str, native_price: float | None) -> None:
        ch = CHAINS[chain]
        if self.store.db.execute("SELECT 1 FROM paper_balances WHERE account=? AND chain=?",
                                 (self.account, chain)).fetchone() or not native_price:
            return
        self.move(chain, ch.quote_asset.symbol, C.CAPITAL)
        self.move(chain, ch.native, C.NATIVE_FLOAT_QUOTE / native_price)
        self.store.audit("fund", {"account": self.account, "chain": chain, "capital": C.CAPITAL,
                                  "native": C.NATIVE_FLOAT_QUOTE / native_price, "native_price": native_price})

    # --- scheduling -----------------------------------------------------------------------------------------------
    def _at(self, due: float, kind: str, cid: int) -> None:
        self.seq += 1
        self.events.append((due, self.seq, kind, cid))

    def open_count(self, chain: str, token: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM paper_cycles WHERE account=? AND chain=? AND status IN ('PENDING','LEG1','OPEN_EXPOSURE')"
        a = [self.account, chain]
        if token:
            q += " AND token=?"
            a.append(token)
        return self.store.db.execute(q, a).fetchone()[0]

    def reserved(self, chain: str) -> float:
        return self.store.db.execute("SELECT IFNULL(SUM(size), 0) FROM paper_cycles WHERE account=? AND chain=? AND "
                                     "status='PENDING'", (self.account, chain)).fetchone()[0]

    def on_candidate(self, opp_id: int, r: dict, baseline: bool = False) -> int | None:
        ch = CHAINS[r["chain"]]
        uid = f"{self.account}|{opp_id}"
        reason = None
        if self.open_count(r["chain"], r["token"]) >= C.MAX_OPEN_PER_TOKEN:
            reason = "max_open_token"
        elif self.open_count(r["chain"]) >= C.MAX_OPEN_PER_CHAIN:
            reason = "max_open_chain"
        elif self.bal(r["chain"], ch.quote_asset.symbol) - self.reserved(r["chain"]) < r["size"]:
            reason = "insufficient_paper_balance"            # pending cycles reserve their size
        cur = self.store.db.execute(
            "INSERT OR IGNORE INTO paper_cycles (uid, account, opportunity_id, chain, token, size, buy_protocol, "
            "sell_protocol, status, reason, started_at, detect_net_after_buffer, baseline) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, self.account, opp_id, r["chain"], r["token"], r["size"], r["buy_protocol"], r["sell_protocol"],
             "SKIPPED" if reason else "PENDING", reason, r["detected_at"], r.get("net_profit_after_buffer"),
             int(baseline)))
        if not cur.rowcount:
            return None                       # duplicate opportunity -> no duplicate cycle
        cid = cur.lastrowid
        if not reason:
            self._at(r["detected_at"] + self.l1, "leg1", cid)
        self.store.commit()
        return cid

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        due = sorted(e for e in self.events if e[0] <= now)
        self.events = [e for e in self.events if e[0] > now]
        for _, _, kind, cid in due:
            if kind == "leg1":
                self._leg1(cid, now)
            elif kind == "leg2_decide":
                self._leg2_decide(cid, now, attempt=2)
            else:
                self._leg2(cid, now)
        self.store.commit()

    # --- legs -----------------------------------------------------------------------------------------------------
    def _cycle(self, cid: int) -> dict:
        cols = ("id", "chain", "token", "size", "buy_protocol", "sell_protocol", "status", "opportunity_id",
                "started_at", "gas_cost", "setup_cost", "spent", "baseline")
        return dict(zip(cols, self.store.db.execute(f"SELECT {','.join(cols)} FROM paper_cycles WHERE id=?",
                                                    (cid,)).fetchone()))

    def _asset(self, chain: str, sym: str) -> Asset:
        ch = CHAINS[chain]
        for a in (ch.quote_asset, *ch.tokens):
            if a.symbol == sym:
                return a
        raise KeyError(sym)

    def _leg_row(self, cid, leg, attempt, proto, a_in, a_out, amt, decision_at, dec_out, min_out, now, q, qid,
                 filled, status, gas_n, gas_q, cst, note=""):
        self.store.db.execute(
            "INSERT INTO paper_legs (cycle_id, leg, attempt, protocol, token_in, token_out, amount_in, decision_at, "
            "decision_quote_out, min_out, exec_at, exec_quote_id, exec_quote_out, filled_out, status, gas_native, "
            "gas_quote, cost_status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, leg, attempt, proto, a_in, a_out, amt, decision_at, dec_out, min_out, now, qid,
             human(q.amount_out, self._asset(q.chain, a_out)) if q is not None and q.ok else None, filled, status,
             gas_n, gas_q, cst, note))

    def _close(self, cid: int, status: str, reason: str, now: float, **kw) -> None:
        sets = {"status": status, "reason": reason, **kw}
        if status in ("CLOSED", "FAILED", "ABORTED"):
            sets["closed_at"] = now
        self.store.db.execute(f"UPDATE paper_cycles SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                              (*sets.values(), cid))

    def _leg1(self, cid: int, now: float) -> None:
        c = self._cycle(cid)
        ch = CHAINS[c["chain"]]
        qa, tok = ch.quote_asset, self._asset(c["chain"], c["token"])
        opp = self.store.db.execute("SELECT amount_in, tokens_mid FROM opportunities WHERE id=?",
                                    (c["opportunity_id"],)).fetchone()
        q_buy = self.quote(c["chain"], c["buy_protocol"], qa, tok, raw(c["size"], qa))
        qid = self.store.add_quote(q_buy, "paper_leg1")
        if not q_buy.ok:
            self._close(cid, "ABORTED", f"leg1_quote_{q_buy.status.lower()}", now)
            return
        q_sell = self.quote(c["chain"], c["sell_protocol"], tok, qa, q_buy.amount_out)
        self.store.add_quote(q_sell, "paper_recheck")
        gas_q, gas_n, setup_q = self.costs(c["chain"], c["buy_protocol"], qa, tok, q_buy, self)
        gas2_q, _, setup2_q = self.costs(c["chain"], c["sell_protocol"], tok, qa, q_sell, self)
        unknown = not (gas_q.known and gas_n.known and setup_q.known and gas2_q.known and setup2_q.known)
        if not c["baseline"]:
            if unknown or not q_sell.ok:
                self._close(cid, "ABORTED", "recheck_cost_unknown" if unknown else "recheck_no_sell_quote", now)
                return
            fresh_net = (human(q_sell.amount_out, qa) - c["size"] - gas_q.native - gas2_q.native - setup_q.native
                         - setup2_q.native)
            buf = c["size"] * C.BUFFER_BPS / 1e4 + C.BUFFER_COST_SHARE * (gas_q.native + gas2_q.native
                                                                        + setup_q.native + setup2_q.native)
            if fresh_net - buf <= c["size"] * C.MIN_NET_BPS / 1e4:
                self._close(cid, "ABORTED", "opportunity_gone_after_latency", now,
                            quote_decay=(fresh_net - buf) - (self._detect_nab(cid) or 0))
                return
        if self.bal(c["chain"], qa.symbol) < c["size"]:
            self._close(cid, "ABORTED", "insufficient_paper_balance", now)
            return
        if not gas_n.known or self.bal(c["chain"], ch.native) < gas_n.native:
            self._close(cid, "ABORTED", "insufficient_gas_balance" if gas_n.known else "gas_unknown", now)
            return
        dec_out = opp[1] if opp else None                  # detection-time buy quote (tokens) -> min-out
        min_out = (dec_out if dec_out is not None else human(q_buy.amount_out, tok)) * (1 - C.SLIPPAGE_TOL_BPS / 1e4)
        got = human(q_buy.amount_out, tok)
        setup_native = (setup_q.native or 0.0)
        self.move(c["chain"], ch.native, -gas_n.native)
        if got < min_out:                       # reverted on chain: gas paid, nothing swapped
            self._leg_row(cid, 1, 1, c["buy_protocol"], qa.symbol, tok.symbol, c["size"], c["started_at"], dec_out,
                          min_out, now, q_buy, qid, None, "REVERTED", gas_n.native, gas_q.native, gas_q.status,
                          "fresh quote below min-out")
            self._close(cid, "FAILED", "leg1_reverted_slippage", now, gas_cost=gas_q.native, spent=0.0, net=-gas_q.native,
                        unknown_costs=int(unknown))
            return
        self.move(c["chain"], qa.symbol, -c["size"])
        self.move(c["chain"], tok.symbol, got)
        self._mark_setup(c["chain"], c["buy_protocol"], qa)
        self._leg_row(cid, 1, 1, c["buy_protocol"], qa.symbol, tok.symbol, c["size"], c["started_at"], dec_out, min_out,
                      now, q_buy, qid, got, "FILLED", gas_n.native, gas_q.native, gas_q.status)
        self.store.db.execute("INSERT INTO paper_positions (account, chain, token, amount, cycle_id, status, opened_at) "
                              "VALUES (?,?,?,?,?,'OPEN',?)", (self.account, c["chain"], tok.symbol, got, cid, now))
        self._close(cid, "LEG1", "", now, spent=c["size"], gas_cost=gas_q.native,
                    setup_cost=setup_native, unknown_costs=int(unknown))
        self._leg2_decide(cid, now, attempt=1)

    def _detect_nab(self, cid):
        r = self.store.db.execute("SELECT detect_net_after_buffer FROM paper_cycles WHERE id=?", (cid,)).fetchone()
        return r[0] if r else None

    def _mark_setup(self, chain, proto, a_in: Asset) -> None:
        key = (chain, proto, a_in.symbol)
        if key not in self.setup_done:
            self.setup_done.add(key)
            self.store.audit("setup_done", {"account": self.account, "key": list(key)})

    def _leg2_decide(self, cid: int, now: float, attempt: int) -> None:
        c = self._cycle(cid)
        ch = CHAINS[c["chain"]]
        tok = self._asset(c["chain"], c["token"])
        amt = self._position(cid)
        q = self._best_sell(c, tok, ch.quote_asset, amt, attempt, now)
        self.store.add_quote(q, "paper_leg2_decision")
        dec = human(q.amount_out, ch.quote_asset) if q.ok else None
        self.store.db.execute("INSERT INTO audit_events (at, kind, detail) VALUES (?,?,?)",
                              (now, "leg2_decision", json.dumps({"cycle": cid, "attempt": attempt, "quote_out": dec})))
        self._pending2[cid] = (attempt, now, dec, q.protocol)
        self._at(now + (self.l2 if attempt == 1 else C.RETRY_S), "leg2", cid)

    def _best_sell(self, c: dict, tok: Asset, qa: Asset, amt: float, attempt: int, now: float):
        """The planned venue; after EXIT_ANY_VENUE_AFTER_S the best quote among the chain's SUPPORTED venues."""
        venues = [c["sell_protocol"]]
        if attempt > 1 and now - c["started_at"] >= C.EXIT_ANY_VENUE_AFTER_S:
            venues = self.venues.get(c["chain"]) or venues
        best = None
        for v in venues:
            q = self.quote(c["chain"], v, tok, qa, raw(amt, tok))
            if best is None or (q.ok and (not best.ok or q.amount_out > best.amount_out)):
                best = q
        return best

    def _position(self, cid: int) -> float:
        r = self.store.db.execute("SELECT amount FROM paper_positions WHERE cycle_id=? AND status='OPEN'",
                                  (cid,)).fetchone()
        return r[0] if r else 0.0

    def _leg2(self, cid: int, now: float) -> None:
        c = self._cycle(cid)
        ch = CHAINS[c["chain"]]
        qa, tok = ch.quote_asset, self._asset(c["chain"], c["token"])
        attempt, dec_at, dec_out, venue = self._pending2.pop(cid, (1, now, None, c["sell_protocol"]))
        amt = self._position(cid)
        if amt <= 0:
            return
        q = self.quote(c["chain"], venue, tok, qa, raw(amt, tok))
        qid = self.store.add_quote(q, "paper_leg2")
        if not q.ok:
            self._leg_row(cid, 2, attempt, venue, tok.symbol, qa.symbol, amt, dec_at, dec_out, None, now,
                          q, qid, None, "QUOTE_FAILED", 0.0, 0.0, "", q.error[:100])
            self._close(cid, "OPEN_EXPOSURE", f"leg2_{q.status.lower()}", now)
            self._leg2_decide(cid, now, attempt + 1)
            return
        gas_q, gas_n, setup_q = self.costs(c["chain"], venue, tok, qa, q, self)
        if not gas_n.known or self.bal(c["chain"], ch.native) < (gas_n.native or 0):
            self._leg_row(cid, 2, attempt, venue, tok.symbol, qa.symbol, amt, dec_at, dec_out, None, now,
                          q, qid, None, "NO_GAS", 0.0, 0.0, gas_n.status)
            self._close(cid, "OPEN_EXPOSURE", "leg2_gas_unknown_or_insufficient", now)
            self._leg2_decide(cid, now, attempt + 1)
            return
        out = human(q.amount_out, qa)
        min_out = dec_out * (1 - C.SLIPPAGE_TOL_BPS / 1e4) if dec_out is not None else None
        self.move(c["chain"], ch.native, -gas_n.native)
        gas_total = (c["gas_cost"] or 0) + gas_q.native
        if min_out is None or out < min_out:
            self._leg_row(cid, 2, attempt, venue, tok.symbol, qa.symbol, amt, dec_at, dec_out, min_out, now,
                          q, qid, None, "REVERTED", gas_n.native, gas_q.native, gas_q.status, "below min-out")
            self._close(cid, "OPEN_EXPOSURE", "leg2_reverted_slippage", now, gas_cost=gas_total)
            self._leg2_decide(cid, now, attempt + 1)
            return
        self.move(c["chain"], tok.symbol, -amt)
        self.move(c["chain"], qa.symbol, out)
        self._mark_setup(c["chain"], venue, tok)
        self._leg_row(cid, 2, attempt, venue, tok.symbol, qa.symbol, amt, dec_at, dec_out, min_out, now, q,
                      qid, out, "FILLED", gas_n.native, gas_q.native, gas_q.status)
        self.store.db.execute("UPDATE paper_positions SET status='CLOSED', closed_at=? WHERE cycle_id=? AND status='OPEN'",
                              (now, cid))
        setup_total = (c["setup_cost"] or 0) + (setup_q.native or 0)
        net = out - c["size"] - gas_total - setup_total
        self._close(cid, "CLOSED", "", now, received=out, gas_cost=gas_total, setup_cost=setup_total, net=net)

    # --- baseline -------------------------------------------------------------------------------------------------
    def pick_baseline(self, chain: str, protos: list[str]) -> tuple | None:
        """Random (token, ordered pair, size) for the non-signal benchmark cycle (seeded per arm)."""
        ch = CHAINS[chain]
        if len(protos) < 2:
            return None
        a, b = self.rng.sample(sorted(protos), 2)
        sizes = C.SCAN_SIZES_SOLANA if chain == "solana" else C.SIZES
        return self.rng.choice(ch.tokens), a, b, self.rng.choice(sizes)
