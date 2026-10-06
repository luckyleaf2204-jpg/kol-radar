"""KOL ranking v2: win rate (wins / resolved trades), the win-rate table and its tie-breaks, sample-size and other
filters, new columns (wins, losses, median, streak, best / worst, last seen), status rules unchanged, pagination,
history since first seen, persistence across restarts, no duplicate trades."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import api  # noqa: E402
from kolbot.devs import DevTracker  # noqa: E402
from kolbot.kolhist import KolHistory  # noqa: E402
from kolbot.store import Store  # noqa: E402

NOW = time.time()
_id = [0]


def t(kol, pnl, ts=None, mint=None, gap=False):
    _id[0] += 1
    i = _id[0]
    ts = ts if ts is not None else NOW - 86400 + i
    return {"id": i, "mint": mint or f"M{i}", "kol": kol, "kol_sol": 1.0, "trigger_ts": ts - 60,
            "entry_ts": ts - 56, "entry_how": "after_trade", "exit_ts": ts, "exit_kind": "kol_sold",
            "spend_sol": 0.1, "proceeds_sol": 0.1 + pnl, "pnl_sol": pnl, "net_pct": 1000 * pnl, "gap": gap}


def book(path, rows):
    st = Store(path)
    for r in rows:
        st.closed(r)
    st.db.commit()
    api._cache.clear()
    api._ci.clear()
    return st


def wl(kol, wins, losses, win=0.01, loss=-0.01):
    return [t(kol, win) for _ in range(wins)] + [t(kol, loss) for _ in range(losses)]


def test_win_rate_wins_losses_and_resolved_only(tmp_path):
    rows = wl("A", 3, 1) + [t("A", 0.0)] + [t("A", 0.5, gap=True)]       # 0 P&L = not a win; gap trade excluded
    st = book(tmp_path / "a.db", rows)
    r = api.kol_table(st.db, {})["rows"][0]
    assert (r["n"], r["wins"], r["losses"]) == (5, 3, 2) and r["win_rate_pct"] == 60.0
    assert 0 < r["win_rate_low_pct"] < 60


def test_winrate_ranking_tiebreaks_and_sample_filter(tmp_path):
    rows = (wl("SMALL", 5, 0) + wl("BIG", 200, 100) + wl("BIG2", 140, 70) + wl("MID", 70, 40)
            + wl("RICH", 60, 60, win=1.0))                                   # huge P&L, 50 % win rate
    st = book(tmp_path / "b.db", rows)
    default = api.winrate_table(st.db, {})                                    # default: >= 100 resolved trades
    assert default["min_n"] == 100
    assert [r["kol"] for r in default["rows"]] == ["BIG", "BIG2", "MID", "RICH"]
    assert [r["wr_rank"] for r in default["rows"]] == [1, 2, 3, 4]
    assert all(r["kol"] != "SMALL" for r in default["rows"])                   # 5/5 cannot jump the queue
    # BIG and BIG2 both 66.7 %: more resolved trades first; P&L never decides
    assert default["rows"][0]["n"] == 300 and default["rows"][1]["n"] == 210
    assert default["rows"][3]["pnl_sol"] > default["rows"][0]["pnl_sol"]       # RICH has the most P&L, ranks last
    loose = api.winrate_table(st.db, {}, min_n=1)
    assert loose["rows"][0]["kol"] == "SMALL"                                   # only when the user lowers the bar
    for n, expect in ((30, 5), (50, 5), (100, 4), (200, 2)):
        assert api.winrate_table(st.db, {}, min_n=n)["total"] == (expect if n != 30 and n != 50 else 4), n


def test_winrate_tiebreak_on_wins_is_deterministic(tmp_path):
    st = book(tmp_path / "c.db", wl("X", 60, 40) + wl("Y", 60, 40))            # identical record: stable by wallet
    rows = api.winrate_table(st.db, {}, min_n=1)["rows"]
    assert [r["kol"] for r in rows] == ["X", "Y"]


def test_filters(tmp_path):
    rows = wl("A", 80, 20) + wl("B", 50, 50, win=0.02) + wl("C", 20, 80) + wl("D", 3, 0)
    st = book(tmp_path / "d.db", rows)
    names = lambda res: sorted(r["kol"] for r in res["rows"])  # noqa: E731
    assert names(api.kol_table(st.db, {}, min_wr=55)) == ["A", "D"]
    assert names(api.kol_table(st.db, {}, hi_wr=True)) == ["A", "D"]
    assert names(api.kol_table(st.db, {}, hi_wr=True, min_n=30)) == ["A"]
    assert names(api.kol_table(st.db, {}, pnl="pos")) == ["A", "B", "D"]
    assert names(api.kol_table(st.db, {}, status="REJECT")) == ["C"]
    assert names(api.kol_table(st.db, {}, status="INCONCLUSIVE")) == ["D"]
    KolHistory(st.db, {k: {"name": k} for k in "ABCD"})                         # first_seen from the ledger
    st.db.execute("UPDATE kols SET first_seen_at = ? WHERE wallet IN ('A','B')", (NOW - 40 * 86400,))
    st.db.commit()
    api._cache.clear()
    assert names(api.kol_table(st.db, {}, seen="30d")) == ["C", "D"]             # first seen inside the window
    assert api.winrate_table(st.db, {}, group="control")["rows"] == []


def test_new_columns_median_streak_best_worst_last_seen(tmp_path):
    seq = [0.01, 0.02, -0.03, 0.04, 0.05, 0.06]                                 # ends on 3 wins
    st = book(tmp_path / "e.db", [t("A", p, ts=NOW - 100 + k) for k, p in enumerate(seq)])
    r = api.kol_table(st.db, {})["rows"][0]
    assert r["median_pnl_sol"] == pytest.approx(0.03)
    assert (r["streak"], r["longest_win"], r["longest_loss"]) == (3, 3, 1)
    assert r["best_pct"] == pytest.approx(60) and r["worst_pct"] == pytest.approx(-30)
    assert r["last_seen_at"] == pytest.approx(NOW - 95)
    st.closed(t("A", -0.01, ts=NOW - 10))                                       # cache refreshes on a new trade
    st.db.commit()
    api._cache.clear()
    r = api.kol_table(st.db, {})["rows"][0]
    assert r["streak"] == -1 and r["n"] == 7 and r["median_pnl_sol"] == pytest.approx(0.02)


def test_sorts_and_pagination(tmp_path):
    rows = []
    for k in range(60):
        rows += wl(f"K{k:02d}", k % 7 + 1, k % 5 + 1, win=0.001 * (k + 1))
    st = book(tmp_path / "f.db", rows)
    for key in api.SORTS:
        for d in ("asc", "desc"):
            assert api.kol_table(st.db, {}, sort=key, direction=d)["total"] == 60, key
    w = api.kol_table(st.db, {}, sort="wins")["rows"]
    assert w[0]["wins"] == 7 and all(w[i]["wins"] >= w[i + 1]["wins"] for i in range(len(w) - 1))
    lo = api.kol_table(st.db, {}, sort="losses", direction="asc")["rows"]
    assert lo[0]["losses"] == 1
    p1, p3 = api.winrate_table(st.db, {}, min_n=1, page=1), api.winrate_table(st.db, {}, min_n=1, page=3)
    assert p1["pages"] == 3 and len(p3["rows"]) == 10 and p3["rows"][-1]["wr_rank"] == 60
    wr = [r["wins"] / r["n"] for r in p1["rows"]]
    assert wr == sorted(wr, reverse=True)


def test_status_rules_unchanged(tmp_path):
    st = book(tmp_path / "g.db", wl("UP", 90, 30, win=0.02, loss=-0.01) + wl("DOWN", 30, 90) + wl("FEW", 9, 0))
    by = {r["kol"]: r["status"] for r in api.kol_table(st.db, {})["rows"]}
    assert by == {"UP": "PROVISIONAL", "DOWN": "REJECT", "FEW": "INCONCLUSIVE"}
    assert all(r["status"] != "PASS" for r in api.winrate_table(st.db, {}, min_n=1)["rows"])


def test_history_since_first_seen_persists_and_no_duplicates(tmp_path):
    path = tmp_path / "h.db"
    rows = [t("A", 0.01, ts=NOW - 3 * 86400, mint="T1"), t("A", -0.02, ts=NOW - 2 * 86400, mint="T2"),
            t("A", 0.03, ts=NOW - 3600, mint="T1")]
    st = book(path, rows)
    h = KolHistory(st.db, {"A": {"name": "a"}})
    h.on_kol_event("A", NOW - 4 * 86400)
    h.tick(force=True)
    dv = DevTracker(st.db)
    dv.on_event({"kind": "create", "mint": "T1", "name": "t1", "symbol": "T1", "creator": "DEV", "user": "DEV",
                 "ts": NOW - 5 * 86400})
    dv.tick(force=True)
    st.db.close()
    st = Store(path)                                                             # restart / redeploy
    KolHistory(st.db, {"A": {"name": "a"}}).tick(force=True)
    for r in rows:                                                               # replayed events: no duplicates
        st.closed(r)
    st.db.commit()
    api._cache.clear()
    api._ci.clear()
    d = api.kol_detail(st.db, {"A": {"name": "a"}}, "A")
    sf = d["since_first"]
    assert sf["first_seen_at"] == pytest.approx(NOW - 4 * 86400) and sf["total_trades"] == 3
    assert d["history"]["total"] == 3 and st.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    assert [x["cum_win_rate_pct"] for x in reversed(sf["daily"])] == [100.0, 50.0, pytest.approx(66.7)]
    t1 = next(x for x in d["by_token"] if x["mint"] == "T1")
    assert t1["trades"] == 2 and t1["outcome"] == "DEAD" and t1["dev"]["creator"] == "DEV"
