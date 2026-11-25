"""Server side of the dashboard's Insiders page: serves the last scan (bundled result.json or the latest run) and
runs a new scan in a background thread with the server's HELIUS_API_KEY (never sent to the browser)."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from insiders import scan as S


def light(result: dict | None) -> dict | None:
    """The page needs per-token counts, not the 40 early wallets of every token (keeps the phone payload small)."""
    if not result:
        return result
    toks = [{**{k: v for k, v in t.items() if k != "buyers"}, "n_buyers": len(t.get("buyers") or []),
             "n_pre_call": sum(1 for b in t.get("buyers") or [] if b.get("pre_call"))} for t in result.get("tokens", [])]
    out = {**result, "tokens": toks}
    if result.get("pnl"):
        out["pnl"] = {k: v for k, v in result["pnl"].items() if k != "rows"}
    return out


class InsiderService:
    def __init__(self, result_path: Path = S.RESULT_JSON, key: str | None = None):
        self.result_path = result_path
        self.key = key if key is not None else os.environ.get("HELIUS_API_KEY")
        self.result = None
        self.running = False
        self.started_at = None
        self.error = None
        self.log = deque(maxlen=30)
        self.lock = threading.Lock()
        try:
            self.result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.result = None

    def status(self) -> dict:
        return {"running": self.running, "started_at": self.started_at, "error": self.error,
                "progress": list(self.log), "can_run": bool(self.key), "result": light(self.result),
                "watch": self.watcher.status() if getattr(self, "watcher", None) else None}

    def chart(self, mint: str) -> dict:
        """Candles for the early-signal board (GeckoTerminal; cached)."""
        if not getattr(self, "charts", None):
            from insiders.chart import Charts
            self.charts = Charts()
        w = getattr(self, "watcher", None)
        created = ((w.state.get("meta") or {}).get(mint) or {}).get("created_ts") if w else None
        return self.charts.candles(mint, created)

    def start_watch(self, state_path: Path) -> bool:
        """Start the 5-minute deployer watch in a daemon thread (public RPC; no key needed)."""
        if getattr(self, "watcher", None) or not self.result:
            return False
        from insiders.watch import Watcher, coin_meta, telegram_sender
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        levels = tuple(x for x in (os.environ.get("INSIDER_ALERT_LEVELS") or "hold,vip,vipstrong").split(",") if x)
        from insiders.paper import jupiter_quote
        from insiders import backup as B
        try:                                                  # B1: a verified copy of the state before it is loaded
            self.last_backup = B.backup(Path(state_path).parent, reason="start")
            self.backup_error = None
        except Exception as e:                                # never blocks the start; shown on /healthz
            self.last_backup, self.backup_error = None, f"{type(e).__name__}: {str(e)[:120]}"
        self.watcher = Watcher(self.result, state_path, notify=telegram_sender(tok, chat) if tok and chat else None,
                               notify_levels=levels,
                               paper_quote=jupiter_quote if (os.environ.get("INSIDER_PAPER") or "0") == "1" else None,   # off: trace only
                               meta=coin_meta)
        self.watch_stop = threading.Event()
        # INSIDER_MODE=vip (default): only the special wallets (45 holders + successors + the wallets they fund),
        # streamed live, plus their balances; =broad: the earlier 1 500-wallet polling as well
        self.watcher.mode = (os.environ.get("INSIDER_MODE") or "vip").lower()
        if self.watcher.mode == "broad":
            threading.Thread(target=self.watcher.run_forever, args=(self.watch_stop,), daemon=True).start()
        if (os.environ.get("INSIDER_VIP") or "1") != "0":     # real-time stream of the special wallets
            from insiders.portfolio import Portfolio
            from insiders.vip import VipStream
            from insiders.watch import Rpc
            # INSIDER_FOCUS: groups followed (default DEPTH: DEPTH + SNACKS, one team); ALL = every list
            self.watcher.vip = VipStream(self.watcher, self.result, focus=os.environ.get("INSIDER_FOCUS") or "DEPTH,STANK")
            self.watcher.vip.start(self.watch_stop)
            from insiders.pumpinfo import PumpInfo
            self.watcher.pump = PumpInfo()                    # dev / description / profile names for the page
            from insiders.devstats import DevStats
            self.watcher.devstats = DevStats(os.environ.get("HELIUS_API_KEY"), self.watcher.vip.rpc, self.watcher.pump,
                                             state=self.watcher.state)
            self.watcher.devstats.paused = self.watcher.vip.paused
            if (os.environ.get("INSIDER_TRACK") or "1") != "0":    # coin watchlist + funding graph + signal outcomes
                self.start_track(Path(state_path).parent / "insiders_track.db")
            threading.Thread(target=self.watcher.devstats.run_forever, args=(self.watch_stop,), daemon=True).start()
            # tracing only by default (2026-10-14): balances, shadow paper and the paper book are off unless asked for
            if (os.environ.get("INSIDER_SHADOW") or "0") == "1":     # live measurement of every signal (paper)
                from insiders.shadow import Shadow
                w = self.watcher

                def sold_half(mint, wallet):
                    x = ((w.state.get("coins") or {}).get(mint) or {}).get("wallets", {}).get(wallet) or {}
                    return bool(x.get("tok_in")) and x.get("tok_out", 0) >= x["tok_in"] / 2
                w.shadow = Shadow(w.state, sold_half=sold_half, meta=w._meta, lock=w.lock)
                threading.Thread(target=w.shadow.run_forever, args=(self.watch_stop, w.save), daemon=True).start()
            if (os.environ.get("INSIDER_PORTFOLIO") or "0") == "1":  # balances: many RPC calls, off by default
                self.watcher.portfolio = Portfolio(self.watcher.vip.wallets, Rpc(gap_s=0.25))
                threading.Thread(target=self.watcher.portfolio.run_forever, args=(self.watch_stop,), daemon=True).start()
        return True

    def start_track(self, db_path: Path, rpc=None, stop: threading.Event | None = None):
        """The tracking worker (track.Tracker) next to the stream: read-only, one Helius budget, pauses with it."""
        from insiders import track as T
        from insiders.watch import coin_meta
        w = getattr(self, "watcher", None)
        vip = getattr(w, "vip", None) if w else None
        if rpc is None:
            from insiders.vip import _rpc
            rpc = _rpc(0.1)

        def known():
            if vip is None:
                return {}
            return {**{a: 1 for a in vip.base}, **{a: 1 for a in (vip.state.get("children") or {})}}
        self.tracker = T.Tracker(T.Store(db_path), rpc, known=known, group=(vip.group if vip else (lambda a: None)),
                                 meta=coin_meta, paused=(vip.paused if vip else (lambda: False)))
        if vip is not None:
            vip.track = self.tracker
            self.tracker.sol_usd = lambda: vip.sol_usd

            def facts(mint):
                meta = ((w.state.get("meta") or {}).get(mint) or {})
                sup = ((w.state.get("supply") or {}).get(mint) or {})
                return {"symbol": meta.get("symbol"), "name": meta.get("name"),
                        "supply_ui": sup.get("ui"), "decimals": sup.get("decimals")}
            self.tracker.coin_facts = facts
        stop = stop or getattr(self, "watch_stop", None) or threading.Event()
        threading.Thread(target=self.tracker.run_forever, args=(stop,), kwargs={"vip": vip}, daemon=True).start()
        if vip is not None:                                   # the stream's getTransaction calls are metered too
            raw, store = vip.rpc, self.tracker.s

            def metered(method, params):
                try:
                    res = raw(method, params)
                except Exception:
                    store.usage("stream", method, None, err=True)
                    raise
                store.usage("stream", method, res)
                return res
            vip.rpc = metered
        # B3/B4: wallet history + PnL per cohort. The cohorts are written locally (no RPC); the worker that reads
        # the chain runs only with INSIDER_WALLET_HISTORY=1 (default off), on its own budget and daily / monthly caps
        from insiders import wallets as WH
        self.wallet_history = WH.WalletHistory(self.tracker.s, rpc, paused=(vip.paused if vip else (lambda: False)))
        self.wallet_history.seed(self.result or {})
        if self.wallet_history.enabled:
            threading.Thread(target=self.wallet_history.run_forever, args=(stop,), daemon=True).start()
        return self.tracker

    def start(self, runner=None) -> bool:
        """Start a scan unless one is running. Returns False when it cannot start."""
        with self.lock:
            if self.running or not self.key:
                return False
            self.running, self.started_at, self.error = True, time.time(), None
            self.log.clear()
        threading.Thread(target=self._run, args=(runner,), daemon=True).start()
        return True

    def _run(self, runner) -> None:
        try:
            if runner is not None:
                res = runner(self.log.append)
            else:
                chain = S.Chain(self.key, S.default_cache())
                res = S.run(chain, S.load_calls(), progress=self.log.append)
            if self.result and self.result.get("deep") and not res.get("deep"):
                res["deep"] = {**self.result["deep"], "from_earlier_scan": True}   # deep trace runs offline
            if self.result and self.result.get("pnl") and not res.get("pnl"):
                res["pnl"] = {**self.result["pnl"], "from_earlier_scan": True}
            self.result = res
            try:
                Path(self.result_path).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass                                         # read-only deploy: kept in memory
        except Exception as e:                               # type + message only (no URL / key)
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            self.running = False
