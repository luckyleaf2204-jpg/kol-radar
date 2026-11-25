"""New Coins LIVE: every pump.fun coin the moment its CreateEvent is on chain, its buys / sells for 120 minutes, and
evidence-based signals. Source: ONE logsSubscribe on the pump.fun program (insiders.pumplog decodes the events from
the logs, no getTransaction, no Helius unless NEWCOINS_WS points there). Read only, paper only: nothing is signed.

Threads: Stream (websocket, reconnects) -> queue -> Engine (batched SQLite writes, signals). A slow coin never blocks
the stream; duplicates (reconnect, restart) are dropped by key; coins younger than 120 min are reloaded on restart.

Signals (one row per coin and kind; evidence kept, no blended score):
  NEW          the CreateEvent was seen
  EARLY_BUY    first buy by a wallet other than the creator
  SMART_MONEY  a buyer is on the Smart Money watchlist (Early Smart Money research, CREATOR_GROUP never)
  MULTI_WALLET >= MULTI_MIN buyers in the first 15 min that are not known to share an owner / bot group
  HIGH_ALERT   >= 2 Smart Money buyers, or Smart Money + MULTI_WALLET
  RISK         creator bought >= 10 % of supply, >= 3 wallets bought in the creation slot (bundle), creator sold,
               or the creator belongs to a CREATOR_GROUP"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import statistics
import threading
import time
from collections import deque

from insiders import pumplog as P

WS_URL = os.environ.get("NEWCOINS_WS") or "wss://api.mainnet-beta.solana.com"
WINDOW_S = 7200
MULTI_MIN = 5
DEV_RISK = 0.10
BUNDLE_MIN = 3
SMART_REFRESH_S = 600
KEEP_TRADES_S = 2 * 86400
KEEP_QUIET_COINS_S = 3 * 86400


def sane(price: float | None) -> float | None:
    """A SOL price that fits a pump.fun SOL curve (launch ~2.8e-8 SOL / token, migration ~4e-7); else None, never guessed."""
    return price if price and 1e-9 < price < 1e-5 else None


def stage(minute: float) -> str:
    return "0-15" if minute < 15 else "15-60" if minute < 60 else "60-120"


class SmartBook:
    """Who is Smart Money and what we already know about any wallet (from the v5 esm_* tables)."""

    def __init__(self, store):
        self.s, self.smart, self.info, self.loaded = store, {}, {}, 0.0

    def refresh(self, now: float) -> None:
        if now - self.loaded < SMART_REFRESH_S and self.loaded:
            return
        self.loaded = now
        try:
            from insiders import esm_view as V
            if not self.s.q("SELECT 1 FROM esm_coin LIMIT 1"):
                return
            r = V.radar(self.s, False, min_score=4)
            self.smart = {x["wallet"]: {"rank": x["rank"], "multi": x["multi"], "why": x["why"][:3], "creator_of": x["creator_of"]}
                          for x in r["smart"] if not x["creator_of"]}
            info = {}
            for e in self.s.q("SELECT wallet, entity, class FROM esm_wallet_entity"):
                info[e["wallet"]] = {"class": e["class"], "entity": e["entity"], "flags": []}
            for f in self.s.q("SELECT wallet, flag, ref FROM esm_flag"):
                info.setdefault(f["wallet"], {"class": None, "entity": None, "flags": []})["flags"].append(
                    f["flag"] if f["flag"] == "CREATOR_GROUP" else f"{f['flag']}:{f['ref']}")
            self.info = info
        except Exception as e:                                   # never stop the stream for the watchlist
            print(f"[newcoins] smart list: {type(e).__name__}: {e}", flush=True)

    def group(self, w: str) -> str:
        """Owner / bot group id when known, else the wallet itself (independence check for MULTI_WALLET)."""
        i = self.info.get(w) or {}
        if i.get("entity"):
            return "E:" + i["entity"]
        co = [f for f in i.get("flags", []) if f.startswith("COORDINATED")]
        return co[0] if co else w


class Coin:
    __slots__ = ("mint", "creator", "t0", "seen", "buyers", "smart", "keys", "first_buy", "signals", "dev_tokens", "dev_sold",
                 "create_slot", "slot_buyers", "early_buyers", "row", "dirty")

    def __init__(self, row: dict):
        self.mint, self.creator, self.t0, self.row = row["mint"], row["creator"], row["t_create"], row
        self.seen = row["seen_at"]
        self.buyers, self.smart, self.keys, self.early_buyers = set(), [], set(), set()
        self.first_buy, self.signals = None, set(json.loads(row.get("signals") or "[]"))
        self.dev_tokens, self.dev_sold, self.create_slot, self.slot_buyers = row.get("dev_tokens") or 0, bool(row.get("dev_sold")), row.get("create_slot"), set()
        self.dirty = True


class Engine:
    def __init__(self, store, now=time.time, alert=None):
        self.s, self.now, self.alert = store, now, alert
        self.book = SmartBook(store)
        self.coins: dict[str, Coin] = {}
        self.q: queue.Queue = queue.Queue(maxsize=200_000)
        self.stat = {"creates": 0, "trades_tracked": 0, "trades_other": 0, "dups": 0, "signals": 0, "decode_errors": 0, "dropped": 0}
        self.disc_lat: deque = deque(maxlen=500)                # seen_at - creation block time
        self.sig_lat: deque = deque(maxlen=2000)                # seen_at - trade block time
        self.last_retention = 0.0
        self.recover()

    # -- restart: coins younger than the window come back with their trades (no double count, keys rebuilt)
    def recover(self) -> None:
        cut = self.now() - WINDOW_S
        for row in self.s.q("SELECT * FROM nc_coin WHERE t_create >= ? AND closed=0", (cut,)):
            c = Coin(row)
            for t in self.s.q("SELECT * FROM nc_trade WHERE mint=? ORDER BY t", (c.mint,)):
                c.keys.add((t["sig"], t["wallet"], t["buy"]))
                if t["buy"] and not t["dev"]:
                    c.buyers.add(t["wallet"])
                    if c.first_buy is None:
                        c.first_buy = t["t"]
                    if (t["t"] - c.t0) < 900:
                        c.early_buyers.add(t["wallet"])
                    if c.create_slot and t["slot"] == c.create_slot:
                        c.slot_buyers.add(t["wallet"])
                    if t["smart"] and t["wallet"] not in c.smart:
                        c.smart.append(t["wallet"])
            c.dirty = False
            self.coins[c.mint] = c

    def put(self, sig: str, slot: int | None, logs: list[str], seen_at: float) -> None:
        evs = P.events(logs)
        if not evs:
            return
        try:
            self.q.put_nowait((sig, slot, evs, seen_at))
        except queue.Full:
            self.stat["dropped"] += 1

    # -- the engine loop: batches the queue, one SQLite transaction per batch
    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            batch = []
            try:
                batch.append(self.q.get(timeout=0.5))
                while len(batch) < 2000:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            try:
                self.step(batch)
            except Exception as e:
                print(f"[newcoins] engine: {type(e).__name__}: {e}", flush=True)
                time.sleep(1)

    def step(self, batch: list) -> None:
        now = self.now()
        self.book.refresh(now)
        alerts = []
        with self.s.lock:
            db = self.s.db
            for sig, slot, evs, seen in batch:
                for e in evs:
                    if e["ev"] == "create":
                        self._create(db, e, sig, slot, seen)
                    else:
                        alerts += self._trade(db, e, sig, slot, seen)
            for c in list(self.coins.values()):
                if now - c.t0 > WINDOW_S:                     # the window is over: frozen, kept for history
                    c.row["closed"], c.dirty = 1, True
                if c.dirty:
                    self._save(db, c, now)
                if c.row.get("closed"):
                    del self.coins[c.mint]
            db.commit()
        if now - self.last_retention > 3600:
            self.retention(now)
        for a in alerts:
            if self.alert:
                try:
                    self.alert(a)
                except Exception as ex:
                    print(f"[newcoins] alert: {type(ex).__name__}", flush=True)

    def _create(self, db, e, sig, slot, seen) -> None:
        if e["mint"] in self.coins:
            self.stat["dups"] += 1
            return
        t0 = e.get("ts") or seen
        row = {"mint": e["mint"], "name": e["name"][:80], "symbol": e["symbol"][:24], "uri": e["uri"][:300], "creator": e["creator"],
               "bonding_curve": e["bonding_curve"], "platform": "pump.fun", "t_create": t0, "create_sig": sig, "create_slot": slot,
               "seen_at": seen, "price": sane(P.price_sol(e.get("vsol"), e.get("vtok"))) if e.get("vsol") == 30_000_000_000 else None, "mc_sol": None, "liq_sol": 0.0, "buyers": 0, "smart": 0,
               "buy_sol": 0.0, "sell_sol": 0.0, "trades": 0, "dev_sol": 0.0, "dev_tokens": 0.0, "dev_sold": 0, "first_buy_t": None,
               "first_buy_seen": None, "signals": "[]", "updated": seen, "closed": 0, "source": "stream"}
        if e.get("vsol") != 30_000_000_000:                    # not the standard SOL curve: no SOL MC is shown, never guessed
            row["platform"] = "pump.fun (đường cong khác)"
        if row["price"]:
            row["mc_sol"] = row["price"] * P.SUPPLY
        if db.execute("SELECT 1 FROM nc_coin WHERE mint=?", (e["mint"],)).fetchone():
            self.stat["dups"] += 1
            return
        c = Coin(row)
        self.coins[c.mint] = c
        self.stat["creates"] += 1
        self.disc_lat.append(max(0.0, seen - t0))
        cr = self.book.info.get(e["creator"]) or {}
        self._signal(db, c, "NEW", t0, seen, [e["creator"]], {"creator": e["creator"], "name": row["name"], "symbol": row["symbol"]})
        if "CREATOR_GROUP" in cr.get("flags", []):
            self._signal(db, c, "RISK", t0, seen, [e["creator"]], {"why": "creator thuộc CREATOR_GROUP (nhóm phát hành có liên kết đã xác minh)"})

    def _trade(self, db, e, sig, slot, seen) -> list:
        c = self.coins.get(e["mint"])
        if c is None:
            self.stat["trades_other"] += 1                  # a coin created before we listened, or older than 120 min
            return []
        k = (sig, e["user"], int(e["buy"]))
        if k in c.keys:
            self.stat["dups"] += 1
            return []
        c.keys.add(k)
        t = e.get("ts") or seen
        minute = max(0.0, (t - c.t0) / 60)
        if minute * 60 > WINDOW_S:
            return []
        sol, tok = e["sol"] / 1e9, e["tokens"] / 10 ** P.TOKEN_DEC
        price = P.price_sol(e.get("vsol"), e.get("vtok")) if e["sol"] else None
        if not e["sol"] and not e.get("vsol"):                # tokens moved, no SOL side: a curve quoted in another token
            if c.row["platform"] == "pump.fun":
                c.row["platform"], c.row["price"], c.row["mc_sol"] = "pump.fun (quote khác SOL)", None, None
        dev = e["user"] == c.creator
        smart = (not dev) and e["user"] in self.book.smart
        db.execute("INSERT OR IGNORE INTO nc_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (c.mint, sig, e["user"], int(e["buy"]), sol, tok, price, t, slot, seen, int(dev), int(smart)))
        self.stat["trades_tracked"] += 1
        self.sig_lat.append(max(0.0, seen - t))
        r = c.row
        r["trades"] += 1
        if price and r["platform"] == "pump.fun":
            r["price"], r["mc_sol"] = price, price * P.SUPPLY
        if e.get("rsol") is not None:
            r["liq_sol"] = e["rsol"] / 1e9
        out = []
        if e["buy"]:
            r["buy_sol"] += sol
            if dev:
                r["dev_sol"] += sol
                c.dev_tokens += tok
                r["dev_tokens"] = c.dev_tokens
                if c.dev_tokens / P.SUPPLY >= DEV_RISK:
                    self._signal(db, c, "RISK", t, seen, [e["user"]], {"why": f"creator mua {c.dev_tokens / P.SUPPLY:.0%} nguồn cung", "sig": sig})
            else:
                new = e["user"] not in c.buyers
                c.buyers.add(e["user"])
                r["buyers"] = len(c.buyers)
                if c.first_buy is None:
                    c.first_buy, r["first_buy_t"], r["first_buy_seen"] = t, t, seen
                    self._signal(db, c, "EARLY_BUY", t, seen, [e["user"]], {"wallet": e["user"], "sol": round(sol, 4), "sec_after_create": round(t - c.t0, 1), "sig": sig})
                if minute < 15:
                    c.early_buyers.add(e["user"])
                if slot is not None and slot == c.create_slot:
                    c.slot_buyers.add(e["user"])
                    if len(c.slot_buyers) >= BUNDLE_MIN:
                        self._signal(db, c, "RISK", t, seen, sorted(c.slot_buyers), {"why": f"{len(c.slot_buyers)} ví mua ngay trong slot tạo coin (bundle)"})
                if smart and new:
                    c.smart.append(e["user"])
                    r["smart"] = len(c.smart)
                    sm = self.book.smart[e["user"]]
                    ev = {"wallets": [{"wallet": w, **self.book.smart.get(w, {})} for w in c.smart], "last": {"wallet": e["user"], "sol": round(sol, 4), "minute": round(minute, 2), "sig": sig}}
                    self._signal(db, c, "SMART_MONEY", t, seen, list(c.smart), ev, update=True)
                    out.append({"kind": "SMART_MONEY", "mint": c.mint, "symbol": r["symbol"], "name": r["name"], "wallet": e["user"],
                                "rank": sm["rank"], "sol": sol, "minute": minute, "sig": sig, "why": sm["why"]})
                if minute < 15 and "MULTI_WALLET" not in c.signals:
                    groups = {self.book.group(w) for w in c.early_buyers}
                    if len(groups) >= MULTI_MIN:
                        self._signal(db, c, "MULTI_WALLET", t, seen, sorted(c.early_buyers)[:50],
                                     {"buyers_15m": len(c.early_buyers), "independent_groups": len(groups),
                                      "note": "độc lập theo dữ liệu chủ thể / nhóm bot đã có; chưa truy funding"})
                hi = len(c.smart) >= 2 or (c.smart and "MULTI_WALLET" in c.signals)
                if hi and "HIGH_ALERT" not in c.signals:
                    self._signal(db, c, "HIGH_ALERT", t, seen, list(c.smart),
                                 {"why": f"{len(c.smart)} ví Smart Money" + (" + MULTI_WALLET" if "MULTI_WALLET" in c.signals else "")})
                    out.append({"kind": "HIGH_ALERT", "mint": c.mint, "symbol": r["symbol"], "name": r["name"], "wallets": list(c.smart), "minute": minute})
        else:
            r["sell_sol"] += sol
            if dev and not c.dev_sold:
                c.dev_sold, r["dev_sold"] = True, 1
                self._signal(db, c, "RISK", t, seen, [e["user"]], {"why": f"creator bán ở phút {minute:.1f}", "sig": sig})
        c.dirty = True
        return out

    def _signal(self, db, c: Coin, kind, t, seen, wallets, evidence, update=False) -> None:
        minute = max(0.0, (t - c.t0) / 60)
        if kind in c.signals and not update:
            if kind == "RISK":                                  # several risk reasons: keep them all
                row = db.execute("SELECT evidence FROM nc_signal WHERE mint=? AND kind='RISK'", (c.mint,)).fetchone()
                ev = json.loads(row[0]) if row else {}
                whys = ev.get("whys") or [ev.get("why")]
                if evidence["why"] not in whys:
                    whys.append(evidence["why"])
                    db.execute("UPDATE nc_signal SET evidence=? WHERE mint=? AND kind='RISK'", (json.dumps({"whys": whys}, ensure_ascii=False), c.mint))
            return
        if kind == "RISK":
            evidence = {"whys": [evidence["why"]], **{k: v for k, v in evidence.items() if k != "why"}}
        if kind in c.signals:
            db.execute("UPDATE nc_signal SET wallets=?, evidence=? WHERE mint=? AND kind=?",
                       (json.dumps(wallets), json.dumps(evidence, ensure_ascii=False), c.mint, kind))
            return
        db.execute("INSERT OR IGNORE INTO nc_signal VALUES (?,?,?,?,?,?,?,?)",
                   (c.mint, kind, t, seen, round(minute, 2), stage(minute), json.dumps(wallets), json.dumps(evidence, ensure_ascii=False)))
        c.signals.add(kind)
        c.row["signals"] = json.dumps(sorted(c.signals))
        self.stat["signals"] += 1
        c.dirty = True

    def _save(self, db, c: Coin, now: float) -> None:
        r = c.row
        r["updated"] = now
        cols = ("mint", "name", "symbol", "uri", "creator", "bonding_curve", "platform", "t_create", "create_sig", "create_slot", "seen_at", "price",
                "mc_sol", "liq_sol", "buyers", "smart", "buy_sol", "sell_sol", "trades", "dev_sol", "dev_tokens", "dev_sold", "first_buy_t",
                "first_buy_seen", "signals", "updated", "closed", "source")
        db.execute(f"INSERT OR REPLACE INTO nc_coin ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(r.get(k) for k in cols))
        c.dirty = False

    def retention(self, now: float) -> None:
        """Bounded disk: raw trades kept 2 days (Smart Money trades forever); quiet coins (< 3 buyers, NEW only) 3 days."""
        self.last_retention = now
        with self.s.lock:
            self.s.db.execute("DELETE FROM nc_trade WHERE t < ? AND smart=0", (now - KEEP_TRADES_S,))
            old = [r[0] for r in self.s.db.execute("SELECT mint FROM nc_coin WHERE t_create < ? AND buyers < 3 AND signals = '[\"NEW\"]'",
                                                   (now - KEEP_QUIET_COINS_S,))]
            for m in old:
                self.s.db.execute("DELETE FROM nc_coin WHERE mint=?", (m,))
                self.s.db.execute("DELETE FROM nc_signal WHERE mint=?", (m,))
            self.s.db.commit()

    def metrics(self) -> dict:
        def pct(d, p):
            v = sorted(d)
            return round(v[min(len(v) - 1, int(len(v) * p))], 2) if v else None
        day = self.now() - 86400
        q1 = lambda sql, a=(): self.s.q(sql, a)[0]["n"]          # noqa: E731
        return {**self.stat, "active_coins": len(self.coins), "queue": self.q.qsize(), "smart_watchlist": len(self.book.smart),
                "discovery_latency_s": {"p50": pct(self.disc_lat, .5), "p90": pct(self.disc_lat, .9)},
                "signal_latency_s": {"p50": pct(self.sig_lat, .5), "p90": pct(self.sig_lat, .9)},
                "coins_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ?", (day,)),
                "with_buys_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ? AND buyers > 0", (day,)),
                "smart_coins_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ? AND smart > 0", (day,)),
                "signals_24h": q1("SELECT COUNT(*) AS n FROM nc_signal WHERE seen_at >= ?", (day,))}


class Stream:
    """logsSubscribe(mentions = pump.fun) -> Engine.put. Reconnects with backoff; status for /healthz and the page."""

    def __init__(self, engine: Engine, url: str = WS_URL, connect=None):
        self.e, self.url, self.connect = engine, url, connect
        self.stat = {"connected": False, "msgs": 0, "reconnects": 0, "last_msg": None, "error": None, "since": None,
                     "provider": "helius" if "helius" in url else "public"}

    def run_forever(self, stop: threading.Event) -> None:
        asyncio.run(self._loop(stop))

    async def _loop(self, stop: threading.Event) -> None:
        import websockets
        backoff = 1
        while not stop.is_set():
            try:
                conn = self.connect() if self.connect else websockets.connect(self.url, max_size=2 ** 22, ping_interval=20, ping_timeout=20)
                async with conn as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [P.PUMP]}, {"commitment": "confirmed"}]}))
                    self.stat.update(connected=True, error=None, since=time.time())
                    backoff = 1
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 30)
                        except asyncio.TimeoutError:
                            continue
                        m = json.loads(raw)
                        res = (m.get("params") or {}).get("result") or {}
                        v = res.get("value")
                        if not v:
                            continue
                        self.stat["msgs"] += 1
                        self.stat["last_msg"] = time.time()
                        if v.get("err") is None:
                            self.e.put(v.get("signature"), (res.get("context") or {}).get("slot"), v.get("logs") or [], time.time())
            except Exception as ex:
                self.stat.update(connected=False, error=f"{type(ex).__name__}: {str(ex)[:120]}")
                self.stat["reconnects"] += 1
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2


# ---------------------------------------------------------------------------------------------- read side (page)
def coins(store, max_age_s: int = WINDOW_S, limit: int = 400) -> list[dict]:
    now = time.time()
    rows = store.q("SELECT mint, name, symbol, creator, platform, t_create, seen_at, price, mc_sol, liq_sol, buyers, smart, buy_sol, sell_sol, "
                   "trades, dev_sol, dev_sold, first_buy_t, first_buy_seen, signals, closed FROM nc_coin WHERE t_create >= ? "
                   "ORDER BY t_create DESC LIMIT ?", (now - max_age_s, limit))
    for r in rows:
        r["signals"] = json.loads(r["signals"] or "[]")
    return rows


def coin_detail(store, mint: str, book: SmartBook | None = None) -> dict | None:
    c = store.q("SELECT * FROM nc_coin WHERE mint=?", (mint,))
    if not c:
        return None
    c = c[0]
    c["signals"] = [{**s, "wallets": json.loads(s["wallets"] or "[]"), "evidence": json.loads(s["evidence"] or "{}")}
                    for s in store.q("SELECT * FROM nc_signal WHERE mint=? ORDER BY t, kind", (mint,))]
    trades = store.q("SELECT sig, wallet, buy, sol, tokens, price, t, slot, seen_at, dev, smart FROM nc_trade WHERE mint=? ORDER BY t, slot LIMIT 3000", (mint,))
    smart, info = (book.smart, book.info) if book else ({}, {})
    by: dict[str, dict] = {}
    for t in trades:
        w = by.setdefault(t["wallet"], {"wallet": t["wallet"], "first_t": t["t"], "first_slot": t["slot"], "buys": 0, "sells": 0, "sol_in": 0.0,
                                         "sol_out": 0.0, "tokens": 0.0, "dev": bool(t["dev"]), "first_sig": t["sig"]})
        if t["buy"]:
            w["buys"] += 1
            w["sol_in"] += t["sol"]
            w["tokens"] += t["tokens"]
        else:
            w["sells"] += 1
            w["sol_out"] += t["sol"]
            w["tokens"] -= t["tokens"]
    buyers = []
    for w in by.values():
        if not w["buys"]:
            continue
        i = info.get(w["wallet"]) or {}
        sm = smart.get(w["wallet"])
        w.update(sec=round(w["first_t"] - c["t_create"], 1), stage=stage((w["first_t"] - c["t_create"]) / 60),
                 in_create_slot=bool(c["create_slot"] and w["first_slot"] == c["create_slot"]),
                 smart=sm, cls=i.get("class"), entity=i.get("entity"), flags=i.get("flags", []),
                 sol_in=round(w["sol_in"], 4), sol_out=round(w["sol_out"], 4), held_pct=round(max(0.0, w["tokens"]) / P.SUPPLY * 100, 3))
        buyers.append(w)
    buyers.sort(key=lambda w: (w["first_t"], w["first_slot"] or 0))
    c["buyers_list"] = buyers
    c["trades_list"] = trades[-200:][::-1]
    return c
