"""HTTP glue for the Smart Caller Hunter inside the Insiders site server (same access code). GET routes are
read-only; the only writes are the user's own watchlist / caller list / manual imports in callers.db."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs

from callers import report
from callers.extract import is_mint
from callers.market import Market
from callers.service import CallerHunter
from callers.store import Store
from callers.xapi import XClient

PAGE = Path(__file__).with_name("page")
STATIC = {"/callers/": ("index.html", "text/html; charset=utf-8"),
          "/callers/index.html": ("index.html", "text/html; charset=utf-8"),
          "/callers/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/callers/styles.css": ("styles.css", "text/css; charset=utf-8")}


class CallersApp:
    def __init__(self, state_dir: Path, start: bool = True, market=None, x=None, smart_money=None, notify=None):
        self.store = Store(Path(state_dir) / "callers.db")
        self.market = market or Market()
        self.x = x if x is not None else XClient(self.store)
        self.hunter = CallerHunter(self.store, self.market, self.x, smart_money=smart_money, notify=notify)
        seed = Path(__file__).resolve().parents[1] / "insiders" / "sol_pump_insiders_calls.csv"
        if not self.store.kv("seeded_channel") and seed.exists():
            try:
                self.store.kv("seeded_channel", self.hunter.import_channel_csv(seed))
            except Exception as e:
                self.store.kv("seeded_channel_error", type(e).__name__)
        self.thread = None
        if start and (os.environ.get("CALLERS") or "1") != "0":
            self.thread = threading.Thread(target=self.hunter.run_forever, daemon=True)
            self.thread.start()

    def health(self) -> dict:
        x = self.x.status()
        return {"x_enabled": x["enabled"], "x_spent_est_usd": x["spent_est_usd"], "ticks": self.hunter.stat["ticks"],
                "evaluated": self.hunter.stat["evaluated"], "error": self.hunter.stat["error"]}

    # --- routes --------------------------------------------------------------------------------------------------------
    def get(self, path: str, query: str):
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path == "/api/callers/overview":
            return 200, report.overview(self.store, self.hunter, self.x)
        if path == "/api/callers/signals":
            return 200, report.signals(self.store, int(q.get("limit") or 100))
        if path == "/api/callers/leaderboard":
            return 200, report.leaderboard(self.store, q.get("tier") or None)
        if path == "/api/callers/caller":
            d = report.caller_detail(self.store, q.get("u", ""))
            return (200, d) if d else (404, {"error": "caller"})
        if path == "/api/callers/coins":
            return 200, report.early_coins(self.store)
        if path == "/api/callers/backtest":
            return 200, report.backtest(self.store, q.get("h") or "24h", q.get("group") or "week")
        if path == "/api/callers/watchlist":
            return 200, report.watchlist(self.store)
        return None

    def post(self, path: str, query: str, body: bytes):
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path == "/api/callers/watchlist":
            kind, key, op = q.get("kind"), (q.get("key") or "").strip().lstrip("@"), q.get("op", "add")
            if kind not in ("coin", "caller") or not key or (kind == "coin" and not is_mint(key)) or len(key) > 64:
                return 400, {"error": "kind=coin|caller and a valid key"}
            if op == "remove":
                self.store.x("DELETE FROM watchlist WHERE kind=? AND key=?", (kind, key))
            else:
                self.store.x("INSERT OR IGNORE INTO watchlist VALUES (?,?,?,strftime('%s','now'))",
                             (kind, key, (q.get("note") or "")[:200]))
                if kind == "caller":
                    self.store.add_caller(key, source="watchlist", watched=1)
                    self.store.x("UPDATE callers SET watched=1 WHERE username=?", (key,))
            return 200, report.watchlist(self.store)
        if path == "/api/callers/import":
            text = body.decode("utf-8", "replace")[:200_000]
            try:
                text = json.loads(text).get("text", "")
            except (ValueError, AttributeError):
                pass
            return 200, self.hunter.import_text(text)
        return None
