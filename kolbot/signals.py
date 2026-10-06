"""Buy signals: a wallet in a Top 10 buys a token -> a signal is recorded and shown for the user to decide.
NOTHING IS BOUGHT. The paper bot, its copy rules and the KOL methodology are untouched; Smart Wallet data stays a
research input and never reaches the paper bot or Direction C.

Top 10 lists (refreshed every minute, same rules as the dashboard tables):
  kol    Top KOL by Win Rate (wins / resolved paper trades, >= 100 resolved, ties: trades, wins)
  smart  Smart Wallet ranking (>= 100 resolved own round trips, flagged wallets hidden, ties: trades, wins)
A Top 10 wallet only sources signals if its status is not REJECT and its average hold is >= MIN_AVG_HOLD_S
(KOL: KOL buy -> paper exit, which follows the KOL's sell; smart: the wallet's own round trips). Excluded wallets
are shown with the reason and are NOT replaced by #11 and below: "Top 10" keeps the dashboard's meaning.
A signal fires on a buy >= SIGNAL_MIN_SOL by a listed wallet; one signal per (wallet, token) per DEDUP_S. Each
signal is a self-contained, persisted record (what was known at that moment) so that a future executor could
consume it; its outcome (market cap after 5 min / 30 min / 2 h and the peak) is filled in automatically so the
signals can be judged on data before anyone considers automation.

Automatic trading is NOT implemented: AUTO_TRADE must stay off; the process refuses to start if it is set."""
from __future__ import annotations

import json
import os
import time

SIGNAL_MIN_SOL = 0.05
MIN_AVG_HOLD_S = 60          # user decision 2026-10-06: a source must hold >= 60 s on average (copyable by a person)
EXCLUDED_STATUSES = ("REJECT",)   # user decision 2026-10-06: a REJECT wallet never sources a buy signal
SIGNAL_MIN_N = 100
TOP_N = 10                   # displayed signals + notifications
EXT_N = 20                   # paper books may follow up to the Top 20 (user request 2026-10-06)
DEDUP_S = 1800
REFRESH_S = 60
HORIZONS = (("mc_5m", 300), ("mc_30m", 1800), ("mc_2h", 7200))
WATCH_S = 7200
LAMPORTS = 1_000_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, uid TEXT UNIQUE, ts REAL, detected_at REAL,
    source TEXT, wallet TEXT, name TEXT, rank INTEGER, n INTEGER, wins INTEGER, win_rate_pct REAL,
    win_rate_low_pct REAL, status TEXT, pnl_sol REAL, mint TEXT, buy_sol REAL, mc_sol REAL, price_sol REAL,
    creator TEXT, dev_label TEXT, dev_risk TEXT, confluence INTEGER DEFAULT 1, mc_5m REAL, mc_30m REAL,
    mc_2h REAL, peak_mc_sol REAL, migrated INTEGER DEFAULT 0, state TEXT DEFAULT 'new', state_ts REAL,
    snapshot TEXT, auto_eligible INTEGER DEFAULT 0, executed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_signals_mint ON signals(mint, ts);
"""
STATES = ("new", "seen", "dismissed")


def auto_trade_requested() -> bool:
    return os.environ.get("AUTO_TRADE", "").strip().lower() in ("1", "true", "yes", "on")


def refuse_auto_trade() -> None:
    """Called at startup: automatic trading does not exist in this build and must not be switched on."""
    if auto_trade_requested():
        raise SystemExit("AUTO_TRADE is set but automatic trading is not implemented (signals are display-only). "
                         "Unset AUTO_TRADE to start.")


class SignalEngine:
    def __init__(self, db, roster: dict, clock=time.time, log=print):
        self.db, self.roster, self.clock, self.log = db, roster, clock, log
        db.executescript(SCHEMA)
        db.commit()
        self.top: dict[str, dict] = {}                     # wallet -> signal source snapshot
        self.excluded: dict[str, dict] = {}                # Top 20 wallets that do not source signals, with reason
        self.sources: dict[str, dict] = {}                 # passing wallets ranked <= EXT_N (paper books Top 5/20)
        self.last_refresh = 0.0
        self.watch: dict[str, dict] = {}                   # mint -> {"sigs": [(id, ts)], "peak": mc, "last": mc}
        for sid, mint, ts, peak in db.execute(
                "SELECT id, mint, ts, peak_mc_sol FROM signals WHERE ts >= ? AND migrated = 0",
                (clock() - WATCH_S,)):
            w = self.watch.setdefault(mint, {"sigs": [], "peak": peak or 0.0, "last": None})
            w["sigs"].append((sid, ts))

    # --- Top 10 lists -----------------------------------------------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last_refresh < REFRESH_S:
            return
        self.last_refresh = now
        from kolbot import api, smart_api
        cands = []
        kol_rows = api.winrate_table(self.db, self.roster, min_n=SIGNAL_MIN_N, size=EXT_N)["rows"][:EXT_N]
        holds = {}
        if kol_rows:
            ks = [r["kol"] for r in kol_rows]
            holds = dict(self.db.execute(f"SELECT kol, AVG(exit_ts - trigger_ts) FROM trades WHERE gap = 0 AND kol IN "
                                         f"({','.join('?' * len(ks))}) GROUP BY kol", ks).fetchall())
        for r in kol_rows:
            cands.append((r["kol"], {"source": "kol", "rank": r["wr_rank"], "name": r["name"], "n": r["n"],
                                     "wins": r["wins"], "win_rate_pct": r["win_rate_pct"],
                                     "win_rate_low_pct": r["win_rate_low_pct"], "status": r["status"],
                                     "pnl_sol": r["pnl_sol"], "avg_hold_s": holds.get(r["kol"])}))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='sw_wallets'").fetchone():
            for r in smart_api.smart_table(self.db, min_n=SIGNAL_MIN_N, size=EXT_N)["rows"][:EXT_N]:
                cands.append((r["wallet"], {"source": "smart", "rank": r["rank"], "name": None, "n": r["n"],
                                            "wins": r["wins"], "win_rate_pct": r["win_rate_pct"],
                                            "win_rate_low_pct": r["win_rate_low_pct"], "status": r["status"],
                                            "pnl_sol": r["pnl_sol"], "avg_hold_s": r["avg_hold_s"]}))
        top, excluded = {}, {}
        for w, info in cands:
            if w in top or w in excluded:                   # a KOL never doubles as a smart wallet (roster excluded)
                continue
            why = []
            if info["status"] in EXCLUDED_STATUSES:
                why.append(f"trạng thái {info['status']}")
            h = info["avg_hold_s"]
            if h is None or h < MIN_AVG_HOLD_S:
                why.append(f"giữ trung bình {h:.1f}s < {MIN_AVG_HOLD_S}s" if h is not None else "chưa biết thời gian giữ")
            if why:
                excluded[w] = dict(info, reason="; ".join(why))
            else:
                top[w] = info
        # sources: every passing wallet ranked <= EXT_N in its list; displayed signals only for rank <= TOP_N
        self.sources = top
        self.top = {w: i for w, i in top.items() if i["rank"] <= TOP_N}
        self.excluded = excluded

    # --- stream ---------------------------------------------------------------------------------------------------
    def on_event(self, ev: dict) -> dict | None:
        if ev["kind"] == "complete" and ev["mint"] in self.watch:
            self.db.execute("UPDATE signals SET migrated = 1 WHERE mint = ? AND ts >= ?",
                            (ev["mint"], ev["ts"] - WATCH_S))
            self.watch.pop(ev["mint"], None)
            return None
        if ev["kind"] != "trade":
            return None
        mc = ev["vsol"] / ev["vtok"] * 1e6
        w = self.watch.get(ev["mint"])
        if w:
            w["last"] = mc
            w["peak"] = max(w["peak"], mc)
            self._outcomes(ev["mint"], w, ev["ts"], mc)
        info = self.top.get(ev["user"])
        if not info or not ev["is_buy"] or ev["sol"] < SIGNAL_MIN_SOL * LAMPORTS:
            return None
        return self._fire(ev, info, mc)

    def source_of(self, ev: dict) -> dict | None:
        """A buy >= SIGNAL_MIN_SOL by a passing wallet ranked <= EXT_N in its list (for the Top N paper books)."""
        if ev["kind"] != "trade" or not ev["is_buy"] or ev["sol"] < SIGNAL_MIN_SOL * LAMPORTS:
            return None
        return self.sources.get(ev["user"])

    def _fire(self, ev: dict, info: dict, mc: float) -> dict | None:
        bucket = int(ev["ts"] // DEDUP_S)
        uid = f"{ev['user']}:{ev['mint']}:{bucket}"
        dup = self.db.execute("SELECT 1 FROM signals WHERE wallet=? AND mint=? AND ts > ?",
                              (ev["user"], ev["mint"], ev["ts"] - DEDUP_S)).fetchone()
        if dup:
            return None
        dev = self._dev(ev["mint"], ev.get("creator"))
        others = self.db.execute("SELECT COUNT(DISTINCT wallet) FROM signals WHERE mint=? AND ts > ?",
                                 (ev["mint"], ev["ts"] - DEDUP_S)).fetchone()[0]
        sig = {"uid": uid, "ts": ev["ts"], "detected_at": self.clock(), "source": info["source"],
               "wallet": ev["user"], "name": info["name"], "rank": info["rank"], "n": info["n"], "wins": info["wins"],
               "win_rate_pct": info["win_rate_pct"], "win_rate_low_pct": info["win_rate_low_pct"],
               "status": info["status"], "pnl_sol": info["pnl_sol"], "mint": ev["mint"],
               "buy_sol": ev["sol"] / LAMPORTS, "mc_sol": mc, "price_sol": mc / 1e9, "creator": dev["creator"],
               "dev_label": dev["label"], "dev_risk": dev["risk"], "confluence": others + 1, "peak_mc_sol": mc,
               "auto_eligible": 0}
        sig["snapshot"] = json.dumps({k: sig[k] for k in sig if k != "snapshot"}, ensure_ascii=False)
        cols = list(sig)
        cur = self.db.execute(f"INSERT OR IGNORE INTO signals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                              [sig[c] for c in cols])
        if cur.rowcount == 0:
            return None
        sig["id"] = cur.lastrowid
        if others:                                           # earlier signals on the same token gain confluence
            self.db.execute("UPDATE signals SET confluence = ? WHERE mint = ? AND ts > ?",
                            (others + 1, ev["mint"], ev["ts"] - DEDUP_S))
        self.db.commit()
        wt = self.watch.setdefault(ev["mint"], {"sigs": [], "peak": mc, "last": mc})
        wt["sigs"].append((sig["id"], ev["ts"]))
        tag = "KOL" if info["source"] == "kol" else "SMART"
        try:                                                 # a console that cannot print a name must not stop us
            self.log(f"[signal] {tag} #{info['rank']} {(info['name'] or ev['user'][:6])} bought "
                     f"{sig['buy_sol']:.2f} SOL of {ev['mint'][:8]}.. (display only, nothing bought)")
        except Exception:
            pass
        return sig

    def _dev(self, mint: str, creator: str | None) -> dict:
        try:
            from kolbot.api import _token_devs
            d = _token_devs(self.db, [mint]).get(mint)
        except Exception:
            d = None
        if d and d.get("creator"):
            return {"creator": d["creator"], "label": d["label"], "risk": d["risk"]}
        if creator:
            from kolbot.devs import dev_badge
            b = dev_badge(self.db, creator) if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='devs'").fetchone() else None
            return {"creator": creator, "label": (b or {}).get("label", "NEW DEV"),
                    "risk": (b or {}).get("risk", "UNKNOWN")}
        return {"creator": None, "label": None, "risk": None}

    def _outcomes(self, mint: str, w: dict, ts: float, mc: float) -> None:
        keep = []
        for sid, t0 in w["sigs"]:
            age = ts - t0
            sets = [f"{col} = COALESCE({col}, ?)" for col, h in HORIZONS if age >= h]
            args = [mc for col, h in HORIZONS if age >= h]
            self.db.execute(f"UPDATE signals SET peak_mc_sol = MAX(COALESCE(peak_mc_sol, 0), ?)"
                            f"{''.join(', ' + s for s in sets)} WHERE id = ?", (mc, *args, sid))
            if age < WATCH_S:
                keep.append((sid, t0))
        w["sigs"] = keep
        if not keep:
            self.watch.pop(mint, None)

    def tick(self) -> None:
        self.refresh()
        self.settle(self.clock())
        self.db.commit()

    def settle(self, now: float) -> None:
        """Horizons reached without any trade on the token: the price did not move, so the horizon takes the last
        known market cap (else dead tokens would never get an outcome and the stats would only see survivors)."""
        for col, h in HORIZONS:
            self.db.execute(f"UPDATE signals SET {col} = COALESCE((SELECT k.last_mc_sol FROM tokens k WHERE "
                            f"k.mint = signals.mint AND k.last_trade_ts <= signals.ts + {h}), mc_sol) "
                            f"WHERE {col} IS NULL AND migrated = 0 AND ts <= ?", (now - h,)) \
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='tokens'").fetchone() else \
                self.db.execute(f"UPDATE signals SET {col} = mc_sol WHERE {col} IS NULL AND migrated = 0 AND ts <= ?",
                                (now - h,))
        for m in [m for m, w in self.watch.items() if all(now - t0 > WATCH_S + 600 for _, t0 in w["sigs"])]:
            self.watch.pop(m, None)


# --- read API ---------------------------------------------------------------------------------------------------------
COLS = ("id", "uid", "ts", "detected_at", "source", "wallet", "name", "rank", "n", "wins", "win_rate_pct",
        "win_rate_low_pct", "status", "pnl_sol", "mint", "buy_sol", "mc_sol", "price_sol", "creator", "dev_label",
        "dev_risk", "confluence", "mc_5m", "mc_30m", "mc_2h", "peak_mc_sol", "migrated", "state", "state_ts",
        "auto_eligible", "executed")


def list_signals(db, source: str = "", state: str = "", since_id: int = 0, page: int = 1, size: int = 30,
                 symbols: dict | None = None, now_mc: dict | None = None) -> dict:
    from kolbot.api import _page
    where, args = ["id > ?"], [int(since_id)]
    if source in ("kol", "smart"):
        where.append("source = ?")
        args.append(source)
    if state in STATES:
        where.append("state = ?")
        args.append(state)
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM signals WHERE {w}", args).fetchone()[0]
    page, pages, size = _page(total, page, size)
    rows = [dict(zip(COLS, r)) for r in db.execute(
        f"SELECT {','.join(COLS)} FROM signals WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, size, (page - 1) * size))]
    symbols, now_mc = symbols or {}, now_mc or {}
    for r in rows:
        r["symbol"] = symbols.get(r["mint"])
        r["mc_now_sol"] = now_mc.get(r["mint"])
        r["token_url"] = f"https://pump.fun/coin/{r['mint']}"
        r["wallet_url"] = f"https://solscan.io/account/{r['wallet']}"
    return {"total": total, "page": page, "pages": pages, "size": size, "rows": rows,
            "new": db.execute("SELECT COUNT(*) FROM signals WHERE state = 'new'").fetchone()[0],
            "max_id": db.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()[0]}


def set_state(db, sid: int, state: str, now: float | None = None) -> bool:
    if state not in STATES:
        return False
    cur = db.execute("UPDATE signals SET state = ?, state_ts = ? WHERE id = ?", (state, now or time.time(), int(sid)))
    db.commit()
    return cur.rowcount == 1


def outcome_stats(db) -> dict:
    """How signals did so far (market cap change after each horizon), per source. Shown so the signals are judged
    on data; nothing here is a recommendation."""
    out = {}
    for src in ("kol", "smart"):
        s = {"n": db.execute("SELECT COUNT(*) FROM signals WHERE source=?", (src,)).fetchone()[0]}
        for col, _ in HORIZONS:
            vals = [100 * (b / a - 1) for a, b in db.execute(
                f"SELECT mc_sol, {col} FROM signals WHERE source=? AND {col} IS NOT NULL AND mc_sol > 0", (src,))]
            vals.sort()
            s[col] = {"n": len(vals), "median_pct": round(vals[len(vals) // 2], 1) if vals else None,
                      "up_share_pct": round(100 * sum(v > 0 for v in vals) / len(vals), 1) if vals else None}
        out[src] = s
    return out


def top_lists(engine: SignalEngine) -> dict:
    def side(src):
        rows = [dict(v, wallet=k, active=True) for k, v in engine.sources.items() if v["source"] == src]
        rows += [dict(v, wallet=k, active=False) for k, v in engine.excluded.items() if v["source"] == src]
        return sorted(rows, key=lambda r: r["rank"])
    return {"kol": side("kol"), "smart": side("smart"), "min_n": SIGNAL_MIN_N, "min_avg_hold_s": MIN_AVG_HOLD_S,
            "top_n": TOP_N, "ext_n": EXT_N,
            "excluded_statuses": list(EXCLUDED_STATUSES), "refreshed_at": engine.last_refresh}
