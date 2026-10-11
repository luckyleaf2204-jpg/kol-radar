"""Insiders site server (KOL Radar host): stdlib HTTP, read-only research page. PAPER ONLY: nothing is signed or sent.

  GET  /healthz                 public: liveness, commit, storage durability (no data)
  GET  / , /app.js, /styles.css the page (static)
  GET  /api/insiders            X-Access-Code: last scan result + watch status
  GET  /api/insiders/watch      X-Access-Code: watch status only (the page polls it every 30 s)
  POST /api/insiders/run        X-Access-Code: rescan (needs HELIUS_API_KEY on the server; the key never leaves it)

The watch (public RPC, every 2 min) and its $500 paper book keep their state in INSIDERS_STATE_DIR (default: the
directory of KOL_DB, i.e. the persistent disk /var/data on Render)."""
from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from insiders.service import InsiderService

PAGE = Path(__file__).with_name("page")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}


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


def make_handler(svc: InsiderService, access_code: str | None, health: dict):
    class H(BaseHTTPRequestHandler):
        server_version = "insiders"

        def log_message(self, fmt, *args):                  # quiet access log (no headers, no codes)
            pass

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
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

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
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
                                    "rpc_throttled": getattr(v.rpc, "throttled", None)}
                    pf = getattr(w, "portfolio", None)
                    if pf is not None:
                        h["portfolio"] = {"updated": pf.updated, "seconds": pf.seconds,
                                          "wallets": len((pf.view or {}).get("wallets", [])), "progress": pf.progress,
                                          "error": pf.error, "rpc_calls": getattr(pf.rpc, "calls", None),
                                          "rpc_throttled": getattr(pf.rpc, "throttled", None)}
                    h["mode"] = getattr(w, "mode", None)
                    sh = getattr(w, "shadow", None)
                    if sh is not None:                        # counts only (no coins, no wallets)
                        sg = sh.s.get("signals", [])
                        h["shadow"] = {"signals": len(sg), "live": sum(x["status"] == "live" for x in sg),
                                       "waiting": sum(x["status"] == "wait_price" for x in sg),
                                       "paper_open": sum(1 for x in sg if (x.get("paper") or {}).get("status") == "open"),
                                       "ticks": sh.stat.get("ticks"), "error": sh.stat.get("error")}
                return self._json(200, h)
            if path in STATIC:
                name, ctype = STATIC[path]
                return self._send(200, ctype, (PAGE / name).read_bytes())
            if path.startswith("/api/") and not self._authed():
                return None
            if path == "/api/insiders":
                return self._json(200, svc.status())
            if path == "/api/insiders/chart":
                from urllib.parse import parse_qs, urlsplit
                mint = (parse_qs(urlsplit(self.path).query).get("mint") or [""])[0]
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
                    return self._json(400, {"error": "mint"})
                return self._json(200, svc.chart(mint))
            if path == "/api/insiders/watch":
                w = getattr(svc, "watcher", None)
                return self._json(200, {"watch": w.status() if w else None,
                                        "tickers": (svc.result or {}).get("tickers", {})})
            return self._json(404, {"error": "route"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            n = int(self.headers.get("content-length") or 0)
            if n:
                self.rfile.read(min(n, 65536))
            if not path.startswith("/api/") or not self._authed():
                return None if path.startswith("/api/") else self._json(404, {"error": "route"})
            if path == "/api/insiders/run":
                if not svc.key:
                    return self._json(503, {"error": "helius_key_missing"})
                return self._json(200, {"started": svc.start(), "running": svc.running})
            return self._json(404, {"error": "route"})
    return H


def serve(host: str = "127.0.0.1", port: int = 8780, watch: bool = True, svc: InsiderService | None = None):
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
    httpd = ThreadingHTTPServer((host, port), make_handler(svc, code, health))
    print(f"[insiders] http://{host}:{port} · state {sdir} (durable {durable}) · watch {bool(started)}", flush=True)
    return httpd


def run_forever(**kw):
    httpd = serve(**kw)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()

