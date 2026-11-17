"""Smart Caller Hunter runtime: ingest posts (X API or manual import), discover callers, evaluate calls at 1h / 6h /
24h / 7d, recompute caller scores and tiers, score live calls into signals, keep the watchlist. One background thread,
every step budgeted (X spend cap, GeckoTerminal ~30 calls / min shared with the Insiders charts)."""
from __future__ import annotations

import csv
import json
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from callers import config as C
from callers.evaluate import evaluate_call
from callers.extract import extract, parse_post_url, snowflake_time
from callers.score import caller_metrics, signal_score
from callers.xapi import BudgetExceeded, XDisabled, iso_ts, post_urls

LIVE_WINDOW_S = 600            # a post seen within 10 min of being published is a LIVE call (can become a signal)
EVAL_PER_TICK = 3            # each evaluation = ~5-7 paced GeckoTerminal calls
TICK_S = 60


class CallerHunter:
    def __init__(self, store, market, x=None, clock=time.time, log=print, smart_money=None, notify=None):
        self.store, self.market, self.x, self.clock, self.log = store, market, x, clock, log
        self.smart_money = smart_money or (lambda mint: None)
        self.notify = notify
        self.stat = {"ticks": 0, "evaluated": 0, "signals": 0, "x_posts": 0, "error": None, "last_tick": None}
        self.stop = threading.Event()

    # --- ingest ------------------------------------------------------------------------------------------------------
    def ingest_post(self, username: str, post_id: str, created_at: float, text: str, urls: list | None,
                    source: str, platform: str = "x") -> list[int]:
        self.store.add_caller(username, platform=platform, source=source)
        url = f"https://x.com/{username}/status/{post_id}" if platform == "x" else ""
        self.store.add_post(post_id, username, created_at, text or "", url, source)
        f = extract(text or "", urls)
        live = self.clock() - created_at <= LIVE_WINDOW_S
        ids = []
        for m in f.mints:
            cid = self.store.add_call(post_id, username, m, created_at, source, live=live)
            if cid:
                ids.append(cid)
        if not f.mints and f.tickers and f.buy_words:          # ticker-only call: resolved only if unambiguous
            for t in f.tickers[:3]:
                mint, why = self.market.resolve_ticker(t, created_at)
                if mint:
                    cid = self.store.add_call(post_id, username, mint, created_at, source, ticker=t,
                                              resolve_note=why, live=live)
                    if cid:
                        ids.append(cid)
                else:
                    self.store.kv(f"unresolved:{post_id}:{t}", {"reason": why, "at": self.clock()})
        if live:
            for cid in ids:
                self.make_signal(cid)
        return ids

    def import_text(self, text: str, source: str = "import") -> dict:
        """One call per line: `<x post url>[,<mint or $TICKER>]` (time = the post id's own timestamp), or
        `<username>,<ISO time or unix>,<mint>`. Returns counts and per-line errors."""
        added, errors = 0, []
        for i, line in enumerate((text or "").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                pu = parse_post_url(parts[0])
                if pu:
                    user, pid = pu
                    ts = snowflake_time(pid)
                    body = " ".join(parts[1:]) if len(parts) > 1 else ""
                    ids = self.ingest_post(user, pid, ts, body, [parts[0]], source)
                else:
                    user, when, mint = parts[0].lstrip("@"), parts[1], parts[2]
                    ts = float(when) if when.replace(".", "").isdigit() else \
                        datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
                    ids = self.ingest_post(user, f"manual:{user}:{int(ts)}:{mint[:8]}", ts, mint, [], source)
                if ids:
                    added += len(ids)
                else:
                    errors.append(f"line {i}: no Solana mint / unambiguous ticker")
            except Exception as e:
                errors.append(f"line {i}: {type(e).__name__}")
        return {"added": added, "errors": errors[:50]}

    def import_channel_csv(self, path, caller: str = "tg:SOL Pump Insiders") -> int:
        """The Telegram call channel export (insiders/sol_pump_insiders_calls.csv): ca + call_time_utc7."""
        n = 0
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ca, t = (r.get("ca") or "").strip(), (r.get("call_time_utc7") or "").strip()
                if not ca or not t:
                    continue
                ts = (datetime.strptime(t, "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=7)))
                      .timestamp())
                pid = f"tg:{ca}:{int(ts)}"
                n += len(self.ingest_post(caller, pid, ts, ca + " " + (r.get("text") or "")[:200], [], "telegram",
                                          platform="telegram"))
        return n

    # --- X collection (budgeted) --------------------------------------------------------------------------------------
    def x_cycle(self) -> dict:
        if not self.x or not self.x.enabled:
            return {"skipped": "x off"}
        out = {"search": 0, "timelines": 0, "posts": 0}
        try:
            for qi, q in enumerate(C.X_DISCOVERY_QUERIES):
                since = self.store.kv(f"search_since:{qi}")
                d = self.x.search(q, since_id=since)
                users = {u["id"]: u for u in (d.get("includes") or {}).get("users") or []}
                for t in d.get("data") or []:
                    u = users.get(t.get("author_id")) or {}
                    if u.get("username"):
                        self.discover(u["username"], t, u)
                        out["posts"] += 1
                if (d.get("meta") or {}).get("newest_id"):
                    self.store.kv(f"search_since:{qi}", d["meta"]["newest_id"])
                out["search"] += 1
            order = {"ELITE": 0, "QUALIFIED": 1, "CANDIDATE": 2, "NEW": 3}
            callers = self.store.q("SELECT * FROM callers WHERE platform='x' AND (watched=1 OR tier IN "
                                   "('ELITE','QUALIFIED','CANDIDATE','NEW'))")
            for c in sorted(callers, key=lambda c: (0 if c["watched"] else 1, order.get(c["tier"], 9))):
                out["posts"] += self.poll_caller(c)
                out["timelines"] += 1
        except (BudgetExceeded, XDisabled) as e:
            out["stopped"] = str(e)
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        self.stat["x_posts"] += out["posts"]
        return out

    def discover(self, username: str, tweet: dict, user: dict) -> None:
        f = extract(tweet.get("text") or "", post_urls(tweet))
        if not f.mints:
            return
        row = self.store.q("SELECT * FROM discovery WHERE username=?", (username,))
        mints = set(json.loads(row[0]["mints"])) if row else set()
        mints.update(f.mints)
        now = self.clock()
        self.store.x("INSERT INTO discovery (username, mints, refs, first_seen, last_seen) VALUES (?,?,0,?,?) ON "
                     "CONFLICT (username) DO UPDATE SET mints=excluded.mints, last_seen=excluded.last_seen",
                     (username, json.dumps(sorted(mints)[-200:]), now, now))
        for mention in ((tweet.get("entities") or {}).get("mentions") or []):   # referrals ("via @x", "cc @x")
            self.store.x("INSERT INTO discovery (username, mints, refs, first_seen, last_seen) VALUES (?, '[]', 1, ?, ?) "
                         "ON CONFLICT (username) DO UPDATE SET refs=refs+1, last_seen=excluded.last_seen",
                         (mention.get("username"), now, now))
        known = bool(self.store.q("SELECT 1 FROM callers WHERE username=?", (username,)))
        if not known and len(mints) < C.DISCOVERY_MIN_CALLS:
            return                                   # not a caller yet: counted in `discovery` only
        if not known:
            self.store.add_caller(username, source="discovery", user_id=user.get("id"), name=user.get("name"),
                                  followers=(user.get("public_metrics") or {}).get("followers_count"))
            self.store.x("UPDATE discovery SET promoted=1 WHERE username=?", (username,))
        self.ingest_post(username, tweet["id"], iso_ts(tweet["created_at"]), tweet.get("text") or "",
                         post_urls(tweet), "x_search")     # earlier posts come with the history backfill

    def poll_caller(self, c: dict) -> int:
        uid = c["user_id"]
        if not uid:
            u = self.x.user(c["username"])
            if not u:
                return 0
            uid = u["id"]
            self.store.x("UPDATE callers SET user_id=?, name=?, followers=? WHERE username=?",
                         (uid, u.get("name"), (u.get("public_metrics") or {}).get("followers_count"), c["username"]))
        n, token, since = 0, None, c["since_id"]
        budget_posts = C.X_BACKFILL_POSTS if not c["backfilled"] else 100
        newest = since
        while n < budget_posts:
            d = self.x.timeline(uid, since_id=since if c["backfilled"] else None,
                                max_results=min(100, budget_posts - n), pagination_token=token)
            for t in d.get("data") or []:
                self.ingest_post(c["username"], t["id"], iso_ts(t["created_at"]), t.get("text") or "", post_urls(t),
                                 "x_timeline")
                n += 1
            newest = (d.get("meta") or {}).get("newest_id") or newest if not token else newest
            token = (d.get("meta") or {}).get("next_token")
            if not token or c["backfilled"]:
                break
        self.store.x("UPDATE callers SET since_id=?, backfilled=1 WHERE username=?",
                     (newest or since, c["username"]))
        return n

    # --- evaluation ----------------------------------------------------------------------------------------------------
    def due_calls(self, limit: int) -> list[dict]:
        now = self.clock()
        rows = self.store.q(
            "SELECT c.*, o.final AS o_final, o.evaluated_at AS o_at FROM calls c LEFT JOIN outcomes o ON "
            "o.call_id = c.id AND o.method = ? WHERE o.final IS NULL OR o.final = 0 ORDER BY c.post_time DESC",
            (C.METHOD_VERSION,))
        out = []
        for r in rows:
            if r["o_at"] is None:
                out.append(r)
            else:                                     # re-evaluate after each horizon boundary passes
                marks = [r["post_time"] + h + 120 for h in C.HORIZONS_S.values()]
                if any(r["o_at"] < m <= now for m in marks):
                    out.append(r)
            if len(out) >= limit:
                break
        return out

    def evaluate(self, r: dict) -> dict:
        t = r["post_time"]
        fine = self.market.candles(r["mint"], t - 3600, t + 6 * 3600, minutes=1)
        coarse = self.market.candles(r["mint"], t - 86400, t + 7 * 86400, minutes=15)
        sig = self.store.q("SELECT snapshot FROM signals WHERE call_id=?", (r["id"],))
        snap = json.loads(sig[0]["snapshot"]) if sig and sig[0]["snapshot"] else None
        src = "AT_CALL" if snap else "CURRENT_ESTIMATE"
        if snap is None:
            snap = self.market.snapshot(r["mint"]) or {}
        out = evaluate_call(t, fine, coarse, snap.get("liquidity_usd"), self._dex(r["mint"], snap), self.clock(), src,
                            liq_price=snap.get("price_usd"))
        self.store.set_outcome(r["id"], C.METHOD_VERSION, out)
        self.stat["evaluated"] += 1
        return out

    def _dex(self, mint: str, snap: dict) -> str | None:
        pools = self.market.pool_cache.get(mint, (0, []))[1]
        return pools[0]["dex"] if pools else snap.get("dex")

    def refresh_callers(self) -> None:
        for c in self.store.q("SELECT DISTINCT username FROM calls"):
            rows = self.store.outcomes(C.METHOD_VERSION, c["username"])
            m = caller_metrics(rows)
            self.store.x("UPDATE callers SET tier=?, score=?, metrics=?, updated=? WHERE username=?",
                         (m["tier"], m["score"], json.dumps(m), self.clock(), c["username"]))

    # --- signals -------------------------------------------------------------------------------------------------------
    def make_signal(self, call_id: int) -> dict | None:
        r = self.store.q("SELECT * FROM calls WHERE id=?", (call_id,))
        if not r:
            return None
        r = r[0]
        caller = (self.store.q("SELECT tier, score FROM callers WHERE username=?", (r["username"],)) or [{}])[0]
        snap = self.market.snapshot(r["mint"])
        safety = self.market.safety(r["mint"])
        cons = self.store.q("SELECT DISTINCT c.username FROM calls c JOIN callers k ON k.username = c.username WHERE "
                            "c.mint=? AND c.post_time >= ? AND k.tier IN ('ELITE','QUALIFIED')",
                            (r["mint"], r["post_time"] - C.CONSENSUS_WINDOW_S))
        s = signal_score(caller, len(cons), snap, safety, self.smart_money(r["mint"]))
        self.store.x("INSERT OR REPLACE INTO signals (call_id, mint, username, created_at, score, label, components, "
                     "snapshot) VALUES (?,?,?,?,?,?,?,?)",
                     (call_id, r["mint"], r["username"], self.clock(), s["score"], s["label"],
                      json.dumps({**s, "consensus": len(cons), "caller_tier": caller.get("tier"),
                                  "safety": safety}), json.dumps(snap)))
        if snap:
            self.store.x("INSERT OR REPLACE INTO coins VALUES (?,?,?,?,?,?)",
                         (r["mint"], snap.get("symbol"), snap.get("name"), json.dumps(snap), json.dumps(safety),
                          self.clock()))
        self.stat["signals"] += 1
        if self.notify and s["label"] == "HIGH_PRIORITY":
            try:
                self.notify(f"HIGH PRIORITY {snap.get('symbol') if snap else ''} {r['mint']} · @{r['username']} "
                            f"({caller.get('tier')}) · score {s['score']} · not financial advice")
                self.store.x("UPDATE signals SET alerted=1 WHERE call_id=?", (call_id,))
            except Exception:
                pass
        return s

    # --- loop ----------------------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        if self.x and self.x.enabled and now - (self.store.kv("x_last") or 0) >= C.X_POLL_EVERY_S:
            self.store.kv("x_last", now)
            self.stat["x"] = self.x_cycle()
        for r in self.due_calls(EVAL_PER_TICK):
            try:
                self.evaluate(r)
            except Exception as e:
                self.stat["error"] = f"eval {type(e).__name__}: {str(e)[:100]}"
        self.refresh_callers()
        self.stat["ticks"] += 1
        self.stat["last_tick"] = now

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.stat["error"] = f"{type(e).__name__}: {e}"
                self.log("[callers] tick error " + traceback.format_exc(limit=2))
            self.stop.wait(TICK_S)
