"""Insiders site server (KOL Radar host): stdlib HTTP, read-only research page. PAPER ONLY: nothing is signed or sent.

  GET  /healthz                 public: liveness, commit, storage durability (no data)
  GET  / , /app.js, /styles.css the page (static)
  GET  /api/insiders            X-Access-Code: last scan result + watch status
  GET  /api/insiders/watch      X-Access-Code: watch status only (the page polls it every 30 s)
  POST /api/insiders/run        X-Access-Code: rescan (needs HELIUS_API_KEY on the server; the key never leaves it)

The watch (public RPC, every 2 min) and its $500 paper book keep their state in INSIDERS_STATE_DIR (default: the
directory of KOL_DB, i.e. the persistent disk /var/data on Render)."""
from __future__ import annotations

import gzip
import hmac
import json
import os
import re
import socket
import subprocess
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from insiders.service import InsiderService

PAGE = Path(__file__).with_name("page")
STATIC = {"/": ("live/index.html", "text/html; charset=utf-8"), "/index.html": ("live/index.html", "text/html; charset=utf-8"),
          "/live/live.js": ("live/live.js", "application/javascript; charset=utf-8"),
          "/live/live.css": ("live/live.css", "text/css; charset=utf-8"),
          # the earlier Insiders dashboard (watch, VIP, paper): secondary menu
          "/dashboard/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          # the earlier research page (generated from the SOL Memecoin Hunter UI by tools/build_insiders_page.py)
          "/legacy/": ("legacy/index.html", "text/html; charset=utf-8"),
          "/legacy/index.html": ("legacy/index.html", "text/html; charset=utf-8"),
          "/legacy/app.js": ("legacy/app.js", "application/javascript; charset=utf-8"),
          "/legacy/styles.css": ("legacy/styles.css", "text/css; charset=utf-8"),
          # Early Smart Money (120-minute window): read-only, data from the v5 esm_* tables
          "/early/": ("early/index.html", "text/html; charset=utf-8"),
          "/early/index.html": ("early/index.html", "text/html; charset=utf-8"),
          "/early/early.js": ("early/early.js", "application/javascript; charset=utf-8"),
          "/early/early.css": ("early/early.css", "text/css; charset=utf-8")}


def asset_version() -> str:
    """Cache-busting tag for the page's own assets (changes whenever app.js / styles.css change)."""
    import hashlib
    h = hashlib.sha1()
    for n in ("app.js", "styles.css"):
        try:
            h.update((PAGE / n).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def state_dir() -> tuple[Path, bool]:
    """Where the watch / paper state lives and whether it survives a restart (a mounted disk)."""
    d = os.environ.get("INSIDERS_STATE_DIR") or (str(Path(os.environ["KOL_DB"]).parent) if os.environ.get("KOL_DB")
                                                  else str(Path(__file__).resolve().parents[1] / "data"))
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p, str(p).startswith("/var/data")


def git_commit() -> str | None:
    c = os.environ.get("RENDER_GIT_COMMIT")
    if c:
        return c
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).resolve().parents[1]).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def make_handler(svc: InsiderService, access_code: str | None, health: dict, callers=None, esm=None, nc=None):
    class H(BaseHTTPRequestHandler):
        server_version = "insiders"
        protocol_version = "HTTP/1.1"                       # keep-alive: every response sets Content-Length

        def log_message(self, fmt, *args):                  # quiet access log (no headers, no codes)
            pass

        def finish(self):
            """Graceful close: half-close then drain what the client still sends. Closing a socket with unread input
            makes Windows send an RST that can wipe a response already written (seen locally: CSS / JSON cut off)."""
            super().finish()
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(0.5)
                while self.connection.recv(65536):
                    pass
            except OSError:
                pass

        def _send(self, code: int, ctype: str, body: bytes):
            gz = len(body) > 2048 and "gzip" in (self.headers.get("accept-encoding") or "") and \
                ctype.split(";")[0] in ("application/json", "application/javascript", "text/css", "text/html")
            if gz:                                            # large tables (Early Smart Money: ~0.7 MB) go out ~10x smaller
                body = gzip.compress(body, 5)
            self.send_response(code)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode())

        def _authed(self) -> bool:
            if access_code and not hmac.compare_digest(self.headers.get("x-access-code", ""), access_code):
                self._json(401, {"error": "code"})
                return False
            return True

        def _fail(self, e: Exception) -> None:
            """An exception here used to close the socket with no status line (Render reports that as 502)."""
            try:
                from insiders.vip import redact
                msg = redact(str(e)[:240])
            except Exception:
                msg = type(e).__name__
            path = (self.path or "").split("?", 1)[0]
            frames = " | ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
                                for f in traceback.extract_tb(e.__traceback__, limit=8))
            print(f"[insiders] {self.command} {path} {type(e).__name__}: {msg} [{frames}]", flush=True)
            try:
                self._json(500, {"error": "internal"})
            except Exception:
                pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                self._do_GET()
            except Exception as e:
                self._fail(e)

        def _do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":                           # counts only: no address, no alert content
                h = dict(health)
                w = getattr(svc, "watcher", None)
                if w is not None:
                    h["watch_polls"] = w.state.get("polls", 0)
                    v = getattr(w, "vip", None)
                    if v is not None:
                        h["vip"] = {"connected": v.stat.get("connected"), "subscribed": v.stat.get("subscribed"),
                                    "pushed": v.stat.get("pushed", 0), "handled": v.stat.get("handled", 0),
                                    "missed": v.stat.get("missed", 0), "error": v.stat.get("error"),
                                    "rpc_throttled": getattr(v.rpc, "throttled", None),
                                    "rpc_gap_s": getattr(v.rpc, "gap_s", None),
                                    "children": len(v.state.get("children") or {}),
                                    "parked_under_min_sol": sum(1 for k in (v.state.get("children") or {}).values() if k.get("bal") is not None and k["bal"] < getattr(__import__("insiders.vip", fromlist=["CHILD_MIN_SOL"]), "CHILD_MIN_SOL", 0)),
                                    "focus": getattr(v, "focus", None) or ["ALL"], "base": len(getattr(v, "base", {}) or {}),
                                    "children_seed": sum(1 for x in (v.state.get("children") or {}).values() if x.get("seed")),
                                    "removed_by_day": removed_counts(v.state.get("removed") or {}),
                                    "turbo": v.turbo() if hasattr(v, "turbo") else None,
                                    "paused": v.paused() if hasattr(v, "paused") else None,
                                    "wallets": len(v.wallets()), "poll": dict(getattr(v, "pstat", {}) or {}),
                                    "flows": len(w.state.get("flows", [])),
                                    "provider": __import__("insiders.vip", fromlist=["PROVIDER"]).PROVIDER}
                    pf = getattr(w, "portfolio", None)
                    if pf is not None:
                        h["portfolio"] = {"updated": pf.updated, "seconds": pf.seconds,
                                          "wallets": len((pf.view or {}).get("wallets", [])), "progress": pf.progress,
                                          "error": pf.error, "rpc_calls": getattr(pf.rpc, "calls", None),
                                          "rpc_throttled": getattr(pf.rpc, "throttled", None)}
                    h["mode"] = getattr(w, "mode", None)
                    ds = getattr(w, "devstats", None)
                    if ds is not None:
                        h["devstats"] = {**ds.stat, "queue": len(ds.todo), "devs": len(ds.devs), "coins": len(ds.coins)}
                    pu = getattr(w, "pump", None)
                    if pu is not None:
                        h["pump"] = dict(pu.stat)                 # pump.fun lookups: counts only
                    sh = getattr(w, "shadow", None)
                    if sh is not None:                        # counts only (no coins, no wallets)
                        sg = sh.s.get("signals", [])
                        h["shadow"] = {"signals": len(sg), "live": sum(x["status"] == "live" for x in sg),
                                       "waiting": sum(x["status"] == "wait_price" for x in sg),
                                       "paper_open": sum(1 for x in sg if (x.get("paper") or {}).get("status") == "open"),
                                       "paper_closed": sum(1 for x in sg if (x.get("paper") or {}).get("status") == "closed"),
                                       "exits": dict(__import__("collections").Counter((x.get("paper") or {}).get("exit")
                                                     for x in sg if (x.get("paper") or {}).get("exit"))),
                                       "kinds": dict(__import__("collections").Counter(x["kind"] + "/" + x["tier"] for x in sg)),
                                       "ticks": sh.stat.get("ticks"), "error": sh.stat.get("error")}
                tr = getattr(svc, "tracker", None)
                if tr is not None:                            # coin watchlist: counts only
                    st = tr.status()
                    h["track"] = {k: st.get(k) for k in ("mints", "by_status", "queue", "funding_todo", "events", "edges",
                                                         "signals", "calls", "paused", "shadow_on", "schema", "last_error",
                                                         "gaps_open", "rpc_today")}
                wh = getattr(svc, "wallet_history", None)
                if wh is not None:                            # B3: on / off and the calls spent today, counts only
                    h["wallet_history"] = {"enabled": wh.enabled, "calls_today": wh._used(
                        time.strftime("%Y-%m-%d", time.gmtime())), "safe_stop": wh.stat.get("safe_stop")}
                if getattr(svc, "last_backup", None) is not None or getattr(svc, "backup_error", None):
                    h["backup"] = {"last": getattr(svc.last_backup, "name", None), "error": svc.backup_error}
                if nc is not None:                            # New Coins LIVE: counts only
                    m = nc[0].metrics()
                    h["newcoins"] = {"connected": nc[1].stat["connected"], "msgs": nc[1].stat["msgs"], "reconnects": nc[1].stat["reconnects"],
                                     **{k: m[k] for k in ("creates", "trades_tracked", "active_coins", "queue", "dropped", "signals",
                                                          "discovery_latency_s", "signal_latency_s", "smart_watchlist")}}
                if callers is not None:                       # Smart Caller Hunter: counts only
                    h["callers"] = callers.health()
                return self._json(200, h)
            if path == "/callers":
                self.send_response(301)
                self.send_header("Location", "/callers/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if callers is not None and path.startswith("/callers/"):
                from callers.web import PAGE as CPAGE, STATIC as CSTATIC
                if path in CSTATIC:
                    name, ctype = CSTATIC[path]
                    return self._send(200, ctype, (CPAGE / name).read_bytes())
                return self._send(404, "text/plain", b"")
            if path == "/dashboard":
                self.send_response(301)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path == "/early":
                self.send_response(301)
                self.send_header("Location", "/early/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path == "/legacy":
                self.send_response(301)
                self.send_header("Location", "/legacy/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path in STATIC:
                name, ctype = STATIC[path]
                body = (PAGE / name).read_bytes()
                if name.endswith("index.html"):
                    body = body.replace(b"__V__", asset_version().encode())
                return self._send(200, ctype, body)
            if path.startswith("/api/") and not self._authed():
                return None
            if callers is not None and path.startswith("/api/callers/"):
                from urllib.parse import urlsplit
                res = callers.get(path, urlsplit(self.path).query)
                return self._json(*res) if res else self._json(404, {"error": "route"})
            if path.startswith("/api/nc/"):
                return self._json(*nc_get(nc, esm, path, self.path))
            if path.startswith("/api/insiders/esm/"):
                from urllib.parse import urlsplit
                return self._json(*esm_get(esm, path, self.path, svc))
            if path == "/api/insiders":
                return self._json(200, svc.status())
            if path == "/api/insiders/chart":
                from urllib.parse import parse_qs, urlsplit
                mint = (parse_qs(urlsplit(self.path).query).get("mint") or [""])[0]
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
                    return self._json(400, {"error": "mint"})
                return self._json(200, svc.chart(mint))
            if path == "/api/insiders/coin":
                from urllib.parse import parse_qs, urlsplit
                mint = (parse_qs(urlsplit(self.path).query).get("mint") or [""])[0]
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
                    return self._json(400, {"error": "mint"})
                w = getattr(svc, "watcher", None)
                d = w.coin_view(mint) if w else None
                return self._json(200, d) if d else self._json(404, {"error": "coin"})
            if path == "/api/insiders/wallet":
                from urllib.parse import parse_qs, urlsplit
                addr = (parse_qs(urlsplit(self.path).query).get("addr") or [""])[0]
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr):
                    return self._json(400, {"error": "addr"})
                w = getattr(svc, "watcher", None)
                d = w.wallet_view(addr) if w else None
                return self._json(200, d) if d else self._json(404, {"error": "wallet"})
            if path.startswith("/api/insiders/track/"):    # coin watchlist, events, graph, ranking, paper metrics
                res = track_get(svc, path, self.path)
                return self._json(*res)
            if path == "/api/insiders/watch":
                w = getattr(svc, "watcher", None)
                return self._json(200, {"watch": w.status() if w else None,
                                        "tickers": (svc.result or {}).get("tickers", {})})
            return self._json(404, {"error": "route"})

        def do_POST(self):
            try:
                self._do_POST()
            except Exception as e:
                self._fail(e)

        def _do_POST(self):
            path = self.path.split("?", 1)[0]
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(min(n, 262144)) if n else b""
            if n > 262144:
                self.close_connection = True                   # unread rest of the body: never parse it as a request
            if not path.startswith("/api/") or not self._authed():
                return None if path.startswith("/api/") else self._json(404, {"error": "route"})
            if callers is not None and path.startswith("/api/callers/"):
                from urllib.parse import urlsplit
                res = callers.post(path, urlsplit(self.path).query, body)
                return self._json(*res) if res else self._json(404, {"error": "route"})
            if path == "/api/insiders/pause":                 # "Tạm dừng" / "Bật lại quét" (?on=1 pause | ?on=0 resume)
                from urllib.parse import parse_qs, urlsplit
                v = getattr(getattr(svc, "watcher", None), "vip", None)
                if v is None or not hasattr(v, "set_paused"):
                    return self._json(503, {"error": "no_stream"})
                on = (parse_qs(urlsplit(self.path).query).get("on") or ["0"])[0] == "1"
                return self._json(200, {"paused": v.set_paused(on)})
            if path == "/api/insiders/turbo":                 # "Quét liên tục" on / off (?on=1 | ?on=0)
                from urllib.parse import parse_qs, urlsplit
                v = getattr(getattr(svc, "watcher", None), "vip", None)
                if v is None:
                    return self._json(503, {"error": "no_stream"})
                on = (parse_qs(urlsplit(self.path).query).get("on") or ["0"])[0] == "1"
                until = v.set_turbo(on)
                return self._json(200, {"turbo": v.turbo(), "turbo_until": until})
            if path.startswith("/api/insiders/track/"):
                res = track_post(svc, path, self.path, body)
                return self._json(*res)
            if path == "/api/insiders/run":
                if not svc.key:
                    return self._json(503, {"error": "helius_key_missing"})
                return self._json(200, {"started": svc.start(), "running": svc.running})
            return self._json(404, {"error": "route"})
    return H


_ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _qs(raw: str) -> dict:
    from urllib.parse import parse_qs, urlsplit
    return {k: v[0] for k, v in parse_qs(urlsplit(raw).query).items()}


def esm_get(store, path: str, raw: str, svc=None) -> tuple[int, object]:
    """GET /api/insiders/esm/{summary|wallets|wallet|entities|entity|coins|coin}: Early Smart Money, read only."""
    from insiders import esm_view as V
    if store is None:
        return 503, {"error": "esm_off"}
    q = _qs(raw)
    for k in ("mint", "wallet"):
        if q.get(k) and not _ADDR.fullmatch(q[k]):
            return 400, {"error": k}
    if q.get("id") and not re.fullmatch(r"E\d{1,5}", q["id"]):
        return 400, {"error": "id"}
    name = path.rsplit("/", 1)[-1]
    if name == "summary":
        return 200, V.summary(store)
    if name == "radar":
        t = getattr(svc, "tracker", None)
        on = t is not None and not t.paused()
        note = None if on else ("tracker tắt (chạy với --no-watch hoặc INSIDER_TRACK=0)" if t is None else "tracker đang tạm dừng")
        try:
            ms = max(1, min(6, int(q.get("min_score") or 4)))
        except ValueError:
            return 400, {"error": "min_score"}
        return 200, V.radar(store, on, note, ms)
    if name == "wallets":
        return 200, {"wallets": V.wallet_table(store)}
    if name == "entities":
        return 200, {"entities": V.entity_table(store)}
    if name == "coins":
        return 200, V.coins(store)
    if name == "wallet" and q.get("wallet"):
        d = V.wallet_detail(store, q["wallet"])
        return (200, d) if d else (404, {"error": "wallet"})
    if name == "entity" and q.get("id"):
        d = V.entity_detail(store, q["id"])
        return (200, d) if d else (404, {"error": "entity"})
    if name == "coin" and q.get("mint"):
        d = V.coin_detail(store, q["mint"])
        return (200, d) if d else (404, {"error": "coin"})
    return 404, {"error": "route"}


def nc_get(nc, store, path: str, raw: str) -> tuple[int, object]:
    """GET /api/nc/{status|coins|coin}: New Coins LIVE (read only). `nc` = (engine, stream) when the stream runs."""
    from insiders import newcoins as N
    q = _qs(raw)
    name = path.rsplit("/", 1)[-1]
    eng, stream = nc if nc else (None, None)
    st = eng.s if eng else store
    if name == "status":
        return 200, {"running": eng is not None, "stream": dict(stream.stat) if stream else None,
                     "metrics": eng.metrics() if eng else None,
                     "note": None if eng else "luồng coin mới tắt (chạy main.py không --no-watch, hoặc đặt INSIDER_NEWCOINS=1)"}
    if st is None:
        return 503, {"error": "nc_off"}
    if name == "coins":
        try:
            age = max(60, min(7 * 86400, int(q.get("age") or 7200)))
            limit = max(1, min(1000, int(q.get("limit") or 400)))
        except ValueError:
            return 400, {"error": "number"}
        return 200, {"coins": N.coins(st, age, limit), "now": time.time()}
    if name == "coin":
        if not q.get("mint") or not _ADDR.fullmatch(q["mint"]):
            return 400, {"error": "mint"}
        d = N.coin_detail(st, q["mint"], eng.book if eng else None)
        return (200, d) if d else (404, {"error": "coin"})
    return 404, {"error": "route"}


def open_newcoins(sdir: Path, on: bool):
    """The New Coins stream (public websocket by default, no RPC calls) + engine threads. Off unless asked."""
    flag = os.environ.get("INSIDER_NEWCOINS")
    if flag == "0" or not (on or flag == "1"):
        return None
    import threading
    from insiders import newcoins as N
    from insiders.track import Store
    alert = None
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat and os.environ.get("NEWCOINS_TELEGRAM") == "1":    # opt-in: SMART_MONEY / HIGH_ALERT only
        from insiders.watch import telegram_sender
        send = telegram_sender(tok, chat)

        def alert(a):
            send(f"<b>{a['kind']}</b> {a.get('symbol') or ''} phút {a['minute']:.1f} https://pump.fun/coin/{a['mint']}")
    eng = N.Engine(Store(sdir / "insiders_track.db"), alert=alert)
    stream = N.Stream(eng)
    stop = threading.Event()
    threading.Thread(target=eng.run_forever, args=(stop,), daemon=True, name="nc-engine").start()
    threading.Thread(target=stream.run_forever, args=(stop,), daemon=True, name="nc-stream").start()
    print(f"[insiders] New Coins LIVE: stream {stream.stat['provider']} websocket", flush=True)
    return eng, stream


def open_esm(sdir: Path):
    """The Early Smart Money store (same SQLite file as the tracker; migration v5 = new tables only, backup first).
    When its tables are empty and the research folder exists locally, the 5 verified coins are imported once
    (idempotent). Nothing here calls the network."""
    from insiders import esm as E
    from insiders.track import Store
    if (os.environ.get("INSIDER_ESM_PAGE") or "1") == "0":
        return None
    st = Store(sdir / "insiders_track.db")
    src = Path(os.environ.get("ESM_RESEARCH_DIR") or Path(__file__).resolve().parents[1] / "research" / "seven_coins")
    if not (src / "early_smart_money.json").exists():
        src = Path(__file__).with_name("esm_seed")           # the committed results of the 5 verified coins (server)
    if not st.q("SELECT COUNT(*) AS n FROM esm_coin")[0]["n"] and (src / "early_smart_money.json").exists():
        n = E.import_research(st, src)
        print(f"[insiders] Early Smart Money: đã nạp dữ liệu nghiên cứu {n}", flush=True)
    return st


def track_get(svc, path: str, raw: str) -> tuple[int, object]:
    """GET /api/insiders/track/{status|coins|coin|events|graph|wallets|paper}; every input validated here."""
    t = getattr(svc, "tracker", None)
    if t is None:
        return 503, {"error": "tracker_off"}
    q = _qs(raw)
    for k in ("mint", "wallet"):
        if q.get(k) and not _ADDR.fullmatch(q[k]):
            return 400, {"error": k}
    name = path.rsplit("/", 1)[-1]
    if name == "status":
        return 200, t.status()
    if name == "coins":
        return 200, {"coins": t.coins(), "pending": t.pending()}
    if name == "coin":
        if not q.get("mint"):
            return 400, {"error": "mint"}
        d = t.coin_detail(q["mint"])
        return (200, d) if d else (404, {"error": "coin"})
    if name == "events":
        kind = q.get("kind")
        if kind and not re.fullmatch(r"[A-Z_]{2,12}", kind):
            return 400, {"error": "kind"}
        try:
            since = float(q["since"]) if q.get("since") else None
            limit = max(1, min(1000, int(q.get("limit") or 300)))
        except ValueError:
            return 400, {"error": "number"}
        return 200, {"events": t.events(q.get("mint"), kind, q.get("wallet"), since, limit)}
    if name == "graph":
        return 200, t.graph(mint=q.get("mint"), wallet=q.get("wallet"))
    if name == "radar":
        win = {"5m": 300, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}.get(q.get("win") or "24h")
        if win is None:
            return 400, {"error": "win"}
        return 200, t.radar(window_s=win)
    if name in ("history", "cohorts"):                       # B3/B4: wallet history / PnL (read-only)
        wh = getattr(svc, "wallet_history", None)
        if wh is None:
            return 503, {"error": "wallet_history_off"}
        if name == "cohorts":
            return 200, wh.status()
        if not q.get("wallet"):
            return 400, {"error": "wallet"}
        sm = wh.summary(q["wallet"])
        if sm is None:
            return 404, {"error": "wallet"}
        return 200, {**sm, "pnl": t.s.q("SELECT * FROM wallet_pnl WHERE wallet=? ORDER BY last_ts DESC LIMIT 500",
                                         (q["wallet"],))}
    if name == "wallets":
        w = getattr(svc, "watcher", None)
        vip = getattr(w, "vip", None) if w else None
        rank = t.rank_cache or (t.rank_children(vip) if vip is not None else {})
        top = sorted(rank.items(), key=lambda kv: -kv[1]["score"])[:300]
        bots = t.bot_labels([a for a, _ in top[:150]])
        followed = set(vip.wallets()) if vip is not None else set()
        return 200, {"wallets": [{"wallet": a, **r, "bot": bots.get(a), "followed": a in followed,
                                  "group": vip.group(a) if vip is not None else None} for a, r in top],
                     "rank_version": __import__("insiders.track", fromlist=["RANK_VERSION"]).RANK_VERSION,
                     "weights": __import__("insiders.track", fromlist=["RANK_W"]).RANK_W}
    if name == "paper":
        return 200, t.metrics()
    return 404, {"error": "route"}


def track_post(svc, path: str, raw: str, body: bytes) -> tuple[int, object]:
    """POST /api/insiders/track/{coins|coin|paper|edge}: add / bulk add, pause / resume / remove / retry, measurement
    on / off, hide / show a wrong edge. Read-only towards the chain: nothing here signs or sends anything."""
    t = getattr(svc, "tracker", None)
    if t is None:
        return 503, {"error": "tracker_off"}
    q = _qs(raw)
    name = path.rsplit("/", 1)[-1]
    if name == "coins":
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "json"}
        if not isinstance(data, dict):
            return 400, {"error": "json"}
        from insiders.track import BULK_MAX, parse_bulk
        items = data.get("items")
        if items is None:
            from insiders.track import valid_mint
            items = parse_bulk(str(data.get("text") or ""))
            good = [x for x in items if valid_mint(x["mint"])]
            for x in items:
                x.update({k: data[k] for k in ("history", "live", "tags") if k in data})
                if len(good) == 1 and x is good[0]:          # name / note typed in the form: the one valid address
                    x["name"] = x.get("name") or data.get("name") or ""
                    x["note"] = x.get("note") or data.get("note") or ""
        if not isinstance(items, list) or not items:
            return 400, {"error": "empty"}
        if len(items) > BULK_MAX:
            return 413, {"error": "too_many", "max": BULK_MAX}
        items = [x for x in items if isinstance(x, dict)]
        return 200, {"results": t.add(items)}
    if name == "coin":
        mint, op = q.get("mint") or "", q.get("op") or ""
        if not _ADDR.fullmatch(mint) or op not in ("pause", "resume", "remove", "retry"):
            return 400, {"error": "args"}
        m = t.action(mint, op)
        return (200, {"coin": m}) if m else (404, {"error": "coin"})
    if name == "paper":
        return 200, {"on": t.set_shadow(q.get("on") == "1")}
    if name == "edge":
        try:
            eid = int(q.get("id") or "")
        except ValueError:
            return 400, {"error": "id"}
        return (200, {"ok": True}) if t.hide_edge(eid, q.get("hidden") == "1") else (404, {"error": "edge"})
    return 404, {"error": "route"}


def removed_counts(removed: dict) -> dict:
    """Why followed wallets left, per UTC day: counts only, no address."""
    out: dict = {}
    for x in removed.values():
        day = time.strftime("%Y-%m-%d", time.gmtime(x.get("ts") or 0))
        why = str(x.get("reason") or "?").split(" (")[0]
        out.setdefault(day, {})
        out[day][why] = out[day].get(why, 0) + 1
    return dict(sorted(out.items()))


def _callers_app(svc, sdir: Path, start: bool):
    """Smart Caller Hunter (callers/): its own callers.db next to the watch state; smart money = the Insiders group
    wallets already traded the coin (from the watch state, when the watch runs)."""
    from callers.web import CallersApp

    def smart_money(mint: str):
        w = getattr(svc, "watcher", None)
        if w is None:
            return None
        coin = ((w.state.get("coins") or {}).get(mint) or {})
        return bool(coin.get("wallets"))
    notify = None
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat and (os.environ.get("CALLERS_TELEGRAM") or "0") == "1":   # opt-in: HIGH_PRIORITY only
        from insiders.watch import telegram_sender
        notify = telegram_sender(tok, chat)
    return CallersApp(sdir, start=start, smart_money=smart_money, notify=notify)


def serve(host: str = "127.0.0.1", port: int = 8780, watch: bool = True, svc: InsiderService | None = None,
          callers_app=None):
    svc = svc or InsiderService()
    sdir, durable = state_dir()
    started = svc.start_watch(sdir / "insider_watch.json") if watch and (os.environ.get("INSIDER_WATCH") or "1") != "0" \
        else False
    health = {"ok": True, "app": "insiders", "paper": True, "commit": git_commit(),
              "storage": {"state_dir": str(sdir), "durable": durable}, "watch": bool(started),
              "result": bool(svc.result)}
    code = os.environ.get("APP_ACCESS_CODE") or None
    if host not in ("127.0.0.1", "localhost") and not code:
        print("[insiders] WARNING: public host without APP_ACCESS_CODE: anyone with the URL can read the page", flush=True)
    callers = None
    if callers_app is not False and (os.environ.get("CALLERS") or "1") != "0":
        callers = callers_app or _callers_app(svc, sdir, start=watch)
    esm = open_esm(sdir)
    health["esm"] = esm is not None
    nc = open_newcoins(sdir, on=bool(started))
    health["newcoins"] = nc is not None
    httpd = ThreadingHTTPServer((host, port), make_handler(svc, code, health, callers, esm, nc))
    print(f"[insiders] http://{host}:{port} · state {sdir} (durable {durable}) · watch {bool(started)}", flush=True)
    return httpd


def run_forever(**kw):
    httpd = serve(**kw)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()

