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
from pathlib import Path

from insiders import pumplog as P

WS_URL = os.environ.get("NEWCOINS_WS") or "wss://api.mainnet-beta.solana.com"
WINDOW_S = 7200
MULTI_MIN = 5
DEV_RISK = 0.10
BUNDLE_MIN = 3
SMART_REFRESH_S = 600
# only wallets that put real size in (user, 2026-11-26): on the watchlist only a wallet that bought >= SMART_MIN_BUY_SOL
# in one coin it entered early (0-60 min, research window), and a live buy counts only from SMART_MIN_LIVE_SOL
SMART_MIN_BUY_SOL = float(os.environ.get("SMART_MIN_BUY_SOL") or 5)
SMART_MIN_BUY_MULTI_SOL = float(os.environ.get("SMART_MIN_BUY_MULTI_SOL") or 1)   # a wallet early in >= 2 coins (repeat)
SMART_MIN_LIVE_SOL = float(os.environ.get("SMART_MIN_LIVE_SOL") or 1)
MIN_SCORE = 4                                              # esm_view.radar min_score: the research watchlist rule
# the old research cohorts join the watchlist (user, 2026-11-26): read only from insiders/result.json (wallets.cohorts),
# never changed; exchange / deposit successors (skip) left out. No window data: only the live >= 1 SOL rule applies
SMART_COHORTS = tuple(x.strip().upper() for x in (os.environ.get("SMART_COHORTS") or "DEPTH,STANK,INSIDERS").split(",") if x.strip())
# trades of a coin's first 120 minutes, kept 12 h (disk, 2026-11-26; was 2 days); Smart Money trades (smart=1) are kept
KEEP_TRADES_S = float(os.environ.get("NC_KEEP_TRADES_H") or 12) * 3600
KEEP_QUIET_COINS_S = 3 * 86400
HORIZONS = (("15m", 900), ("1h", 3600), ("2h", 7200), ("6h", 21600), ("24h", 86400))
LAUNCH_PRICE = 30 / 1_073_000_000                          # SOL per token on a fresh standard pump.fun SOL curve
LATE_S = 300                                               # a snapshot taken > 5 min after its mark is "late" (e.g. restart)


def sane(price: float | None) -> float | None:
    """A SOL price that fits a pump.fun SOL curve (launch ~2.8e-8 SOL / token, migration ~4e-7); else None, never guessed."""
    return price if price and 1e-9 < price < 1e-5 else None


def stage(minute: float) -> str:
    return "0-15" if minute < 15 else "15-60" if minute < 60 else "60-120"


_COHORTS: list | None = None


def cohort_wallets() -> list[dict]:
    """DEPTH / STANK / INSIDERS wallets of the old research (insiders/result.json), read once; skip = exchange / deposit."""
    global _COHORTS
    if _COHORTS is None:
        try:
            from insiders.wallets import cohorts
            res = json.loads((Path(__file__).with_name("result.json")).read_text(encoding="utf-8"))
            _COHORTS = [c for c in cohorts(res) if c["cohort"] in SMART_COHORTS and not c.get("skip")]
        except (OSError, ValueError) as e:
            print(f"[newcoins] cohorts: {type(e).__name__}: {e}", flush=True)
            _COHORTS = []
    return _COHORTS


def big(x: dict) -> bool:
    """A watchlist wallet that bought >= SMART_MIN_BUY_SOL in one coin it entered early (not at the coin's creation),
    or, when it entered >= 2 coins early (a repeat insider pattern), >= SMART_MIN_BUY_MULTI_SOL in one of them."""
    buys = [e.get("sol_in") or 0 for e in x.get("entries") or [] if e.get("bucket") in ("0-15", "15-60") and not e.get("at_creation")]
    top = max(buys, default=0)
    return top >= SMART_MIN_BUY_SOL or (len(x.get("early_coins") or []) >= 2 and top >= SMART_MIN_BUY_MULTI_SOL)


def auto_wallets(store) -> dict[str, dict]:
    """Big wallets of the coins the server worker traced (esm_coin.source='worker'): kept in the database (never in
    the file), so they stay on the watchlist across restarts and seed updates. Creators / CREATOR_GROUP never."""
    coins = {r["mint"]: r for r in store.q("SELECT mint, symbol, creator FROM esm_coin WHERE source='worker'")}
    if not coins:
        return {}
    skip = {r["wallet"] for r in store.q("SELECT wallet FROM esm_flag WHERE flag IN ('CREATOR_GROUP', 'CREATOR')")}
    skip |= {r["creator"] for r in store.q("SELECT creator FROM esm_coin WHERE creator IS NOT NULL")}
    lv = {r["wallet"]: r["ref"] for r in store.q("SELECT wallet, ref FROM esm_flag WHERE flag='INDEPENDENCE'")}
    q = ",".join("?" * len(coins))
    out = {}
    for r in store.q(f"SELECT DISTINCT wallet FROM esm_window_wallet WHERE mint IN ({q}) AND bucket IN ('0-15', '15-60')", list(coins)):
        w = r["wallet"]
        if w in skip:
            continue
        rows = store.q("SELECT w.mint, w.bucket, w.sol_in, w.first_buy_min, w.points, c.symbol, c.creator FROM esm_window_wallet w "
                       "JOIN esm_coin c ON c.mint=w.mint WHERE w.wallet=?", (w,))
        early = [x for x in rows if x["bucket"] in ("0-15", "15-60") and x["creator"] != w]
        x = {"entries": [{"bucket": e["bucket"], "sol_in": e["sol_in"], "at_creation": False} for e in early],
             "early_coins": sorted({e["symbol"] for e in early})}
        # same watchlist rule as the research list (esm_view.radar): early in >= 2 coins, or >= MIN_SCORE points in one
        # (early + the price rose >= 2x after it bought / ended in profit): a big buyer of a coin that dumped is not one
        if not big(x) or not (len(x["early_coins"]) >= 2 or max((e["points"] or 0) for e in early) >= MIN_SCORE):
            continue
        mine = [e for e in early if e["mint"] in coins]
        out[w] = {"rank": "AUTO", "multi": len(x["early_coins"]) >= 2, "creator_of": [], "cohort": None,
                  "sources": [f"auto:{e['symbol']}" for e in mine], "early_coins": x["early_coins"],
                  "max_buy_sol": max((e["sol_in"] or 0) for e in early),
                  "why": [f"tự truy vết: vào sớm {e['symbol']} phút {e['first_buy_min']:g}, mua {e['sol_in'] or 0:.2f} SOL" for e in mine][:3],
                  "independence": lv.get(w) or "INSUFFICIENT_DATA"}
    return out


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
            info = {}
            for e in self.s.q("SELECT wallet, entity, class FROM esm_wallet_entity"):
                info[e["wallet"]] = {"class": e["class"], "entity": e["entity"], "flags": []}
            for f in self.s.q("SELECT wallet, flag, ref FROM esm_flag"):
                info.setdefault(f["wallet"], {"class": None, "entity": None, "flags": []})["flags"].append(
                    f["flag"] if f["flag"] == "CREATOR_GROUP" else f"{f['flag']}:{f['ref']}")
            self.info = info
            # independence of a Smart Money wallet (tier-A review, V2 network trace): shown with every signal, never
            # used to raise one; a wallet linked by evidence keeps its rank but is not counted as independent
            self.smart = {x["wallet"]: {"rank": x["rank"], "multi": x["multi"], "why": x["why"][:3], "creator_of": x["creator_of"],
                                        "independence": self.independence(x["wallet"]),
                                        "network": [f for f in (info.get(x["wallet"]) or {}).get("flags", [])
                                                    if f.split(":")[0] in ("PASS_THROUGH", "BATCH_SPLIT")]}
                          for x in r["smart"] if not x["creator_of"] and big(x)}
            from insiders.esm import load_wallet_db
            wdb = load_wallet_db()
            if wdb:                                                  # the ONE watchlist file (insiders/wallet_db.json)
                self.smart = {w: {"rank": e.get("rank") if e.get("rank") is not None else e.get("cohort"), "multi": bool(e.get("multi")),
                                  "why": (e.get("why") or [f"ví {e.get('cohort')} (nghiên cứu cũ)"])[:3], "creator_of": [],
                                  "cohort": e.get("cohort"), "sources": e.get("sources"), "independence": self.independence(w),
                                  "network": [f for f in (info.get(w) or {}).get("flags", []) if f.split(":")[0] in ("PASS_THROUGH", "BATCH_SPLIT")]}
                              for w, e in wdb["wallets"].items() if "CREATOR_GROUP" not in (info.get(w) or {}).get("flags", [])}
            for w, e in auto_wallets(self.s).items():                # coins traced by the server worker
                if w not in self.smart:
                    self.smart[w] = {k: e[k] for k in ("rank", "multi", "why", "creator_of", "cohort", "sources", "independence")} | {
                        "network": [f for f in (info.get(w) or {}).get("flags", []) if f.split(":")[0] in ("PASS_THROUGH", "BATCH_SPLIT")]}
        except Exception as e:                                   # never stop the stream for the watchlist
            print(f"[newcoins] smart list: {type(e).__name__}: {e}", flush=True)

    def v2_counts(self) -> dict:
        """Counts only (for /healthz): what the book read from the V2 network trace."""
        import collections
        fl = collections.Counter(f.split(":")[0] for i in self.info.values() for f in i.get("flags", [])
                                 if f.split(":")[0] in ("INDEPENDENCE", "PASS_THROUGH", "BATCH_SPLIT"))
        return {"flags": dict(fl), "smart_independence": dict(collections.Counter(v.get("independence") for v in self.smart.values())),
                "smart_with_network_flag": sum(1 for v in self.smart.values() if v.get("network"))}

    def independence(self, w: str) -> str:
        """INDEPENDENT / POSSIBLE_LINK / LIKELY_SAME_ENTITY / INSUFFICIENT_DATA: the tier-A review level when there is
        one, else the owner-group class; unknown = INSUFFICIENT_DATA (never assumed independent)."""
        i = self.info.get(w) or {}
        lv = [f.split(":", 1)[1] for f in i.get("flags", []) if f.startswith("INDEPENDENCE:")]
        return lv[0] if lv else i.get("class") or "INSUFFICIENT_DATA"

    def group(self, w: str) -> str:
        """Owner / bot group id when known, else the wallet itself (independence check for MULTI_WALLET). Unchanged
        by V2: the network flags (PASS_THROUGH, BATCH_SPLIT) and the independence level are shown, never counted."""
        i = self.info.get(w) or {}
        if i.get("entity"):
            return "E:" + i["entity"]
        co = [f for f in i.get("flags", []) if f.startswith("COORDINATED")]
        return co[0] if co else w


class Coin:
    __slots__ = ("mint", "creator", "t0", "seen", "buyers", "smart", "keys", "first_buy", "signals", "dev_tokens", "dev_sold",
                 "create_slot", "slot_buyers", "early_buyers", "row", "dirty", "tx_buyers", "snipe5", "maxp", "minp", "last_t",
                 "migrated", "obs")

    def __init__(self, row: dict):
        self.mint, self.creator, self.t0, self.row = row["mint"], row["creator"], row["t_create"], row
        self.seen = row["seen_at"]
        self.buyers, self.smart, self.keys, self.early_buyers = set(), [], set(), set()
        self.first_buy, self.signals = None, set(json.loads(row.get("signals") or "[]"))
        self.dev_tokens, self.dev_sold, self.create_slot, self.slot_buyers = row.get("dev_tokens") or 0, bool(row.get("dev_sold")), row.get("create_slot"), set()
        self.dirty = True
        self.tx_buyers, self.snipe5, self.obs, self.migrated = set(), set(), set(), False
        self.maxp = self.minp = row.get("price")
        self.last_t = None


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
        self.sol_usd, self.sol_usd_ts = None, 0.0               # SOL price in USD (Jupiter, free), for display only
        self.late: dict[str, dict] = {}                        # closed coins (120 min .. 24 h): price only, for the snapshots
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
                    if t["sig"] == c.row.get("create_sig"):
                        c.tx_buyers.add(t["wallet"])
                    elif t["t"] - c.t0 <= 5 and t["slot"] != c.create_slot:
                        c.snipe5.add(t["wallet"])
                if t["price"]:
                    c.maxp = max(c.maxp or t["price"], t["price"])
                    c.minp = min(c.minp or t["price"], t["price"])
                    c.last_t = t["t"]
            c.obs = {o["horizon"] for o in self.s.q("SELECT horizon FROM nc_obs WHERE mint=?", (c.mint,))}
            c.dirty = False
            self.coins[c.mint] = c
        # closed coins younger than 24 h: price tracking resumes from the last snapshot
        for row in self.s.q("SELECT * FROM nc_coin WHERE t_create >= ? AND closed=1", (self.now() - 86400 - LATE_S,)):
            obs = self.s.q("SELECT * FROM nc_obs WHERE mint=? ORDER BY t DESC", (row["mint"],))
            if obs and obs[0]["horizon"] == "24h":
                continue
            last = obs[0] if obs else {}
            self.late[row["mint"]] = {"t0": row["t_create"], "price": row["price"], "maxp": last.get("max_price") or row["price"],
                                      "minp": last.get("min_price") or row["price"], "last_t": last.get("last_trade_t"),
                                      "migrated": bool(last.get("migrated")), "std": row["platform"] == "pump.fun",
                                      "feat": {k: last.get(k) for k in ("buyers", "early15", "smart", "smart_groups", "in_tx", "in_slot", "snipe5",
                                                                      "dev_sold", "buy_sol", "sell_sol", "signals")} if last else self._feat_row(row),
                                      "done": {o["horizon"] for o in obs}}

    def put(self, sig: str, slot: int | None, logs: list[str], seen_at: float) -> None:
        evs = P.events(logs)
        if not evs:
            return
        try:
            self.q.put_nowait((sig, slot, evs, seen_at))
        except queue.Full:
            self.stat["dropped"] += 1

    # -- the engine loop: batches the queue, one SQLite transaction per batch
    def refresh_sol_usd(self, get=None) -> float | None:
        """Jupiter price API (public, not Helius), every 60 s; the last price is kept on failure. Display only."""
        if self.sol_usd and self.now() - self.sol_usd_ts < 60:
            return self.sol_usd
        self.sol_usd_ts = self.now()
        try:
            if get is None:
                import urllib.request
                req = urllib.request.Request("https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112",
                                             headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
                d = json.loads(urllib.request.urlopen(req, timeout=10).read())
            else:
                d = get()
            p = float(((d or {}).get("So11111111111111111111111111111111111111112") or {}).get("usdPrice") or 0)
            if p > 0:
                self.sol_usd = round(p, 4)
        except Exception:
            pass
        return self.sol_usd

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh_sol_usd()
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
                    elif e["ev"] == "complete":
                        self._complete(e)
                    else:
                        alerts += self._trade(db, e, sig, slot, seen)
            for c in list(self.coins.values()):
                self._observe(db, c.mint, c.t0, now, c.obs, self._price_state(c), self._feat(c))
                if now - c.t0 > WINDOW_S:                     # the window is over: frozen, kept for history
                    c.row["closed"], c.dirty = 1, True
                if c.dirty:
                    self._save(db, c, now)
                if c.row.get("closed"):
                    del self.coins[c.mint]
                    self.late[c.mint] = {"t0": c.t0, **self._price_state(c), "std": c.row["platform"] == "pump.fun",
                                         "feat": self._feat(c), "done": set(c.obs)}
            for m, L in list(self.late.items()):
                self._observe(db, m, L["t0"], now, L["done"], L, L["feat"])
                if "24h" in L["done"] or now - L["t0"] > 86400 + 3600:
                    del self.late[m]
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
            L = self.late.get(e["mint"])
            if L is not None and e["sol"] and L["std"]:      # 120 min .. 24 h: price only (no trade rows, no signals)
                p = sane(P.price_sol(e.get("vsol"), e.get("vtok")))
                if p:
                    L["price"], L["last_t"] = p, e.get("ts") or seen
                    L["maxp"], L["minp"] = max(L["maxp"] or p, p), min(L["minp"] or p, p)
                self.stat["trades_late"] = self.stat.get("trades_late", 0) + 1
                return []
            self.stat["trades_other"] += 1                  # a coin created before we listened, or older than 24 h
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
        smart = (not dev) and e["user"] in self.book.smart and (not e["buy"] or sol >= SMART_MIN_LIVE_SOL)   # small buys ignored
        db.execute("INSERT OR IGNORE INTO nc_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (c.mint, sig, e["user"], int(e["buy"]), sol, tok, price, t, slot, seen, int(dev), int(smart)))
        self.stat["trades_tracked"] += 1
        self.sig_lat.append(max(0.0, seen - t))
        r = c.row
        r["trades"] += 1
        if price and r["platform"] == "pump.fun":
            r["price"], r["mc_sol"] = price, price * P.SUPPLY
            c.maxp, c.minp, c.last_t = max(c.maxp or price, price), min(c.minp or price, price), t
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
                if sig == r.get("create_sig"):
                    c.tx_buyers.add(e["user"])
                elif t - c.t0 <= 5 and slot != c.create_slot:
                    c.snipe5.add(e["user"])
                if slot is not None and slot == c.create_slot:
                    c.slot_buyers.add(e["user"])
                    if len(c.slot_buyers) >= BUNDLE_MIN:
                        self._signal(db, c, "RISK", t, seen, sorted(c.slot_buyers), {"why": f"{len(c.slot_buyers)} ví mua ngay trong slot tạo coin (bundle)"})
                if smart and e["user"] not in c.smart:          # its first buy of >= SMART_MIN_LIVE_SOL (a small test buy before it does not block it)
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

    # -- observation round: snapshots only, never read by a signal rule
    def _complete(self, e) -> None:
        c = self.coins.get(e["mint"])
        if c is not None:
            c.migrated = True
        elif e["mint"] in self.late:
            self.late[e["mint"]]["migrated"] = True

    @staticmethod
    def _price_state(c: Coin) -> dict:
        return {"price": c.row.get("price"), "maxp": c.maxp, "minp": c.minp, "last_t": c.last_t, "migrated": c.migrated}

    def _feat(self, c: Coin) -> dict:
        r = c.row
        return {"buyers": len(c.buyers), "early15": len(c.early_buyers), "smart": len(c.smart),
                "smart_groups": len({self.book.group(w) for w in c.smart}), "in_tx": len(c.tx_buyers),
                "in_slot": len(c.slot_buyers - c.tx_buyers), "snipe5": len(c.snipe5), "dev_sold": int(c.dev_sold),
                "buy_sol": round(r["buy_sol"], 4), "sell_sol": round(r["sell_sol"], 4), "signals": json.dumps(sorted(c.signals))}

    @staticmethod
    def _feat_row(row: dict) -> dict:
        return {"buyers": row["buyers"], "early15": None, "smart": row["smart"], "smart_groups": None, "in_tx": None, "in_slot": None,
                "snipe5": None, "dev_sold": row["dev_sold"], "buy_sol": row["buy_sol"], "sell_sol": row["sell_sol"], "signals": row["signals"]}

    def _observe(self, db, mint, t0, now, done: set, ps: dict, feat: dict) -> None:
        for h, sec in HORIZONS:
            if h in done or now < t0 + sec:
                continue
            done.add(h)
            status = "migrated" if ps.get("migrated") else "late" if now - (t0 + sec) > LATE_S else "ok"
            db.execute("INSERT OR IGNORE INTO nc_obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (mint, h, t0 + sec, now, status, ps.get("price"), ps.get("maxp"), ps.get("minp"), LAUNCH_PRICE, ps.get("last_t"),
                        feat.get("buyers"), feat.get("early15"), feat.get("smart"), feat.get("smart_groups"), feat.get("in_tx"),
                        feat.get("in_slot"), feat.get("snipe5"), feat.get("dev_sold"), feat.get("buy_sol"), feat.get("sell_sol"),
                        feat.get("signals"), int(bool(ps.get("migrated")))))
            self.stat["obs"] = self.stat.get("obs", 0) + 1

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
        """Bounded disk: raw trades kept KEEP_TRADES_S (Smart Money trades forever); quiet coins (< 3 buyers, NEW only)
        3 days; then the WAL file is checkpointed and truncated so the deletes do not keep disk."""
        self.last_retention = now
        with self.s.lock:
            self.s.db.execute("DELETE FROM nc_trade WHERE t < ? AND smart=0", (now - KEEP_TRADES_S,))
            old = [r[0] for r in self.s.db.execute("SELECT mint FROM nc_coin WHERE t_create < ? AND buyers < 3 AND signals = '[\"NEW\"]'",
                                                   (now - KEEP_QUIET_COINS_S,))]
            for m in old:
                self.s.db.execute("DELETE FROM nc_coin WHERE mint=?", (m,))
                self.s.db.execute("DELETE FROM nc_signal WHERE mint=?", (m,))
            self.s.db.commit()
            from insiders.track import checkpoint
            self.stat["wal_checkpoint"] = checkpoint(self.s.db)

    def metrics(self) -> dict:
        def pct(d, p):
            v = sorted(d)
            return round(v[min(len(v) - 1, int(len(v) * p))], 2) if v else None
        day = self.now() - 86400
        q1 = lambda sql, a=(): self.s.q(sql, a)[0]["n"]          # noqa: E731
        return {**self.stat, "sol_usd": self.sol_usd, "active_coins": len(self.coins), "late_coins": len(self.late), "queue": self.q.qsize(), "smart_watchlist": len(self.book.smart),
                "smart_v2": self.book.v2_counts(),
                "discovery_latency_s": {"p50": pct(self.disc_lat, .5), "p90": pct(self.disc_lat, .9)},
                "signal_latency_s": {"p50": pct(self.sig_lat, .5), "p90": pct(self.sig_lat, .9)},
                "coins_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ?", (day,)),
                "with_buys_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ? AND buyers > 0", (day,)),
                "smart_coins_24h": q1("SELECT COUNT(*) AS n FROM nc_coin WHERE t_create >= ? AND smart > 0", (day,)),
                "signals_24h": q1("SELECT COUNT(*) AS n FROM nc_signal WHERE seen_at >= ?", (day,)),
                "obs_24h": q1("SELECT COUNT(*) AS n FROM nc_obs WHERE taken_at >= ?", (day,))}


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
    ev = {}
    sm = [r["mint"] for r in rows if r["smart"]]
    if sm:
        for s in store.q(f"SELECT mint, evidence FROM nc_signal WHERE kind='SMART_MONEY' AND mint IN ({','.join('?' * len(sm))})", sm):
            e = json.loads(s["evidence"] or "{}")
            ev[s["mint"]] = {"wallets": [{k: w.get(k) for k in ("wallet", "rank", "multi", "why")} for w in e.get("wallets", [])][:3],
                             "last": e.get("last")}
    for r in rows:
        r["signals"] = json.loads(r["signals"] or "[]")
        if r["mint"] in ev:
            r["smart_ev"] = ev[r["mint"]]
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
    c["dev_history"] = dev_history(store, c["creator"], mint)
    cr = (info or {}).get(c["creator"]) or {}
    c["dev_flags"], c["dev_class"] = cr.get("flags", []), cr.get("class")
    return c


_META: dict[str, tuple[float, dict | None]] = {}
_META_LOCK = threading.Lock()
IPFS_GW = "https://gateway.pinata.cloud/ipfs/"            # ipfs.io / dweb.link refuse server requests (403)


def metadata(uri: str | None, get=None, now=time.time) -> dict | None:
    """The coin's metadata JSON set by its creator at launch (the CreateEvent uri): X / Telegram / website / description,
    and the tool that launched it (the metadata host, or "Deployed using ..."). https to public hosts only, 64 KB max,
    cached 1 h (a failure 10 min). Untrusted text: the page escapes it."""
    from urllib.parse import urlsplit
    from insiders.pumpinfo import _https, _public_https
    if not uri:
        return None
    with _META_LOCK:
        hit = _META.get(uri)
    if hit and now() < hit[0]:
        return hit[1]
    url = uri
    if "/ipfs/" in uri and urlsplit(uri).hostname in ("ipfs.io", "dweb.link", "w3s.link", "cloudflare-ipfs.com"):
        url = IPFS_GW + uri.split("/ipfs/", 1)[1]
    host = (urlsplit(uri).hostname or "").lower()
    out = None
    if _public_https(url):
        try:
            if get is None:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                d = json.loads(urllib.request.urlopen(req, timeout=8).read(65536))
            else:
                d = get(url)
            desc = (d.get("description") or "").strip()[:600] if isinstance(d, dict) else ""
            launcher = None
            if "Deployed using" in desc:
                launcher = desc.split("Deployed using", 1)[1].strip().split()[0][:60] if desc.split("Deployed using", 1)[1].strip() else None
            elif host and "ipfs" not in host:
                launcher = host
            out = {"twitter": _https(d.get("twitter")), "telegram": _https(d.get("telegram")), "website": _https(d.get("website")),
                   "description": desc or None, "created_on": (d.get("createdOn") or None), "launcher": launcher,
                   "metadata_host": host}
        except Exception:
            out = None
    with _META_LOCK:
        _META[uri] = (now() + (3600 if out else 600), out)
        if len(_META) > 5000:
            for k in sorted(_META, key=lambda k: _META[k][0])[:1000]:
                del _META[k]
    return out


def dev_history(store, creator: str, mint: str | None = None, limit: int = 12) -> dict:
    """The creator's other coins SEEN BY THIS STREAM (since it started): how many, how they did. Our own data only."""
    rows = store.q("SELECT mint, symbol, name, t_create, buyers, smart, mc_sol, dev_sold, signals, platform FROM nc_coin WHERE creator=? "
                   "ORDER BY t_create DESC LIMIT 200", (creator,))
    other = [r for r in rows if r["mint"] != mint]
    peaks = {o["mint"]: o["mx"] for o in store.q(
        "SELECT mint, MAX(max_price) AS mx FROM nc_obs WHERE mint IN (SELECT mint FROM nc_coin WHERE creator=?) GROUP BY mint", (creator,))}
    for r in other:
        r["peak_mc_sol"] = round(peaks[r["mint"]] * P.SUPPLY, 1) if peaks.get(r["mint"]) else None
        r["signals"] = json.loads(r["signals"] or "[]")
    first = min((r["t_create"] for r in rows), default=None)
    return {"coins_seen": len(other), "since": first, "with_buyers": sum(1 for r in other if r["buyers"]),
            "dev_sold": sum(1 for r in other if r["dev_sold"]), "recent": other[:limit]}
