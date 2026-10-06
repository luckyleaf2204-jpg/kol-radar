"""Dashboard read API: ranking, P&L, ROI, status, filters, sort, pagination, detail, explorer links, idempotent
ledger, persistent KOL history, and a 120k-trade dataset (performance, no N+1)."""
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import api  # noqa: E402
from kolbot.kolhist import KolHistory, kol_history, rebuild_daily, streaks  # noqa: E402
from kolbot.store import Store  # noqa: E402

NOW = time.time()


def trade(i, kol, pnl, spend=0.1, mint=None, exit_ts=None, gap=False, trigger=None):
    exit_ts = exit_ts if exit_ts is not None else NOW - 3600 + i
    trig = trigger if trigger is not None else exit_ts - 60
    return {"id": i, "mint": mint or f"M{i}", "kol": kol, "kol_sol": 1.0, "trigger_ts": trig, "entry_ts": trig + 3.5,
            "entry_how": "after_trade", "exit_ts": exit_ts, "exit_kind": "kol_sold", "spend_sol": spend,
            "proceeds_sol": spend + pnl, "pnl_sol": pnl, "net_pct": 100 * pnl / spend, "gap": gap}


def ledger(tmp_path, rows):
    st = Store(tmp_path / "p.db")
    for r in rows:
        st.closed(r)
    st.db.commit()
    api._cache.clear()
    api._ci.clear()
    return st


ROSTER = {"A": {"name": "alpha", "twitter": "https://x.com/a"}, "B": {"name": "beta"}, "C": {"name": "gamma"}}


def small(tmp_path):
    rows = [trade(1, "A", 0.05), trade(2, "A", -0.02), trade(3, "A", 0.03),          # A: +0.06, 2/3 wins
            trade(4, "B", -0.05), trade(5, "B", -0.01),                               # B: -0.06
            trade(6, "C", 0.01), trade(7, "C", 0.5, gap=True)]                        # C: +0.01 (gap excluded)
    return ledger(tmp_path, rows)


def test_ranking_pnl_roi_win_avg(tmp_path):
    st = small(tmp_path)
    t = api.kol_table(st.db, ROSTER)
    assert [r["kol"] for r in t["rows"]] == ["A", "C", "B"] and [r["rank"] for r in t["rows"]] == [1, 2, 3]
    a = t["rows"][0]
    assert a["n"] == 3 and a["pnl_sol"] == pytest.approx(0.06) and a["roi_pct"] == pytest.approx(20.0)
    assert a["win_rate_pct"] == pytest.approx(66.7) and a["avg_pnl_sol"] == pytest.approx(0.02)
    assert a["name"] == "alpha" and a["wallet_url"] == "https://solscan.io/account/A"
    c = next(r for r in t["rows"] if r["kol"] == "C")
    assert c["n"] == 1 and c["pnl_sol"] == pytest.approx(0.01)                         # gap trade not counted


def test_status_rules_never_pass(tmp_path):
    rows = [trade(i, "W", 0.01 + 0.001 * (i % 3)) for i in range(120)] + \
           [trade(200 + i, "L", -0.02 - 0.001 * (i % 3)) for i in range(120)] + [trade(500, "S", 0.5)]
    st = ledger(tmp_path, rows)
    by = {r["kol"]: r for r in api.kol_table(st.db, {})["rows"]}
    assert by["W"]["status"] == "PROVISIONAL" and by["W"]["ci95"][0] > 0
    assert by["L"]["status"] == "REJECT"
    assert by["S"]["status"] == "INCONCLUSIVE"                                         # n < 100, even if winning
    assert all(r["status"] != "PASS" for r in by.values())
    s = api.summary(st.db, {})
    assert s["n"] == 241 and s["status"] in ("PROVISIONAL", "REJECT", "INCONCLUSIVE") and s["status"] != "PASS"


def test_filters_sort_pagination(tmp_path):
    st = small(tmp_path)
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, min_n=2)["rows"]] == ["A", "B"]
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, pnl="pos")["rows"]] == ["A", "C"]
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, pnl="neg")["rows"]] == ["B"]
    assert api.kol_table(st.db, ROSTER, status="REJECT")["total"] == 0
    assert api.kol_table(st.db, ROSTER, status="INCONCLUSIVE")["total"] == 3
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, sort="trades")["rows"]][0] == "A"
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, sort="pnl", direction="asc")["rows"]] == ["B", "C", "A"]
    assert [r["kol"] for r in api.kol_table(st.db, ROSTER, sort="roi")["rows"]][0] == "A"
    for k in api.SORTS:
        assert api.kol_table(st.db, ROSTER, sort=k)["total"] == 3
    p = api.kol_table(st.db, ROSTER, size=5, page=9)
    assert p["page"] == 1 and p["pages"] == 1                                        # page clamped
    ctl = api.kol_table(st.db, ROSTER, group="control")
    assert ctl["rows"] == [] and "Control" in ctl["note"]
    big = ledger(tmp_path / "b", [trade(i, f"K{i:03d}", 0.001 * i) for i in range(60)])
    p1, p3 = api.kol_table(big.db, {}, size=25, page=1), api.kol_table(big.db, {}, size=25, page=3)
    assert p1["pages"] == 3 and len(p1["rows"]) == 25 and len(p3["rows"]) == 10
    assert p1["rows"][0]["rank"] == 1 and p3["rows"][-1]["rank"] == 60


def test_time_range(tmp_path):
    st = ledger(tmp_path, [trade(1, "A", 0.1, exit_ts=NOW - 40 * 86400), trade(2, "A", -0.01, exit_ts=NOW - 100)])
    assert api.kol_table(st.db, ROSTER, rng="7d")["rows"][0]["pnl_sol"] == pytest.approx(-0.01)
    assert api.kol_table(st.db, ROSTER, rng="all")["rows"][0]["pnl_sol"] == pytest.approx(0.09)
    r = api.kol_table(st.db, ROSTER)["rows"][0]
    assert r["pnl_7d_sol"] == pytest.approx(-0.01) and r["pnl_30d_sol"] == pytest.approx(-0.01)


def test_detail_history_and_links(tmp_path):
    rows = [trade(i, "A", 0.01 if i % 2 else -0.01, mint="T1" if i < 3 else f"T{i}") for i in range(1, 31)]
    st = ledger(tmp_path, rows)
    KolHistory(st.db, ROSTER)
    d = api.kol_detail(st.db, ROSTER, "A", size=10, page=2)
    assert d["rank"] == 1 and d["n"] == 30 and d["tokens"] == 29
    assert d["history"]["total"] == 30 and d["history"]["pages"] == 3 and len(d["history"]["rows"]) == 10
    h = d["history"]["rows"][0]
    assert h["delay_s"] == 3.5 and h["token_url"] == f"https://pump.fun/coin/{h['mint']}"
    assert h["solscan_url"] == f"https://solscan.io/token/{h['mint']}"
    assert d["wallet_url"] == "https://solscan.io/account/A"
    assert any(t["mint"] == "T1" and t["trades"] == 2 for t in d["by_token"])
    sf = d["since_first"]
    assert sf["total_trades"] == 30 and sf["first_seen_source"] == "ledger" and sf["history_status"] == "OK"
    assert api.kol_detail(st.db, ROSTER, "NOPE") is None
    assert api.kol_detail(st.db, ROSTER, "B")["n"] == 0                                # roster KOL without trades


def test_idempotent_ledger_and_migration(tmp_path):
    st = ledger(tmp_path, [trade(1, "A", 0.01)])
    st.closed(trade(1, "A", 0.01))                     # same position again (e.g. replay after restart)
    st.closed(dict(trade(1, "A", 0.01), id=99))        # same position, new id
    st.db.commit()
    assert st.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    legacy = tmp_path / "legacy.db"                    # ledger written before uid existed
    db = sqlite3.connect(legacy)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mint TEXT, kol TEXT, kol_sol REAL, trigger_ts REAL, "
               "entry_ts REAL, entry_how TEXT, exit_ts REAL, exit_kind TEXT, spend_sol REAL, proceeds_sol REAL, "
               "pnl_sol REAL, net_pct REAL, gap INTEGER)")
    db.execute("INSERT INTO trades VALUES (1,'M','A',1,100,103,'q',200,'kol_sold',0.1,0.2,0.1,100,0)")
    db.commit()
    db.close()
    st2 = Store(legacy)
    assert st2.db.execute("SELECT uid FROM trades").fetchone()[0] == "A:M:100.0"
    st2.closed(dict(trade(5, "A", 0.1), mint="M", trigger_ts=100))
    st2.db.commit()
    assert st2.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_kol_history_persistent(tmp_path):
    st = ledger(tmp_path, [trade(1, "A", 0.05, exit_ts=NOW - 2 * 86400), trade(2, "A", -0.02, exit_ts=NOW - 86400),
                           trade(3, "A", 0.03, exit_ts=NOW - 10)])
    h = KolHistory(st.db, ROSTER)
    h.on_kol_event("A", NOW - 3 * 86400)
    h.on_kol_event("A", NOW - 5)
    h.tick(force=True)
    first = st.db.execute("SELECT first_seen_at FROM kols WHERE wallet='A'").fetchone()[0]
    assert first == pytest.approx(NOW - 3 * 86400)
    h2 = KolHistory(st.db, {"A": {"name": "alpha-renamed"}})       # restart + rename: identity and first_seen kept
    h2.on_kol_event("A", NOW)                                        # re-entry later: first_seen does not move
    h2.tick(force=True)
    row = st.db.execute("SELECT display_name, first_seen_at, last_seen_at FROM kols WHERE wallet='A'").fetchone()
    assert row[0] == "alpha-renamed" and row[1] == pytest.approx(first) and row[2] == pytest.approx(NOW)
    daily = kol_history(st.db, "A")["daily"]
    assert [d["cum_pnl_sol"] for d in reversed(daily)] == pytest.approx([0.05, 0.03, 0.06])
    assert daily[0]["cum_roi_pct"] == pytest.approx(20.0)
    n1 = rebuild_daily(st.db)
    assert rebuild_daily(st.db) == n1                                 # idempotent
    k = kol_history(st.db, "A")
    assert k["total_trades"] == 3 and k["winning_trades"] == 2 and k["median_pnl_sol"] == pytest.approx(0.03)
    assert k["best_trade_pct"] == pytest.approx(50) and k["worst_trade_pct"] == pytest.approx(-20)
    assert kol_history(st.db, "ZZ")["history_status"] == "UNKNOWN"


def test_streaks():
    assert streaks([True, True, False, True, True, True]) == {"current": 3, "longest_win": 3, "longest_loss": 1}
    assert streaks([False, False])["current"] == -2 and streaks([])["current"] == 0


def test_explorer_links():
    assert api.solscan("wallet", "W") == "https://solscan.io/account/W"
    assert api.solscan("token", "M") == "https://solscan.io/token/M"
    assert api.solscan("tx", "S") == "https://solscan.io/tx/S"


def test_csv_export(tmp_path):
    st = small(tmp_path)
    lines = api.export_csv(st.db).decode().strip().splitlines()
    assert lines[0].startswith("id,mint,kol") and len(lines) == 8


def test_large_dataset_fast_and_constant_queries(tmp_path):
    rng = random.Random(3)
    st = Store(tmp_path / "big.db")
    rows = []
    for i in range(120_000):
        kol = f"K{rng.randrange(500):03d}"
        spend = 0.1
        pnl = spend * rng.gauss(-0.2, 0.4)
        ts = NOW - 30 * 86400 + i * 20
        rows.append((i + 1, f"M{rng.randrange(40000)}", kol, 1.0, ts - 60, ts - 56.5, "after_trade", ts, "kol_sold",
                     spend, spend + pnl, pnl, 100 * pnl / spend, 0, f"{kol}:{i}"))
    st.db.executemany("INSERT INTO trades (id, mint, kol, kol_sol, trigger_ts, entry_ts, entry_how, exit_ts, "
                      "exit_kind, spend_sol, proceeds_sol, pnl_sol, net_pct, gap, uid) VALUES "
                      "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    st.db.commit()
    KolHistory(st.db, {}).tick(force=True)
    api._cache.clear()
    api._ci.clear()
    queries = []
    st.db.set_trace_callback(queries.append)
    t0 = time.perf_counter()
    s = api.summary(st.db, {}, "all")
    t1 = time.perf_counter()
    table = api.kol_table(st.db, {}, page=2)
    t2 = time.perf_counter()
    d = api.kol_detail(st.db, {}, table["rows"][0]["kol"])
    t3 = time.perf_counter()
    tr = api.trade_table(st.db, {}, page=50)
    t4 = time.perf_counter()
    st.db.set_trace_callback(None)
    assert s["n"] == 120_000 and table["total"] == 500 and len(table["rows"]) == 25
    assert d["history"]["total"] > 0 and len(d["history"]["rows"]) == 25 and tr["page"] == 50
    assert len(s["cumulative"]) <= 301                                # downsampled, not 120k points
    assert len(json.dumps(s)) < 200_000 and len(json.dumps(table)) < 100_000
    for dt_, limit in ((t1 - t0, 6.0), (t2 - t1, 2.0), (t3 - t2, 3.0), (t4 - t3, 2.0)):
        assert dt_ < limit, (t1 - t0, t2 - t1, t3 - t2, t4 - t3)
    assert len(queries) < 40, len(queries)                             # independent of the 500 KOLs: no N+1
    t5 = time.perf_counter()
    api.summary(st.db, {}, "all")
    api.kol_table(st.db, {}, page=3)
    assert time.perf_counter() - t5 < 1.5                              # cached CIs / ranking
    print(f"\n120k trades: summary {t1 - t0:.2f}s, table {t2 - t1:.2f}s, detail {t3 - t2:.2f}s, "
          f"trades {t4 - t3:.2f}s, {len(queries)} queries")
