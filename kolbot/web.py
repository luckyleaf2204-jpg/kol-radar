"""Dashboard server: a tiny HTTP server (stdlib asyncio) serving ui.html and read-only JSON endpoints. Nothing
here trades or writes data. With an access code (env APP_ACCESS_CODE) every /api/* route needs header
X-Access-Code; the page itself and /healthz carry no data and stay open."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from kolbot import api
from kolbot import devs as devs_mod
from kolbot.engine import Curve, sell_sol
from kolbot.report import by_kol, summarize
from kolbot.watch import SUPPLY_TOKENS

UI = Path(__file__).with_name("ui.html")
RAW_SUPPLY = SUPPLY_TOKENS * 10 ** 6
_sum_cache: dict = {}


def _summary_cached(closed: list[dict]) -> dict:
    key = (len(closed), closed[-1]["id"] if closed else None)
    if _sum_cache.get("key") != key:
        _sum_cache.update(key=key, value=summarize(closed))
    return _sum_cache["value"]


def build_state(eng, watch, meta, names: dict[str, str], status: dict) -> dict:
    """Live tab: what the KOLs are buying right now, the live feed and the paper book."""
    sol_usd = meta.sol_usd
    tokens = []
    rows = watch.rows()
    db = eng.store.db if eng.store else None
    devmap = api._token_devs(db, [r["mint"] for r in rows]) if db else {}
    for r in rows:
        meta.want(r["mint"])
        coin = meta.coins.get(r["mint"]) or {}
        creator = coin.get("creator")
        dev_net = r["net"].get(creator) if creator else None
        pos = eng.positions.get(r["mint"])
        paper = None
        if pos:
            c = eng.curves.get(pos.mint) or (Curve(**pos.curve) if pos.curve else None)
            now_sol = max(0.0, sell_sol(c, pos.tokens, eng.cfg)) if c else 0.0
            paper = {"entry_mc_sol": pos.entry_px * RAW_SUPPLY, "spend_sol": pos.spend_sol,
                     "pct": 100 * (now_sol / pos.spend_sol - 1), "kol": names.get(pos.kol, pos.kol[:8]),
                     "kol_wallet": pos.kol, "selling": pos.sell_due_ts is not None}
        r = {k: v for k, v in r.items() if k != "net"}
        for k in r["kols"]:
            k["wallet_url"] = api.solscan("wallet", k["kol"])
        r.update({"coin": coin, "creator": creator, "creator_rating": meta.creators.get(creator) if creator else None,
                  "creator_url": api.solscan("wallet", creator) if creator else None,
                  "dev_sold": dev_net is not None and dev_net > 0, "paper": paper,
                  "dev": devmap.get(r["mint"]) or (devs_mod.dev_badge(db, creator) if db and creator else None)})
        tokens.append(r)
    s = _summary_cached(eng.closed)
    closed = [dict(c, kol_name=names.get(c["kol"], c["kol"][:8]),
                   symbol=(meta.coins.get(c["mint"]) or {}).get("symbol")) for c in eng.closed[-40:]][::-1]
    return {"now": time.time(), "sol_usd": sol_usd, "status": status, "tokens": tokens,
            "feed": [dict(a, symbol=(meta.coins.get(a["mint"]) or {}).get("symbol")) for a in list(watch.feed)[:60]],
            "paper": {"cash": eng.cash, "equity": eng.equity(), "start": eng.cfg.starting_sol, "open": len(eng.positions),
                      "summary": s, "by_kol": by_kol(eng.closed, names, 10), "counts": eng.counts, "closed": closed,
                      "settings": {k: getattr(eng.cfg, k) for k in ("position_sol", "delay_s", "max_open",
                                                                    "min_kol_buy_sol", "extra_slippage_pct")}},
            "kols_tracked": len(names)}


def make_routes(db, roster: dict, get_live=None, get_status=None, symbols=None, want_creator=None,
                signal_engine=None, now_mc=None, want_coin=None) -> dict:
    """path -> handler(query) returning (content_type, body). All handlers only read."""
    def q1(q, k, d=""):
        return (q.get(k) or [d])[0]

    def intq(q, k, d):
        try:
            return int(q1(q, k, d))
        except ValueError:
            return d

    def js(obj):
        return "application/json", json.dumps(obj, default=str).encode()

    def summary(q):
        out = api.summary(db, roster, q1(q, "range", "all"), intq(q, "min_n", 1))
        out["stream"] = get_status() if get_status else None
        out["now"] = time.time()
        return js(out)

    def fq(q, k):
        try:
            v = q1(q, k)
            return float(v) if v != "" else None
        except ValueError:
            return None

    def kols(q):
        return js(api.kol_table(db, roster, q1(q, "range", "all"), intq(q, "min_n", 1), q1(q, "pnl"),
                                q1(q, "status"), q1(q, "sort", "pnl"), q1(q, "dir", "desc"), intq(q, "page", 1),
                                intq(q, "size", 25), q1(q, "group", "kol"), fq(q, "min_wr"),
                                q1(q, "hi_wr") in ("1", "true"), q1(q, "seen")))

    def winrate(q):
        return js(api.winrate_table(db, roster, q1(q, "range", "all"),
                                    intq(q, "min_n", api.WINRATE_DEFAULT_MIN_N), q1(q, "pnl"), q1(q, "status"),
                                    fq(q, "min_wr"), q1(q, "hi_wr") in ("1", "true"), q1(q, "seen"),
                                    intq(q, "page", 1), intq(q, "size", 25), q1(q, "group", "kol")))

    def kol(q):
        d = api.kol_detail(db, roster, q1(q, "wallet"), q1(q, "range", "all"), intq(q, "page", 1),
                           intq(q, "size", 25), symbols() if symbols else None)
        return js(d if d is not None else {"error": "not_found"})

    def devs(q):
        return js(api.dev_table(db, intq(q, "min_tokens", 2), q1(q, "risk"), q1(q, "sort", "tokens"),
                                q1(q, "dir", "desc"), intq(q, "page", 1), intq(q, "size", 25), q1(q, "q")))

    def dev(q):
        w = q1(q, "wallet")
        if want_creator and w:
            want_creator(w)                      # fetch the dev's pump.fun history in the background
        d = api.dev_detail(db, w, intq(q, "page", 1), intq(q, "size", 25))
        if d and want_coin:                     # ask pump.fun for names / creation dates the stream never saw
            for (m,) in db.execute("SELECT mint FROM tokens WHERE creator = ? AND (name IS NULL OR created_ts IS NULL) "
                                   "LIMIT 40", (w,)):
                want_coin(m)
        return js(d if d is not None else {"error": "not_found", "wallet": w,
                                           "wallet_url": api.solscan("wallet", w) if w else None})

    def tokens(q):
        return js(api.token_table(db, q1(q, "scope", "kol"), intq(q, "page", 1), intq(q, "size", 25), q1(q, "q")))

    def token(q):
        d = api.token_detail(db, q1(q, "mint"), roster)
        if want_coin and q1(q, "mint") and (d is None or not d.get("name") or not d.get("created_ts")):
            want_coin(q1(q, "mint"))
        return js(d if d is not None else {"error": "not_found"})

    def trades(q):
        return js(api.trade_table(db, roster, q1(q, "range", "all"), q1(q, "kol"), q1(q, "exit"), q1(q, "pnl"),
                                  intq(q, "page", 1), intq(q, "size", 25)))

    def smart(q):
        from kolbot import smart_api
        return js(smart_api.smart_table(db, intq(q, "min_n", smart_api.DEFAULT_MIN_N), q1(q, "top"),
                                        q1(q, "status"), q1(q, "pnl"), q1(q, "hide", "1") not in ("0", "false"),
                                        intq(q, "page", 1), intq(q, "size", 25)))

    def smart_summary(q):
        from kolbot import smart_api
        return js(smart_api.smart_summary(db, intq(q, "min_n", smart_api.DEFAULT_MIN_N)))

    def smart_wallet(q):
        from kolbot import smart_api
        d = smart_api.smart_detail(db, q1(q, "wallet"), intq(q, "page", 1), intq(q, "size", 25))
        return js(d if d is not None else {"error": "not_found"})

    def sigs(q):
        from kolbot import signals as SG
        return js(SG.list_signals(db, q1(q, "source"), q1(q, "state"), intq(q, "since_id", 0), intq(q, "page", 1),
                                  intq(q, "size", 30), symbols() if symbols else None, now_mc() if now_mc else None))

    def sigs_top(q):
        from kolbot import signals as SG
        out = {"outcomes": SG.outcome_stats(db) if api._has(db, "signals") else {}, "auto_trade": False}
        if signal_engine:
            out.update(SG.top_lists(signal_engine))
        return js(out)

    def sig_state(q):
        from kolbot import signals as SG
        ok = SG.set_state(db, intq(q, "id", 0), q1(q, "state"))
        return js({"ok": ok})

    routes = {"/api/signals": sigs, "/api/signals/top": sigs_top, "/api/signals/state": ("POST", sig_state),
              "/api/smart": smart, "/api/smart/summary": smart_summary, "/api/smart/wallet": smart_wallet,
              "/api/summary": summary, "/api/kols": kols, "/api/winrate": winrate, "/api/kol": kol, "/api/devs": devs, "/api/dev": dev,
              "/api/tokens": tokens, "/api/token": token, "/api/trades": trades,
              "/api/status": lambda q: js(dict(get_status() if get_status else {}, now=time.time())),
              "/api/export.csv": lambda q: ("text/csv; charset=utf-8", api.export_csv(db))}
    if get_live:
        routes["/api/state"] = lambda q: js(get_live())
    return routes


def _reply(writer, code: str, ctype: str, body: bytes, head_only: bool = False) -> None:
    writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                 "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + (b"" if head_only else body))


async def serve(routes: dict, host: str = "127.0.0.1", port: int = 8780, log=print, access_code: str | None = None,
                health=None):
    async def handle(reader, writer):
        try:
            line = (await reader.readline()).decode("latin-1")
            headers = {}
            while (h := await reader.readline()) not in (b"\r\n", b"\n", b""):
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            parts = line.split(" ")
            method, target = (parts[0], parts[1]) if len(parts) > 1 else ("GET", "/")
            u = urlsplit(target)
            path, query = u.path, parse_qs(u.query)
            head = method == "HEAD"
            if path.startswith("/api/"):
                if access_code and not hmac.compare_digest(headers.get("x-access-code", ""), access_code):
                    _reply(writer, "401 Unauthorized", "application/json", b'{"error":"code"}', head)
                elif path in routes:
                    h = routes[path]
                    need = "GET"
                    if isinstance(h, tuple):                  # ("POST", handler): a route that writes
                        need, h = h
                    if need == "POST":
                        n = int(headers.get("content-length") or 0)
                        if n:
                            await reader.readexactly(min(n, 65536))
                    if (method == "POST") != (need == "POST"):
                        _reply(writer, "405 Method Not Allowed", "application/json", b'{"error":"method"}', head)
                    else:
                        ctype, body = h(query)
                        _reply(writer, "200 OK", ctype, body, head)
                else:
                    _reply(writer, "404 Not Found", "application/json", b'{"error":"route"}', head)
            elif path == "/healthz":
                _reply(writer, "200 OK", "application/json", json.dumps(health() if health else {"ok": True}).encode(),
                       head)
            elif path in ("/", "/index.html"):
                _reply(writer, "200 OK", "text/html; charset=utf-8", UI.read_bytes(), head)
            else:
                _reply(writer, "404 Not Found", "text/plain", b"", head)
        except Exception as e:
            log(f"[web] {type(e).__name__}: {e}")
            try:
                _reply(writer, "500 Internal Server Error", "application/json", b'{"error":"server"}')
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port)
    log(f"[web] dashboard http://{host}:{port}")
    async with server:
        await server.serve_forever()
