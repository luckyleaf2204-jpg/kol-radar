"""Live view of KOL tokens and the dev heuristic (no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot.meta import creator_rating  # noqa: E402
from kolbot.watch import Watch, mc_sol  # noqa: E402

L = 1_000_000_000


def ev(mint, user, buy, sol, ts, vs=30 * L, vt=10 ** 15):
    return {"kind": "trade", "mint": mint, "user": user, "is_buy": buy, "sol": int(sol * L), "token": 1, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125}


def test_mc_matches_pump_fun():
    # pump.fun reported market_cap 69.4438 SOL for these reserves (2026-10-06)
    assert abs(mc_sol(47279987408, 680837746452872) - 69.4438) < 1e-3


def test_watch_counts_from_first_kol_buy():
    w = Watch({"K": "kol1"}, clock=lambda: 1000)
    w.on_event(ev("M", "x", True, 1, 900))                 # before any KOL: ignored
    w.on_event(ev("M", "K", True, 0.005, 901))             # dust KOL buy: no card
    assert not w.mints
    w.on_event(ev("M", "K", True, 1, 950))
    w.on_event(ev("M", "a", True, 0.5, 960, vs=60 * L, vt=5 * 10 ** 14))
    w.on_event(ev("M", "K", False, 0.4, 990))
    r = w.rows()[0]
    assert r["traders"] == 2 and r["buys"] == 2 and r["sells"] == 1 and r["traders_5m"] == 2
    assert r["kols"][0]["sol"] == 1 and r["kols"][0]["sold_sol"] == 0.4 and r["kols"][0]["first_mc_sol"] == 30
    assert r["ath_mc_sol"] == 120 and w.feed[0]["side"] == "sell"


def test_creator_rating():
    assert creator_rating([{"mint": "NOW"}], "NOW")["label"].startswith("Dev mới")
    spam = [{"mint": str(i), "complete": False} for i in range(12)]
    assert creator_rating(spam)["tone"] == "bad"
    good = [{"mint": "a", "complete": True, "ath_market_cap": 90000}, {"mint": "b"}, {"mint": "c"}]
    r = creator_rating(good)
    assert r["tone"] == "good" and r["graduated"] == 1 and r["best_ath_usd"] == 90000
