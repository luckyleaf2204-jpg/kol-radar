"""Smart Wallet Radar: discovery from a wallet's own round trips, KOL exclusion, win rate, sample filter, ranking and
tie-breaks, CI / status, Top 10 / 50, pagination, detail, flags, persistence across restarts (open positions,
gaps for downtime), dedup, and a large dataset."""
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import smart_api as SA  # noqa: E402
from kolbot.devs import DevTracker  # noqa: E402
from kolbot.smart import EXPIRE_S, SmartTracker  # noqa: E402
from kolbot.store import Store  # noqa: E402

L = 1_000_000_000
T0 = time.time() - 10 * 86400


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def tr(w, mint, buy, sol, tok, ts, fee=0, creator=None, vs=30 * L, vt=10 ** 15):
    return {"kind": "trade", "mint": mint, "user": w, "is_buy": buy, "sol": int(sol * L), "token": tok, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125, "fee_lamports": fee, "creator": creator}


def setup(tmp_path, roster=None, t=T0):
    st = Store(tmp_path / "s.db")
    clk = Clock(t)
    DevTracker(st.db, clock=clk)
    return st, SmartTracker(st.db, roster or {}, clock=clk), clk


def round_trip(sm, w, mint, ts, win=True, sol=1.0):
    sm.on_event(tr(w, mint, True, sol, 1000, ts))
    sm.on_event(tr(w, mint, False, sol * (1.5 if win else 0.5), 1000, ts + 30))


def test_discovery_pnl_fees_dust_and_partial(tmp_path):
    st, sm, clk = setup(tmp_path)
    sm.on_event(tr("W", "M", True, 0.01, 10, T0))                        # dust first buy: no position
    assert not sm.mem
    sm.on_event(tr("W", "M", True, 1.0, 1000, T0 + 1, fee=10_000_000))   # pays 1.01 SOL
    sm.on_event(tr("W", "M", False, 0.6, 500, T0 + 2, fee=5_000_000))    # half: still open
    assert ("W", "M") in sm.mem
    sm.on_event(tr("W", "M", False, 0.9, 495, T0 + 3, fee=5_000_000))    # 99 % sold: closed
    sm.flush(T0 + 4)
    r = st.db.execute("SELECT kind, sol_in, sol_out, pnl_sol, roi_pct FROM sw_trades").fetchone()
    assert r[0] == "closed" and r[1] == pytest.approx(1.01) and r[2] == pytest.approx(1.49)
    assert r[3] == pytest.approx(0.48) and r[4] == pytest.approx(100 * 0.48 / 1.01)
    w = SA.smart_detail(st.db, "W")
    assert w["n"] == 1 and w["wins"] == 1 and w["tokens"] == 1 and w["first_seen"] == T0 + 1


def test_expired_and_migrated(tmp_path):
    st, sm, clk = setup(tmp_path)
    dv = DevTracker(st.db, clock=clk)
    for ev in (tr("H", "DEAD", True, 1.0, 1000, T0, vs=30 * L), tr("x", "DEAD", True, 0.1, 1, T0 + 5, vs=3 * L)):
        dv.on_event(ev)
        sm.on_event(ev)
    sm.on_event(tr("H", "MIG", True, 1.0, 1000, T0))
    sm.on_event({"kind": "complete", "mint": "MIG", "ts": T0 + 60})
    dv.tick(force=True)
    clk.t = T0 + EXPIRE_S + 100
    sm.sweep(clk.t)
    rows = dict(st.db.execute("SELECT kind, pnl_sol FROM sw_trades WHERE wallet='H'").fetchall())
    # 1000 raw tokens at MC 3 SOL: value 1000 * 3 / 1e15 SOL ~ 0 -> loss of ~1 SOL, no survivorship
    assert rows["expired"] == pytest.approx(-1.0, abs=1e-6) and rows["migrated"] is None
    w = SA.smart_detail(st.db, "H")
    assert w["n"] == 1 and w["wins"] == 0 and w["unresolved"] == 1


def test_kol_exclusion_follows_roster(tmp_path):
    st, sm, clk = setup(tmp_path, roster={"KOL": {}})
    for k in range(3):
        round_trip(sm, "KOL", f"A{k}", T0 + k * 100)
        round_trip(sm, "W", f"B{k}", T0 + k * 100)
    sm.flush(T0 + 1000)
    assert [r["wallet"] for r in SA.smart_table(st.db, min_n=1)["rows"]] == ["W"]
    assert SA.smart_detail(st.db, "KOL")["is_kol"]
    SmartTracker(st.db, {"W": {}}, clock=clk)                            # roster changes: W is a KOL now
    assert [r["wallet"] for r in SA.smart_table(st.db, min_n=1)["rows"]] == ["KOL"]


def populate(sm, spec, t0=T0):
    t = t0
    for w, wins, losses in spec:
        for k in range(wins + losses):
            round_trip(sm, w, f"{w}-{k}", t, win=k < wins)
            t += 40
    sm.flush(t + 10)


def test_ranking_sample_filter_tiebreak_top_and_pages(tmp_path):
    st, sm, clk = setup(tmp_path)
    spec = [("NINE", 9, 1), ("BIG", 150, 50), ("BIG2", 120, 40), ("MID", 90, 60), ("LOW", 30, 120)]
    spec += [(f"F{k:02d}", 40 + k, 60) for k in range(60)]
    populate(sm, spec)
    d = SA.smart_table(st.db)                                            # default: >= 100 resolved trades
    assert d["min_n"] == 100 and all(r["wallet"] != "NINE" for r in d["rows"])
    assert [r["wallet"] for r in d["rows"][:2]] == ["BIG", "BIG2"]           # both 75 %: more trades first
    assert d["rows"][0]["rank"] == 1 and d["rows"][0]["win_rate_pct"] == 75.0
    assert SA.smart_table(st.db, min_n=1)["rows"][0]["wallet"] == "NINE"      # only if the user lowers the bar
    assert SA.smart_table(st.db, min_n=200)["total"] == 1
    assert SA.smart_table(st.db, top="10")["total"] == 10 and SA.smart_table(st.db, top="50")["total"] == 50
    assert SA.smart_table(st.db, top="50")["eligible"] == 64                  # others are kept, only hidden
    p3 = SA.smart_table(st.db, top="50", page=2)
    assert p3["pages"] == 2 and len(p3["rows"]) == 25 and p3["rows"][-1]["rank"] == 50
    allr = SA.smart_table(st.db, size=100)["rows"]
    wr = [(r["wins"] / r["n"], r["n"]) for r in allr]
    assert wr == sorted(wr, key=lambda x: (-x[0], -x[1]))
    assert SA.smart_table(st.db, pnl="pos")["total"] < SA.smart_table(st.db)["total"]
    for r in SA.smart_table(st.db, size=100)["rows"]:                       # detail rank == table rank
        assert SA.smart_detail(st.db, r["wallet"])["rank_all"] == r["rank"]


def test_status_ci_never_pass(tmp_path):
    st, sm, clk = setup(tmp_path)
    populate(sm, [("GOOD", 150, 50), ("BAD", 50, 150), ("FEW", 20, 0)])
    assert {r["wallet"]: r["status"] for r in SA.smart_table(st.db, min_n=1)["rows"]}["GOOD"] == "INCONCLUSIVE"
    assert sm.refresh_ci() == 2                                             # only wallets with n >= 100
    st_ = {r["wallet"]: r["status"] for r in SA.smart_table(st.db, min_n=1)["rows"]}
    assert st_ == {"GOOD": "PROVISIONAL", "BAD": "REJECT", "FEW": "INCONCLUSIVE"}
    assert SA.smart_table(st.db, min_n=1, status="PASS")["total"] == 0
    assert SA.smart_table(st.db, min_n=1, status="REJECT")["rows"][0]["wallet"] == "BAD"


def test_flags_with_evidence_and_hiding(tmp_path):
    st, sm, clk = setup(tmp_path)
    for k in range(10):                                                     # trades its own tokens
        sm.on_event({"kind": "create", "mint": f"S{k}", "creator": "SELF", "ts": T0 + k * 100})
        round_trip(sm, "SELF", f"S{k}", T0 + k * 100 + 1)
    t = T0 + 5000
    for k in range(60):                                                     # 2-second round trips
        sm.on_event(tr("HFT", f"H{k}", True, 1.0, 1000, t))
        sm.on_event(tr("HFT", f"H{k}", False, 1.1, 1000, t + 2))
        t += 10
    sm.flush(t + 10)
    codes = lambda w: {f["code"] for f in SA.smart_detail(st.db, w)["flags"]}  # noqa: E731
    assert "self_trading" in codes("SELF") and "abnormal" in codes("HFT")
    assert "2.0s" in next(f for f in SA.smart_detail(st.db, "HFT")["flags"] if f["code"] == "abnormal")["evidence"]
    assert SA.smart_table(st.db, min_n=1)["total"] == 0                       # hidden by default
    assert SA.smart_table(st.db, min_n=1, hide_flagged=False)["total"] == 2


def test_restart_keeps_open_positions_dedups_and_flags_downtime(tmp_path):
    st, sm, clk = setup(tmp_path)
    sm.on_event(tr("W", "A", True, 1.0, 1000, T0))
    sm.on_event(tr("W", "B", True, 1.0, 1000, T0))
    sm.flush(T0 + 10)
    st.db.close()
    st2 = Store(tmp_path / "s.db")                                          # restart 30 s later: no gap
    sm2 = SmartTracker(st2.db, {}, clock=Clock(T0 + 40))
    sm2.on_event(tr("W", "A", False, 2.0, 1000, T0 + 50))                    # position from before the restart
    sm2.flush(T0 + 60)
    w = SA.smart_detail(st2.db, "W")
    assert w["n"] == 1 and w["wins"] == 1 and w["first_seen"] == T0
    sm2._write_resolved([dict(uid=f"W:A:{float(T0)}", wallet="W", mint="A", open_ts=T0, close_ts=T0 + 50,
                              sol_in=1, sol_out=2, mark_sol=0, pnl_sol=1, roi_pct=100, kind="closed", hold_s=50,
                              buys=1, sells=1, self_token=0, gap=0)])          # replayed resolution
    st2.db.commit()
    assert SA.smart_detail(st2.db, "W")["n"] == 1                             # not double counted
    st2.db.close()
    st3 = Store(tmp_path / "s.db")                                          # restart after 1 h down: gap
    sm3 = SmartTracker(st3.db, {}, clock=Clock(T0 + 3700))
    assert st3.db.execute("SELECT COUNT(*) FROM sw_gaps").fetchone()[0] == 1
    sm3.on_event(tr("W", "B", False, 2.0, 1000, T0 + 3800))                  # held across the downtime
    sm3.flush(T0 + 3810)
    w = SA.smart_detail(st3.db, "W")
    assert w["n"] == 1 and w["gap_excluded"] == 1 and w["history"]["total"] == 2


def test_detail_history_daily_cumulative_tokens(tmp_path):
    st, sm, clk = setup(tmp_path)
    for k in range(30):
        round_trip(sm, "W", f"T{k % 7}", T0 + k * 4000, win=k % 3 != 0)
    sm.flush(T0 + 200000)
    d = SA.smart_detail(st.db, "W", page=2, size=10)
    assert d["n"] == 30 and d["wins"] == 20 and d["losses"] == 10 and d["rank_all"] is None   # < 100: unranked
    assert d["history"]["total"] == 30 and d["history"]["pages"] == 3 and len(d["history"]["rows"]) == 10
    assert d["daily"][0]["cum_trades"] == 30 and d["daily"][0]["cum_win_rate_pct"] == pytest.approx(66.7)
    assert d["cumulative"][-1][1] == pytest.approx(20 * 0.5 - 10 * 0.5)
    assert len(d["tokens_traded"]) == 7 and d["median_pnl_sol"] == pytest.approx(0.5)
    assert d["long_win"] == 2 and d["long_loss"] == 1 and d["wallet_url"].endswith("/account/W")
    assert SA.smart_detail(st.db, "NOPE") is None


def test_large_dataset(tmp_path):
    st, sm, clk = setup(tmp_path)
    rng = random.Random(5)
    rows, wallets = [], {}
    for i in range(300_000):
        w = f"W{rng.randrange(60_000):05d}" if i % 3 else f"P{rng.randrange(800):03d}"   # some heavy traders
        pnl = rng.gauss(0, 0.3)
        rows.append((f"{w}:{i}", w, f"M{i % 50000}", T0 + i, T0 + i + 30, 1.0, 1 + pnl, 0, pnl, 100 * pnl, "closed",
                     30, 1, 1, 0, 0))
        a = wallets.setdefault(w, [0, 0, 0.0])
        a[0] += 1
        a[1] += pnl > 0
        a[2] += pnl
    st.db.executemany("INSERT INTO sw_trades (uid, wallet, mint, open_ts, close_ts, sol_in, sol_out, mark_sol, pnl_sol,"
                      " roi_pct, kind, hold_s, buys, sells, self_token, gap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      rows)
    st.db.executemany("INSERT INTO sw_wallets (wallet, first_seen, last_seen, n, wins, pnl_sol, sol_in, hold_sum) "
                      "VALUES (?,?,?,?,?,?,?,?)", [(w, T0, T0 + 1, a[0], a[1], a[2], a[0], 30 * a[0])
                                                    for w, a in wallets.items()])
    st.db.commit()
    t0 = time.perf_counter()
    tab = SA.smart_table(st.db)
    t1 = time.perf_counter()
    summ = SA.smart_summary(st.db)
    t2 = time.perf_counter()
    det = SA.smart_detail(st.db, tab["rows"][0]["wallet"])
    t3 = time.perf_counter()
    sm.refresh_ci()
    t4 = time.perf_counter()
    assert tab["eligible"] == sum(1 for a in wallets.values() if a[0] >= 100) and len(tab["rows"]) == 25 and summ["wallets"] == len(wallets)
    assert det["history"]["total"] > 100
    assert (t1 - t0) < 1.5 and (t2 - t1) < 3 and (t3 - t2) < 1.5 and (t4 - t3) < 10, (t1 - t0, t2 - t1, t3 - t2,
                                                                                   t4 - t3)
    print(f"\nsmart 300k trades / {len(wallets)} wallets: table {t1 - t0:.2f}s summary {t2 - t1:.2f}s "
          f"detail {t3 - t2:.2f}s ci batch {t4 - t3:.2f}s")
