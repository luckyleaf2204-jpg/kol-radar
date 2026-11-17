"""Caller score 0-100 + tier, and the coin Signal Score 0-100 (docs/callers_method.md). Follower counts are never an
input. A score ranks; it is NOT a probability that a coin goes up."""
from __future__ import annotations

import math
import statistics
import time

from callers import config as C


def wilson_low(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def caller_metrics(rows: list[dict]) -> dict:
    """rows: calls joined with their outcome (row['o']). Only calls with a price (status OK) count as verified."""
    ok = [r for r in rows if r["o"].get("status") == "OK"]
    n = len(ok)
    gains = [r["o"].get("best_tradable_gain") for r in ok if r["o"].get("best_tradable_gain") is not None]
    wins = [g for g in gains if g >= 1.0]
    runups = [r["o"].get("runup_1h") for r in ok if r["o"].get("runup_1h") is not None]
    r24 = [r["o"]["horizons"]["24h"]["return_at"] for r in ok
           if (r["o"].get("horizons") or {}).get("24h", {}).get("return_at") is not None]
    weeks: dict[str, bool] = {}
    for r in ok:
        wk = time.strftime("%G-W%V", time.gmtime(r["post_time"]))
        weeks[wk] = weeks.get(wk, False) or (r["o"].get("best_tradable_gain") or -1) >= 1.0
    m = {"calls": len(rows), "verified": n, "unverified": len(rows) - n, "wins_2x": len(wins),
         "wins_5x": sum(1 for g in gains if g >= 4.0), "wins_10x": sum(1 for g in gains if g >= 9.0),
         "win_rate": len(wins) / n if n else None, "win_rate_low95": wilson_low(len(wins), n),
         "median_gain": statistics.median(gains) if gains else None,
         "mean_gain": statistics.fmean(gains) if gains else None,
         "early_share": sum(1 for x in runups if x <= C.EARLY_RUNUP_1H) / len(runups) if runups else None,
         "late_share": sum(1 for x in runups if x >= C.LATE_RUNUP_1H) / len(runups) if runups else None,
         "rug_rate": sum(1 for r in ok if r["o"].get("rug")) / n if n else None,
         "weeks_active": len(weeks), "weeks_with_win": sum(weeks.values()),
         "mean_return_24h": statistics.fmean(r24) if r24 else None,
         "std_return_24h": statistics.pstdev(r24) if len(r24) > 1 else None,
         "avg_time_to_peak_s": statistics.fmean([r["o"]["horizons"]["7d"]["time_to_peak_s"] for r in ok
                                                 if (r["o"].get("horizons") or {}).get("7d", {}).get("time_to_peak_s")
                                                 is not None]) if ok else None}
    comp = {"win_rate": clamp(m["win_rate_low95"] / 0.5),
            "early": m["early_share"] if m["early_share"] is not None else 0.0,
            "avg_return": clamp(math.log1p(max(m["median_gain"] or 0, 0)) / math.log1p(9)),
            "consistency": m["weeks_with_win"] / m["weeks_active"] if m["weeks_active"] else 0.0,
            "rug": 1 - (m["rug_rate"] or 0) if n else 0.0,
            "risk_adjusted": clamp(((m["mean_return_24h"] or 0) / m["std_return_24h"] + 0.5) / 1.5)
            if m["std_return_24h"] else 0.0}
    m["components"] = comp
    m["score"] = round(sum(C.SCORE_WEIGHTS[k] * v for k, v in comp.items()), 1) if n else 0.0
    m["tier"] = tier(m)
    return m


def tier(m: dict) -> str:
    n = m["verified"]
    if n < 3:
        return "NEW"
    if n < C.MIN_RANKED_CALLS:
        return "CANDIDATE"
    if m["score"] < C.REJECT_SCORE or (m["rug_rate"] or 0) > C.REJECT_RUG_RATE or \
            (m["late_share"] or 0) > C.REJECT_LATE_RATE:
        return "REJECTED"
    for name in ("ELITE", "QUALIFIED"):
        r = C.TIER_RULES[name]
        if n >= r["min_calls"] and m["score"] >= r["min_score"] and m["wins_2x"] >= r["min_wins"]:
            return name
    return "CANDIDATE"


TIER_MULT = {"ELITE": 1.0, "QUALIFIED": 0.85, "CANDIDATE": 0.4, "NEW": 0.2, "REJECTED": 0.0}


def signal_score(caller: dict, consensus: int, snap: dict | None, safety: dict | None,
                 smart_money: bool | None) -> dict:
    """caller: {'tier','score'}; consensus: distinct QUALIFIED/ELITE callers on this coin inside the window (this
    one included); snap: DexScreener snapshot at detection; safety: mint/freeze authority + top-10 share."""
    snap = snap or {}
    liq = snap.get("liquidity_usd") or 0
    vol = snap.get("volume_h1") or 0
    chg = (snap.get("change_h1") or 0) / 100
    comp = {"caller": (caller.get("score") or 0) / 100 * TIER_MULT.get(caller.get("tier") or "NEW", 0.2),
            "consensus": {0: 0.0, 1: 0.3, 2: 0.75}.get(consensus, 1.0),
            "timing": 1.0 if chg <= C.EARLY_RUNUP_1H else 0.5 if chg <= C.LATE_RUNUP_1H else 0.0,
            "volume": clamp((vol / liq - 0.05) / 0.45) if liq else 0.0,
            "liquidity": 0.0 if liq < C.MIN_LIQUIDITY_USD else 0.5 if liq < 30_000 else 1.0 if liq <= 2_000_000
            else 0.7,
            "volatility": clamp(1 - abs(chg) / 3),
            "smart_money": 1.0 if smart_money else 0.0,
            "safety": 0.5}
    flags = []
    if safety and not safety.get("error"):
        risky = bool(safety.get("mint_authority")) or bool(safety.get("freeze_authority")) or \
            (safety.get("top10_share") or 0) > 0.5
        comp["safety"] = 0.0 if risky else 1.0
        if safety.get("mint_authority"):
            flags.append("MINT_AUTHORITY_SET")
        if safety.get("freeze_authority"):
            flags.append("FREEZE_AUTHORITY_SET")
        if (safety.get("top10_share") or 0) > 0.5:
            flags.append("TOP10_OVER_50PCT")
    score = sum(C.SIGNAL_WEIGHTS[k] * v for k, v in comp.items())
    if caller.get("tier") not in ("ELITE", "QUALIFIED"):
        score = min(score, 49)                     # only a proven caller can lift a coin above "ignore"
        flags.append("CALLER_NOT_PROVEN")
    if "MINT_AUTHORITY_SET" in flags or "FREEZE_AUTHORITY_SET" in flags or liq < C.MIN_LIQUIDITY_USD:
        score = min(score, 49)
        flags.append("CAPPED_RISK")
    label = next(name for th, name in C.SIGNAL_LABELS if score >= th)
    return {"score": round(score, 1), "label": label, "components": comp, "flags": flags,
            "note": "rank of the setup, not a probability of a price rise"}
