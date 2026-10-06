"""SYNTHETIC demo database for UI / load testing only (never deployed, never mixed with real paper data).

usage: python tools/demo_db.py [--out data/demo.db] [--trades 120000]
then:  python main.py --no-stream --db data/demo.db --port 8781"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kolbot.devs import DevTracker  # noqa: E402
from kolbot.kolhist import KolHistory  # noqa: E402
from kolbot.store import Store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "demo.db"))
    ap.add_argument("--trades", type=int, default=120_000)
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        out.unlink()
    roster = {r["wallet"]: {"name": r.get("name"), "twitter": r.get("twitter")}
              for r in json.loads((ROOT / "kols.json").read_text(encoding="utf-8"))["wallets"]}
    kols = list(roster)[:300]
    rng = random.Random(42)
    skill = {k: rng.gauss(-0.15, 0.12) for k in kols}
    st = Store(out)
    now = time.time()
    devs = [f"DEMOdev{i:05d}" + "x" * 30 for i in range(4000)]
    mints = [f"DEMOmint{i:06d}" + "p" * 30 for i in range(30000)]
    creator = {m: rng.choice(devs) for m in mints}
    rows = []
    for i in range(a.trades):
        k = rng.choice(kols)
        m = rng.choice(mints)
        ts = now - 30 * 86400 + i * (30 * 86400 / a.trades)
        pct = max(-100.0, 100 * rng.gauss(skill[k], 0.35))
        pnl = 0.1 * pct / 100
        rows.append((i + 1, m, k, round(rng.uniform(0.05, 3), 3), ts - 60, ts - 60 + rng.uniform(3, 9), "after_trade",
                     ts, rng.choice(["kol_sold"] * 8 + ["migrated", "max_hold"]), 0.1, 0.1 + pnl, pnl, pct,
                     int(rng.random() < 0.003), f"{k}:{m}:{ts - 60}"))
    st.db.executemany("INSERT INTO trades (id, mint, kol, kol_sol, trigger_ts, entry_ts, entry_how, exit_ts, exit_kind,"
                      " spend_sol, proceeds_sol, pnl_sol, net_pct, gap, uid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      rows)
    st.db.commit()
    tk = DevTracker(st.db, clock=lambda: now)
    for j, m in enumerate(mints):
        c = now - 30 * 86400 + j * 86
        tk.on_event({"kind": "create", "mint": m, "name": f"Demo {j}", "symbol": f"DM{j}", "creator": creator[m],
                     "user": creator[m], "ts": c})
        peak = rng.uniform(30, 400)
        tk.on_event({"kind": "trade", "mint": m, "user": creator[m], "is_buy": True, "sol": 10 ** 9, "token": 10 ** 12,
                     "ts": c + 2, "vsol": 30 * 10 ** 9, "vtok": 10 ** 15, "fee_bps": 125, "creator": creator[m]})
        tk.on_event({"kind": "trade", "mint": m, "user": "buyer", "is_buy": True, "sol": 10 ** 9, "token": 10 ** 12,
                     "ts": c + 60, "vsol": int(peak * 10 ** 9), "vtok": 10 ** 15, "fee_bps": 125,
                     "creator": creator[m]})
        dump = rng.random() < 0.3
        tk.on_event({"kind": "trade", "mint": m, "user": creator[m] if dump else "seller", "is_buy": False,
                     "sol": 10 ** 9, "token": 10 ** 12, "ts": c + 300,
                     "vsol": int((peak * (0.1 if dump else rng.uniform(0.2, 1.2))) * 10 ** 9), "vtok": 10 ** 15,
                     "fee_bps": 125, "creator": creator[m]})
        if rng.random() < 0.02:
            tk.on_event({"kind": "complete", "mint": m, "ts": c + 900})
        if rng.random() < 0.2:
            tk.on_event({"kind": "trade", "mint": m, "user": rng.choice(kols), "is_buy": True, "sol": 10 ** 9,
                         "token": 10 ** 12, "ts": c + 30, "vsol": 40 * 10 ** 9, "vtok": 10 ** 15, "fee_bps": 125,
                         "creator": creator[m]}, is_kol=True)
    tk.tick(force=True)
    h = KolHistory(st.db, roster)
    for k in kols:
        h.on_kol_event(k, now - 30 * 86400 - rng.uniform(0, 86400))
    h.tick(force=True)
    print(f"demo db {out}: {a.trades} trades, {len(mints)} tokens, {len(devs)} devs")


if __name__ == "__main__":
    main()
