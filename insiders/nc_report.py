"""Observation report (read only): how the New Coins signals did, from the nc_obs snapshots.

Entry reference = the price at the 15-minute snapshot (what a user acting on the 0-15 min signals could get).
For each group and horizon (1 h, 6 h, 24 h):
  hit_2x   the running high up to the horizon >= 2 x the 15-min price (the coin offered a 2x at some point)
  close_2x the price at the horizon >= 2 x the 15-min price
  crash    the price at the horizon <= 0.3 x the 15-min price (-70 %)
  median_x median of price at horizon / 15-min price
Missed: coins whose high reached >= 5 x the launch price within 24 h without a Smart Money buyer by minute 15.
Groups are defined from the 15-min snapshot only (no look-ahead). Migrated coins keep their last pump.fun price and
are counted apart; late snapshots (service down at the mark) are excluded."""
from __future__ import annotations

import json
import statistics

HZ = ("1h", "6h", "24h")


def _groups(o15: dict) -> list[str]:
    sig = json.loads(o15.get("signals") or "[]")
    g = ["ALL"]
    if (o15.get("smart") or 0) >= 1:
        g.append("SMART_1" if (o15.get("smart_groups") or o15.get("smart") or 0) < 2 else "SMART_2_INDEPENDENT")
        g.append("SMART_ANY")
    if "MULTI_WALLET" in sig:
        g.append("MULTI")
    if "HIGH_ALERT" in sig:
        g.append("HIGH_ALERT_CURRENT")
    if (o15.get("in_tx") or 0) >= 1:
        g.append("BUNDLE_IN_CREATE_TX")
    if (o15.get("snipe5") or 0) >= 3:
        g.append("SNIPERS_5S_3PLUS")
    if o15.get("dev_sold"):
        g.append("CREATOR_SOLD_BY_15M")
    if "RISK" in sig:
        g.append("RISK")
    if (o15.get("smart") or 0) >= 1 and "RISK" not in sig:
        g.append("FOCUS (Smart, no risk)")
    return g


def report(store, since: float | None = None) -> dict:
    rows = store.q("SELECT * FROM nc_obs" + (" WHERE t >= ?" if since else ""), (since,) if since else ())
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["mint"], {})[r["horizon"]] = r
    stats: dict[str, dict] = {}
    five_x, five_x_missed, base = 0, 0, 0
    for m, o in by.items():
        o15 = o.get("15m")
        if not o15 or o15["status"] == "late" or not o15["price"] or not (o15.get("buyers") or 0):
            continue
        base += 1
        p15, groups = o15["price"], _groups(o15)
        o24 = o.get("24h")
        if o24 and o24["max_price"] and o24["launch_price"] and o24["max_price"] >= 5 * o24["launch_price"]:
            five_x += 1
            if not (o15.get("smart") or 0):
                five_x_missed += 1
        for g in groups:
            s = stats.setdefault(g, {"coins": 0, **{h: {"n": 0, "hit_2x": 0, "close_2x": 0, "crash": 0, "migrated": 0, "x": []} for h in HZ}})
            s["coins"] += 1
            for h in HZ:
                oh = o.get(h)
                if not oh or oh["status"] == "late" or not oh["price"]:
                    continue
                d = s[h]
                d["n"] += 1
                d["migrated"] += oh["status"] == "migrated"
                x = oh["price"] / p15
                d["x"].append(x)
                d["hit_2x"] += (oh["max_price"] or 0) >= 2 * p15
                d["close_2x"] += x >= 2
                d["crash"] += x <= 0.3
    out = {"coins_with_15m_snapshot": base, "five_x_coins": five_x, "five_x_without_smart_by_15m": five_x_missed,
           "missed_share": round(five_x_missed / five_x, 3) if five_x else None, "groups": {}}
    for g, s in sorted(stats.items(), key=lambda kv: -kv[1]["coins"]):
        out["groups"][g] = {"coins": s["coins"], **{h: ({"n": d["n"], "hit_2x": round(d["hit_2x"] / d["n"], 3), "close_2x": round(d["close_2x"] / d["n"], 3),
                                                       "crash": round(d["crash"] / d["n"], 3), "median_x": round(statistics.median(d["x"]), 3),
                                                       "migrated": d["migrated"]} if d["n"] else {"n": 0}) for h, d in ((h, s[h]) for h in HZ)}}
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from insiders.track import Store
    db = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "data" / "insiders_track.db")
    print(json.dumps(report(Store(db)), ensure_ascii=False, indent=1))
