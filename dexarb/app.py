"""Runtime: one scanner per chain (thread), paper arms, health, storage metrics, retention, API data. PAPER ONLY."""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from pathlib import Path

from dexarb import config as C
from dexarb import engine as E
from dexarb.fees import Cost, EvmFees, SolanaFees, to_quote
from dexarb.protocols import V3_TIERS, Quote, evm_quotes, jupiter_quote
from dexarb.registry import ATOMIC, CHAINS, coverage, supported, verification
from dexarb.rpc import Rpc
from dexarb.store import Store

SOL_REQ_SPACING_S = 1.05                # Jupiter public endpoint: about one request per second
METRICS_EVERY_S = 3600
RETENTION_EVERY_S = 86400


def db_path() -> tuple[Path, bool, str]:
    """dexarb.db next to the existing persistent database (KOL_DB on the Render disk) unless DEXARB_DB is set.
    Returns (path, durable, why)."""
    if os.environ.get("DEXARB_DB"):
        p = Path(os.environ["DEXARB_DB"])
    elif os.environ.get("KOL_DB"):
        p = Path(os.environ["KOL_DB"]).with_name("dexarb.db")
    else:
        p = Path(__file__).resolve().parents[1] / "data" / "dexarb.db"
    if os.environ.get("RENDER"):
        ok = p.as_posix().startswith("/var/data/")
        return p, ok, "on the Render persistent disk" if ok else "NOT on the persistent disk: paper sample disabled"
    return p, True, "local disk"


class Lab:
    def __init__(self, store: Store, chains: list[str] | None = None, rpc_factory=None, jup_get=None,
                 clock=time.time, log=print, paper_enabled: bool = True, ver: dict | None = None,
                 sleep=time.sleep):
        self.store, self.clock, self.log, self.sleep = store, clock, log, sleep
        self.ver = verification() if ver is None else ver
        self.lock = threading.RLock()
        self.chains = [c for c in (chains or list(CHAINS)) if c in CHAINS]
        rpc_factory = rpc_factory or (lambda c: Rpc(CHAINS[c].rpc_url()))
        self.rpc = {c: rpc_factory(c) for c in self.chains}
        self.jup_get = jup_get
        self.fees = {c: (EvmFees(c, self.rpc[c], clock) if CHAINS[c].kind == "evm" else SolanaFees(self.rpc[c], clock))
                     for c in self.chains}
        self.price: dict[str, Cost] = {}
        self.price_at: dict[str, float] = {}
        self._gas_price: dict[str, Cost] = {}
        self.last_ok: dict[str, float] = {}
        self.stats = {c: {"scans": 0, "evaluated": 0, "candidates": 0, "rejected": {}, "last_scan": None,
                          "last_error": ""} for c in self.chains}
        self.paper_enabled = paper_enabled
        self.arms = {a: E.PaperA(store, self.quote_one, self.leg_costs, a, clock) for a in C.ARMS} \
            if paper_enabled else {}
        for arm in self.arms.values():
            arm.venues = {c: [p.key for p in supported(c, self.ver)] for c in self.chains}
        self.last_baseline: dict[tuple, float] = {}
        self.last_metrics = self.last_retention = -1e18
        self._seed_registry()

    # --- registry rows ----------------------------------------------------------------------------------------------
    def _seed_registry(self) -> None:
        db = self.store.db
        for ch in CHAINS.values():
            db.execute("INSERT OR REPLACE INTO chains VALUES (?,?,?,?,?,?)",
                       (ch.key, str(ch.chain_id), ch.kind, ch.native, ch.quote_asset.symbol, json.dumps(ch.wallets)))
            for a, role in [(ch.quote_asset, "quote"), *[(t, "token") for t in ch.tokens]]:
                db.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?)",
                           (ch.key, a.symbol, a.address, a.decimals, role, "KNOWN_STANDARD (pre-registered blue chip)"))
        for r in coverage(self.ver):
            db.execute("INSERT OR REPLACE INTO dex_protocols VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (r["chain"], r["protocol"], r["name"],
                        next(p.kind for p in CHAINS[r["chain"]].protocols if p.key == r["protocol"]),
                        r["version"], r["address"], r["pool_types"], r["quote_source"], r["status"], r["atomic"]))
        db.execute("INSERT OR IGNORE INTO experiment_config VALUES (?,?,?)",
                   (C.VERSION, self.clock(), json.dumps({k: getattr(C, k) for k in dir(C) if k.isupper()})))
        db.execute("INSERT INTO experiment_runs (version, started_at, host, note) VALUES (?,?,?,?)",
                   (C.VERSION, self.clock(), os.environ.get("RENDER_SERVICE_NAME") or "local",
                    "paper enabled" if self.paper_enabled else "paper DISABLED"))
        self.store.commit()

    # --- quoting --------------------------------------------------------------------------------------------------
    def _proto(self, chain: str, key: str):
        return next(p for p in CHAINS[chain].protocols if p.key == key)

    def quote_many(self, chain: str, reqs: list[tuple]) -> list[Quote]:
        """reqs: (protocol key, asset in, asset out, raw amount)."""
        if CHAINS[chain].kind == "evm":
            qs = evm_quotes(self.rpc[chain], chain, [self._proto(chain, r[0]) for r in reqs], reqs, self.clock)
        else:
            qs = []
            for i, (pk, a_in, a_out, amt) in enumerate(reqs):
                if i:
                    self.sleep(SOL_REQ_SPACING_S)
                kw = {"get": self.jup_get} if self.jup_get else {}
                qs.append(jupiter_quote(self._proto(chain, pk), a_in, a_out, amt, clock=self.clock, **kw))
        with self.lock:
            for q in qs:
                self.store.health(chain, q.protocol, q.status in ("OK", "NO_ROUTE"), q.error, q.latency_ms)
        return qs

    def quote_one(self, chain, pk, a_in, a_out, amt) -> Quote:
        return self.quote_many(chain, [(pk, a_in, a_out, amt)])[0]

    def native_price(self, chain: str, protos: list) -> Cost:
        """Quote-asset units per native coin, quoted on a SUPPORTED DEX (wrapped native -> quote asset, 1 unit)."""
        ch = CHAINS[chain]
        if chain in self.price and self.clock() - self.price_at[chain] <= C.MAX_QUOTE_AGE_S * 6:
            return self.price[chain]
        qs = self.quote_many(chain, [(p.key, ch.wrapped, ch.quote_asset, E.raw(1.0, ch.wrapped)) for p in protos[:2]])
        ok = [q for q in qs if q.ok]
        if not ok:
            return Cost(None, "UNKNOWN", "native price quote failed")
        best = max(ok, key=lambda q: q.amount_out)
        px = E.human(best.amount_out, ch.quote_asset)
        self.price[chain], self.price_at[chain] = Cost(px, "MEASURED", f"1 {ch.wrapped.symbol} -> {px:.6g} "
                                                                       f"{ch.quote_asset.symbol} on {best.protocol} "
                                                                       f"(context {best.context})"), self.clock()
        with self.lock:
            self.store.db.execute("INSERT INTO oracle_prices (chain, base, quote, price, source, context, at) VALUES "
                                  "(?,?,?,?,?,?,?)", (chain, ch.native, ch.quote_asset.symbol, px, best.protocol,
                                                      best.context, self.clock()))
        return self.price[chain]

    # --- costs ----------------------------------------------------------------------------------------------------
    def leg_costs(self, chain: str, pk: str, a_in, a_out, q: Quote, account) -> tuple[Cost, Cost, Cost]:
        """(gas in quote units, gas in native, one-time setup in quote units) for one swap leg of `account`."""
        ch = CHAINS[chain]
        price = self.price.get(chain) or Cost(None, "UNKNOWN", "no native price yet")
        f = self.fees[chain]
        if ch.kind == "evm":
            gp = self._gas_price.get(chain) or f.gas_price()
            fee = (q.route[0].get("fee_tier") if q is not None and q.route else None)
            gas_n = f.swap_cost(self._proto(chain, pk), a_in.address, a_out.address, fee, gp)
            setup_n = Cost(0.0, "MEASURED", "allowance already set in the paper account")
            key = (chain, pk, a_in.symbol)
            if account is not None and key not in account.setup_done:
                setup_n = f.approval_cost(a_in.address, self._proto(chain, pk).address, gp)
        else:
            gas_n = f.swap_cost()
            setup_n = Cost(0.0, "MEASURED", "token account exists in the paper account")
            key = (chain, "ata", a_out.symbol)
            if a_out.symbol not in (ch.quote_asset.symbol, ch.wrapped.symbol) and account is not None \
                    and (chain, pk, a_in.symbol) not in account.setup_done and key not in account.setup_done:
                setup_n = f.ata_rent()
        return to_quote(gas_n, price), gas_n, to_quote(setup_n, price)

    # --- scan -----------------------------------------------------------------------------------------------------
    def scan(self, chain: str) -> dict:
        """Network I/O happens outside the lock (chains scan in parallel); DB writes take the lock."""
        return self._scan(chain)

    def warm_costs(self, chain: str, protos: list) -> None:
        """Fill the pool / gas-sample / approval caches BEFORE quoting, so costs never delay a decision."""
        ch = CHAINS[chain]
        if ch.kind != "evm":
            self.fees[chain].ata_rent()
            return
        gp = self._gas_price.get(chain)
        f = self.fees[chain]
        for p in protos:
            tiers = [None] if p.kind == "v2_router" else list(V3_TIERS.get(p.key, (500,)))
            for tok in ch.tokens:
                for a_in, a_out in ((ch.quote_asset, tok), (tok, ch.quote_asset)):
                    for fee in tiers:
                        pool = f.pool_of(p, a_in.address, a_out.address, fee)
                        if pool:
                            f.swap_gas_units(p, pool)
                    if gp is not None:
                        f.approval_cost(a_in.address, p.address, gp)

    def _scan(self, chain: str) -> dict:
        ch, now = CHAINS[chain], self.clock()
        st = self.stats[chain]
        protos = supported(chain, self.ver)
        if len(protos) < 2:
            st["last_error"] = f"{len(protos)} SUPPORTED venue(s): no DEX-to-DEX cycle possible"
            return st
        try:
            self.native_price(chain, protos)
            if ch.kind == "evm":
                self._gas_price[chain] = self.fees[chain].gas_price()
                g = self._gas_price[chain]
                with self.lock:
                    self.store.db.execute("INSERT INTO fee_snapshots (chain, kind, value, status, source, at) VALUES "
                                          "(?,?,?,?,?,?)", (chain, "gas_price_native", g.native, g.status, g.source,
                                                            now))
            self.warm_costs(chain, protos)
        except Exception as e:
            self._gap(chain, now, f"setup:{type(e).__name__}")
            return st
        sizes = C.SCAN_SIZES_SOLANA if chain == "solana" else C.SIZES
        primary = self.arms.get("A_fast")
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        n_ok = 0
        for tok in ch.tokens:
            for size in sizes:
                if ch.kind == "evm":
                    buys = self.quote_many(chain, [(p.key, ch.quote_asset, tok, E.raw(size, ch.quote_asset))
                                                   for p in protos])
                    sell_reqs = [(b.protocol, p.key) for b in buys if b.ok for p in protos if p.key != b.protocol]
                    sells = self.quote_many(chain, [(pk, tok, ch.quote_asset, next(b for b in buys if b.protocol == a)
                                                     .amount_out) for a, pk in sell_reqs]) if sell_reqs else []
                    pairs = {(a, b): s for (a, b), s in zip(sell_reqs, sells)}
                else:
                    buys, pairs = [], {}
                    for p in protos:                      # buy then its sells right away (quote skew stays small)
                        b = self.quote_one(chain, p.key, ch.quote_asset, tok, E.raw(size, ch.quote_asset))
                        buys.append(b)
                        if b.ok:
                            for p2 in protos:
                                if p2.key != p.key:
                                    pairs[(p.key, p2.key)] = self.quote_one(chain, p2.key, tok, ch.quote_asset,
                                                                            b.amount_out)
                n_ok += sum(1 for b in buys if b.ok)
                by = {b.protocol: b for b in buys}
                for a in protos:
                    for b in protos:
                        if a.key == b.key:
                            continue
                        bq, sq = by.get(a.key), pairs.get((a.key, b.key))
                        dec_now = self.clock()
                        if bq is not None and bq.ok and sq is not None and sq.ok:
                            gb, _, s1 = self.leg_costs(chain, a.key, ch.quote_asset, tok, bq, primary)
                            gs, _, s2 = self.leg_costs(chain, b.key, tok, ch.quote_asset, sq, primary)
                            setup = Cost(None, "UNKNOWN", f"{s1.source} | {s2.source}") if not (s1.known and s2.known) \
                                else Cost(s1.native + s2.native, s1.status if s1.native else s2.status,
                                          f"{s1.source} | {s2.source}")
                        else:
                            gb = gs = setup = Cost(None, "UNKNOWN", "no quote")
                        r = E.evaluate_cycle(chain, tok, size, bq, sq, gb, gs, setup, dec_now)
                        r["buy_protocol"], r["sell_protocol"] = a.key, b.key
                        with self.lock:
                            self._record(chain, r, bq, sq, day)
        st["scans"] += 1
        st["last_scan"] = now
        if n_ok:
            self.last_ok[chain] = now
        else:
            self._gap(chain, now, "no buy quote succeeded")
        self._baselines(chain, protos, now)
        with self.lock:
            self.store.commit()
        return st

    def _gap(self, chain: str, now: float, reason: str) -> None:
        self.stats[chain]["last_error"] = reason
        with self.lock:
            self.store.gap(chain, "*", self.last_ok.get(chain, now), now, reason)
            self.store.commit()

    def _record(self, chain: str, r: dict, bq, sq, day: str) -> int | None:
        st = self.stats[chain]
        st["evaluated"] += 1
        uid = E.opp_uid(r, bq or Quote(chain, "", "", "", 0, None), sq or Quote(chain, "", "", "", 0, None))
        if r["status"] == "REJECTED":
            st["rejected"][r["reason"]] = st["rejected"].get(r["reason"], 0) + 1
            self.store.rollup(day, chain, r["token"], r["size"], r["buy_protocol"], r["sell_protocol"], r["reason"],
                              r["gross_spread"])
            if not E.keep_raw(r, uid):
                return None
        else:
            st["candidates"] += 1
        if self.store.over_quota():
            self.store.gap(chain, "*", self.clock(), self.clock(), "db_quota")
            return None
        bid = self.store.add_quote(bq, "detect_buy") if bq is not None else None
        sid = self.store.add_quote(sq, "detect_sell") if sq is not None else None
        cols = ["uid", "chain", "chain_id", "token", "size", "buy_protocol", "sell_protocol", "buy_quote_id",
                "sell_quote_id", "detected_at", "buy_context", "sell_context", "quote_age_s", "amount_in", "tokens_mid",
                "amount_out", "gross_spread", "swap_fees", "gas_cost", "setup_cost", "other_cost", "estimated_costs",
                "net_profit_estimate", "uncertainty_buffer", "net_profit_after_buffer", "cost_status", "cost_detail",
                "impact_buy", "impact_sell", "status", "reason", "sampled", "baseline"]
        vals = dict(r, uid=uid, buy_quote_id=bid, sell_quote_id=sid,
                    sampled=int(r["status"] == "REJECTED" and not (r.get("gross_spread") or 0) > 0),
                    baseline=int(r.get("baseline", 0)))
        cur = self.store.db.execute(f"INSERT OR IGNORE INTO opportunities ({','.join(cols)}) VALUES "
                                    f"({','.join('?' * len(cols))})", [vals.get(c) for c in cols])
        if not cur.rowcount:
            return None                                   # duplicate (same quotes) -> no second row / cycle
        oid = cur.lastrowid
        if r["status"] == "CANDIDATE" and self.paper_enabled:
            for arm in self.arms.values():
                arm.fund(chain, self.price[chain].native if chain in self.price else None)
                arm.on_candidate(oid, r)
        return oid

    def _baselines(self, chain: str, protos: list, now: float) -> None:
        """Non-signal benchmark: one random (token, pair, size) cycle per arm per hour, executed by the same model."""
        if not self.paper_enabled:
            return
        if self.store.over_quota():
            with self.lock:
                self.store.gap(chain, "*", now, now, "db_quota")
            return
        ch = CHAINS[chain]
        for name, arm in self.arms.items():
            if now - self.last_baseline.get((chain, name), -1e18) < C.BASELINE_EVERY_S:
                continue
            self.last_baseline[(chain, name)] = now
            pick = arm.pick_baseline(chain, [p.key for p in protos])
            if not pick:
                continue
            tok, a, b, size = pick
            bq = self.quote_one(chain, a, ch.quote_asset, tok, E.raw(size, ch.quote_asset))
            sq = self.quote_one(chain, b, tok, ch.quote_asset, bq.amount_out) if bq.ok else None
            r = E.evaluate_cycle(chain, tok, size, bq, sq, Cost(0.0, "MEASURED", "baseline"),
                                 Cost(0.0, "MEASURED", "baseline"), Cost(0.0, "MEASURED", "baseline"), self.clock())
            if r["reason"] in ("no_route", "quote_failed", "chain_mismatch", "token_mismatch", "quote_stale",
                               "context_mismatch", "impact_high"):
                with self.lock:                          # same execution limits as signal cycles; else no baseline
                    self.store.audit("baseline_skipped", {"chain": chain, "arm": name, "reason": r["reason"],
                                                          "token": tok.symbol, "pair": [a, b], "size": size})
                continue
            with self.lock:
                self._baseline_row(chain, ch, name, arm, tok, a, b, size, bq, sq, r, now)

    def _baseline_row(self, chain, ch, name, arm, tok, a, b, size, bq, sq, r, now) -> None:
        r.update(buy_protocol=a, sell_protocol=b, status="BASELINE", reason=f"random benchmark ({name})",
                 baseline=1)
        uid_r = dict(r, token=f"{r['token']}|{name}|{now}")
        bid = self.store.add_quote(bq, "baseline_buy")
        cur = self.store.db.execute(
            "INSERT OR IGNORE INTO opportunities (uid, chain, chain_id, token, size, buy_protocol, sell_protocol, "
            "buy_quote_id, detected_at, tokens_mid, amount_out, gross_spread, status, reason, baseline) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (E.opp_uid(uid_r, bq, sq or bq), chain, str(ch.chain_id), r["token"], size, a, b, bid, r["detected_at"],
             r["tokens_mid"], r["amount_out"], r["gross_spread"], "BASELINE", r["reason"]))
        if cur.rowcount and bq.ok:
            arm.fund(chain, self.price[chain].native if chain in self.price else None)
            arm.on_candidate(cur.lastrowid, r, baseline=True)

    # --- periodic -------------------------------------------------------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            for arm in self.arms.values():
                arm.tick(now)
            if now - self.last_metrics >= METRICS_EVERY_S:
                self.last_metrics = now
                self.store.record_metrics()
            if now - self.last_retention >= RETENTION_EVERY_S:
                self.last_retention = now
                self.store.retention(now, dry_run=os.environ.get("DEXARB_RETENTION") != "apply")

    async def run(self, stop: asyncio.Event) -> None:
        async def chain_loop(c):
            while not stop.is_set():
                t0 = time.time()
                try:
                    await asyncio.to_thread(self.scan, c)
                except Exception as e:
                    self.log(f"[dexarb:{c}] scan error {type(e).__name__}: {e}")
                    self._gap(c, time.time(), f"scan:{type(e).__name__}")
                await asyncio.sleep(max(1.0, CHAINS[c].scan_every_s - (time.time() - t0)))

        async def timers():
            while not stop.is_set():
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(0.5)
        await asyncio.gather(timers(), *[chain_loop(c) for c in self.chains])

    def atomic(self) -> dict:
        return ATOMIC
