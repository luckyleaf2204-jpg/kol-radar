"""Read-only data for the six pages (Live Signals, Elite Callers, Call History, Early Coins, Backtest, Watchlist).
Every gain shown is the TRADABLE one unless the field says theoretical."""
from __future__ import annotations

import json
import statistics
import time

from callers import config as C


def _j(s, d=None):
    try:
        return json.loads(s) if s else d
    except ValueError:
        return d


def overview(store, hunter, x) -> dict:
    tiers = {r["tier"]: r["n"] for r in store.q("SELECT tier, COUNT(*) n FROM callers GROUP BY tier")}
    labels = {r["label"]: r["n"] for r in store.q("SELECT label, COUNT(*) n FROM signals GROUP BY label")}
    oc = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM outcomes WHERE method=? GROUP BY status",
                                               (C.METHOD_VERSION,))}
    return {"method": C.METHOD_VERSION, "tiers": tiers, "signals": labels, "outcomes": oc,
            "calls": store.q("SELECT COUNT(*) n FROM calls")[0]["n"],
            "pending_eval": store.q("SELECT COUNT(*) n FROM calls c LEFT JOIN outcomes o ON o.call_id=c.id AND "
                                    "o.method=? WHERE o.call_id IS NULL", (C.METHOD_VERSION,))[0]["n"],
            "x": x.status() if x else {"enabled": False}, "runner": hunter.stat if hunter else None,
            "rules": {"detect_latency_s": C.DETECT_LATENCY_S, "trade_usd": C.TRADE_USD,
                      "min_ranked_calls": C.MIN_RANKED_CALLS, "win": "tradable gain >= +100 % within 7 d"},
            "disclaimer": "Research only. Scores rank setups; they are not probabilities and not financial advice."}


def signals(store, limit: int = 100) -> list[dict]:
    rows = store.q("SELECT s.*, c.post_id, c.post_time, c.ticker, c.source, k.tier, k.score AS caller_score, p.url "
                   "FROM signals s JOIN calls c ON c.id=s.call_id LEFT JOIN callers k ON k.username=s.username "
                   "LEFT JOIN posts p ON p.post_id=c.post_id ORDER BY s.created_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["components"] = _j(r["components"], {})
        r["snapshot"] = _j(r["snapshot"], {})
    return rows


def leaderboard(store, tier: str | None = None) -> dict:
    rows = store.q("SELECT username, platform, tier, score, metrics, followers, source, watched FROM callers" +
                   (" WHERE tier=?" if tier else ""), (tier,) if tier else ())
    out = []
    for r in rows:
        m = _j(r.pop("metrics"), {}) or {}
        out.append({**r, **{k: m.get(k) for k in ("calls", "verified", "wins_2x", "wins_5x", "wins_10x", "win_rate",
                                                  "win_rate_low95", "median_gain", "mean_gain", "early_share",
                                                  "late_share", "rug_rate", "avg_time_to_peak_s")},
                    "ranked": (m.get("verified") or 0) >= C.MIN_RANKED_CALLS})
    out.sort(key=lambda r: (not r["ranked"], -(r["score"] or 0), -(r["verified"] or 0)))
    rank = 0
    for r in out:
        if r["ranked"]:
            rank += 1
            r["rank"] = rank
    return {"callers": out, "min_ranked_calls": C.MIN_RANKED_CALLS}


def caller_detail(store, username: str) -> dict | None:
    c = store.q("SELECT * FROM callers WHERE username=?", (username,))
    if not c:
        return None
    c = c[0]
    c["metrics"] = _j(c["metrics"], {})
    calls = store.q("SELECT c.*, o.data AS o_data, p.url, p.text FROM calls c LEFT JOIN outcomes o ON o.call_id=c.id "
                    "AND o.method=? LEFT JOIN posts p ON p.post_id=c.post_id WHERE c.username=? ORDER BY c.post_time",
                    (C.METHOD_VERSION, username))
    hist = []
    for r in calls:
        o = _j(r.pop("o_data"), {}) or {}
        hist.append({"post_time": r["post_time"], "mint": r["mint"], "ticker": r["ticker"], "url": r["url"],
                     "text": (r["text"] or "")[:200], "status": o.get("status", "PENDING"), "cls": o.get("cls"),
                     "entry_raw": o.get("entry_raw"), "best_tradable_gain": o.get("best_tradable_gain"),
                     "best_theoretical_gain": o.get("best_theoretical_gain"), "runup_1h": o.get("runup_1h"),
                     "rug": o.get("rug"), "final": o.get("final"),
                     "horizons": {k: {kk: v.get(kk) for kk in ("tradable_gain", "theoretical_gain", "return_at",
                                                                "time_to_peak_s", "status")}
                                  for k, v in (o.get("horizons") or {}).items()},
                     "liq_source": o.get("liq_source"), "resolve_note": r["resolve_note"], "source": r["source"]})
    return {"caller": c, "calls": hist}


def early_coins(store, limit: int = 100) -> list[dict]:
    rows = store.q("SELECT s.mint, MAX(s.score) score, MIN(c.post_time) first_call, COUNT(DISTINCT s.username) callers, "
                   "k.symbol, k.snapshot, k.safety FROM signals s JOIN calls c ON c.id=s.call_id LEFT JOIN coins k "
                   "ON k.mint=s.mint GROUP BY s.mint ORDER BY score DESC LIMIT ?", (limit,))
    for r in rows:
        r["snapshot"], r["safety"] = _j(r["snapshot"], {}), _j(r["safety"], {})
    return rows


def backtest(store, horizon: str = "24h", group: str = "week") -> dict:
    h = horizon if horizon in C.HORIZONS_S else "24h"
    rows = store.outcomes(C.METHOD_VERSION)
    ok = [r for r in rows if r["o"].get("status") == "OK"]
    fmt = {"day": "%Y-%m-%d", "week": "%G-W%V", "month": "%Y-%m"}.get(group, "%G-W%V")

    def hz(r):
        return (r["o"].get("horizons") or {}).get(h) or {}
    by_period: dict[str, list] = {}
    for r in ok:
        by_period.setdefault(time.strftime(fmt, time.gmtime(r["post_time"])), []).append(r)
    periods = [{"period": k, "calls": len(v), "wins_2x": sum(1 for r in v if (hz(r).get("tradable_gain") or -1) >= 1),
                "win_rate": sum(1 for r in v if (hz(r).get("tradable_gain") or -1) >= 1) / len(v),
                "mean_return_at": _mean([hz(r).get("return_at") for r in v])} for k, v in sorted(by_period.items())]
    per_h = {}
    for name in C.HORIZONS_S:
        vals = [((r["o"].get("horizons") or {}).get(name) or {}) for r in ok]
        tg = [v.get("tradable_gain") for v in vals if v.get("tradable_gain") is not None]
        per_h[name] = {"n": len(tg), "share_2x": sum(1 for g in tg if g >= 1) / len(tg) if tg else None,
                       "median_tradable_gain": statistics.median(tg) if tg else None,
                       "mean_return_at": _mean([v.get("return_at") for v in vals]),
                       "median_theoretical_gain": statistics.median([v["theoretical_gain"] for v in vals
                                                                     if v.get("theoretical_gain") is not None])
                       if any(v.get("theoretical_gain") is not None for v in vals) else None}
    by_caller: dict[str, list] = {}
    for r in ok:
        by_caller.setdefault(r["username"], []).append(r)
    callers = []
    for u, v in by_caller.items():
        ru = [r["o"].get("runup_1h") for r in v if r["o"].get("runup_1h") is not None]
        callers.append({"username": u, "calls": len(v),
                        "win_rate": sum(1 for r in v if (hz(r).get("tradable_gain") or -1) >= 1) / len(v),
                        "mean_return_at": _mean([hz(r).get("return_at") for r in v]),
                        "late_share": sum(1 for x in ru if x >= C.LATE_RUNUP_1H) / len(ru) if ru else None})
    coins = sorted(({"mint": r["mint"], "username": r["username"], "post_time": r["post_time"],
                     "tradable_gain": hz(r).get("tradable_gain"), "theoretical_gain": hz(r).get("theoretical_gain")}
                    for r in ok if hz(r).get("tradable_gain") is not None), key=lambda x: -x["tradable_gain"])[:30]
    return {"horizon": h, "group": group, "periods": periods, "per_horizon": per_h,
            "best_callers": sorted(callers, key=lambda x: -x["win_rate"])[:30],
            "late_callers": sorted((c for c in callers if c["late_share"] is not None),
                                   key=lambda x: -x["late_share"])[:30], "top_coins": coins,
            "verified_calls": len(ok), "all_calls": len(rows),
            "note": "tradable gains after a 60 s detection delay, fees, slippage and liquidity impact; theoretical = "
                    "chart high"}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def watchlist(store) -> dict:
    items = store.q("SELECT * FROM watchlist ORDER BY added_at DESC")
    coins = [i for i in items if i["kind"] == "coin"]
    callers = [i for i in items if i["kind"] == "caller"]
    for c in coins:
        k = store.q("SELECT symbol, snapshot FROM coins WHERE mint=?", (c["key"],))
        c["symbol"] = k[0]["symbol"] if k else None
        c["snapshot"] = _j(k[0]["snapshot"], {}) if k else {}
        c["calls"] = store.q("SELECT username, post_time FROM calls WHERE mint=? ORDER BY post_time DESC LIMIT 10",
                             (c["key"],))
    for c in callers:
        k = store.q("SELECT tier, score FROM callers WHERE username=?", (c["key"],))
        c.update(k[0] if k else {"tier": None, "score": None})
        c["recent"] = store.q("SELECT mint, post_time FROM calls WHERE username=? ORDER BY post_time DESC LIMIT 10",
                              (c["key"],))
    return {"coins": coins, "callers": callers}
