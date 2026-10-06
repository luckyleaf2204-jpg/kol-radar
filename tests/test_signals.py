"""Display-only buy signals from Top 10 KOL (by win rate) and Top 10 smart wallets: list membership, triggers,
dedup, confluence, outcomes, persistence, the AUTO_TRADE guard, write-route method, data boundaries."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import signals as SG  # noqa: E402
from kolbot.devs import DevTracker  # noqa: E402
from kolbot.kolhist import KolHistory  # noqa: E402
from kolbot.smart import SmartTracker  # noqa: E402
from kolbot.store import Store  # noqa: E402

L = 1_000_000_000
NOW = time.time()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def paper(st, kol, wins, losses, start, hold=120):
    i = st.db.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
    for k in range(wins + losses):
        i += 1
        pnl = 0.01 if k < wins else -0.01
        st.closed({"id": i, "mint": f"P{i}", "kol": kol, "kol_sol": 1, "trigger_ts": start + k, "entry_ts": start + k,
                   "entry_how": "q", "exit_ts": start + k + hold, "exit_kind": "kol_sold", "spend_sol": 0.1,
                   "proceeds_sol": 0.1 + pnl, "pnl_sol": pnl, "net_pct": 1000 * pnl, "gap": False})
    st.db.commit()


def smart_wallet(sm, w, wins, losses, t, hold=90):
    for k in range(wins + losses):
        sm.on_event(tr(w, f"{w}{k}", True, 1.0, t))
        sm.on_event({**tr(w, f"{w}{k}", False, 1.5 if k < wins else 0.5, t + hold), "token": 1000})
        t += hold + 10
    return t


def tr(w, mint, buy, sol, ts, vs=30 * L, vt=10 ** 15, creator=None):
    return {"kind": "trade", "mint": mint, "user": w, "is_buy": buy, "sol": int(sol * L), "token": 1000, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125, "fee_lamports": 0, "creator": creator}


@pytest.fixture
def world(tmp_path):
    from kolbot import api
    api._cache.clear()
    api._ci.clear()
    st = Store(tmp_path / "g.db")
    clk = Clock(NOW)
    roster = {"KTOP": {"name": "ktop"}, "KLOW": {"name": "klow"}, "KFEW": {"name": "kfew"}}
    DevTracker(st.db, clock=clk)
    paper(st, "KTOP", 80, 40, NOW - 9000)
    paper(st, "KLOW", 40, 80, NOW - 8000)
    paper(st, "KFEW", 9, 0, NOW - 7000)                                  # 9/9 but < 100: not listed
    roster["KFAST"] = {"name": "kfast"}
    paper(st, "KFAST", 110, 10, NOW - 6500, hold=13)                     # great record, holds 13 s: no source
    KolHistory(st.db, roster)
    sm = SmartTracker(st.db, roster, clock=clk)
    t = smart_wallet(sm, "SW1", 90, 30, NOW - 30000)
    t = smart_wallet(sm, "SWBAD", 40, 80, t)                             # REJECT: no source
    t = smart_wallet(sm, "SWFAST", 100, 20, t, hold=15)                  # holds 15 s: no source
    smart_wallet(sm, "SWX", 5, 0, t)
    sm.flush(NOW)
    sm.refresh_ci()                                                      # statuses (REJECT) need the CI
    sg = SG.SignalEngine(st.db, roster, clock=clk, log=lambda *_: None)
    sg.refresh(force=True)
    return st, sg, clk


def test_top_lists_follow_dashboard_rules_and_source_filters(world):
    st, sg, clk = world
    top = SG.top_lists(sg)
    kol = {r["wallet"]: r for r in top["kol"]}
    assert set(kol) == {"KFAST", "KTOP", "KLOW"} and kol["KFAST"]["rank"] == 1          # ranking unchanged
    assert kol["KTOP"]["active"] and not kol["KFAST"]["active"] and not kol["KLOW"]["active"]
    assert "giữ trung bình 13.0s < 60s" in kol["KFAST"]["reason"] and "REJECT" in kol["KLOW"]["reason"]
    sw = {r["wallet"]: r for r in top["smart"]}
    assert "SWX" not in sw                                                    # SWX has 5 trades only
    assert sw["SW1"]["active"] and not sw["SWBAD"]["active"] and not sw["SWFAST"]["active"]
    assert all(w not in sw for w in ("KTOP", "KLOW", "KFAST"))               # KOLs never appear as smart wallets
    assert set(sg.top) == {"KTOP", "SW1"}                                     # only these two source signals
    for w in ("KFAST", "KLOW", "SWBAD", "SWFAST"):                            # excluded: never a signal
        assert sg.on_event(tr(w, "EX" + w, True, 2, NOW)) is None
    assert st.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_trigger_dedup_confluence_and_non_top_ignored(world):
    st, sg, clk = world
    assert sg.on_event(tr("RANDOM", "T", True, 5, NOW)) is None             # not in a Top 10
    assert sg.on_event(tr("KTOP", "T", True, 0.01, NOW)) is None             # dust
    assert sg.on_event(tr("KTOP", "T", False, 1, NOW)) is None               # a sell is not a buy signal
    s1 = sg.on_event(tr("KTOP", "T", True, 1, NOW + 1))
    assert s1 and s1["source"] == "kol" and s1["rank"] == 2 and s1["name"] == "ktop" and s1["n"] == 120
    assert s1["status"] == "PROVISIONAL" and s1["auto_eligible"] == 0 and s1["mc_sol"] == pytest.approx(30)
    assert sg.on_event(tr("KTOP", "T", True, 2, NOW + 60)) is None           # same wallet + token within 30 min
    s2 = sg.on_event(tr("SW1", "T", True, 1, NOW + 70))
    assert s2["source"] == "smart" and s2["confluence"] == 2
    conf = dict(st.db.execute("SELECT wallet, confluence FROM signals").fetchall())
    assert conf == {"KTOP": 2, "SW1": 2}
    assert sg.on_event(tr("KTOP", "T", True, 1, NOW + 1 + SG.DEDUP_S + 5))    # a new window: new signal
    assert st.db.execute("SELECT COUNT(*) FROM trades WHERE mint='T'").fetchone()[0] == 0   # nothing traded


def test_outcomes_peak_and_migration(world):
    st, sg, clk = world
    s = sg.on_event(tr("KTOP", "U", True, 1, NOW))
    sg.on_event(tr("x", "U", True, 1, NOW + 100, vs=60 * L))                 # MC 60
    sg.on_event(tr("x", "U", False, 1, NOW + 400, vs=45 * L))                # past 5 min: MC 45
    sg.on_event(tr("x", "U", False, 1, NOW + 2000, vs=15 * L))               # past 30 min: MC 15
    r = st.db.execute("SELECT mc_5m, mc_30m, mc_2h, peak_mc_sol FROM signals WHERE id=?", (s["id"],)).fetchone()
    assert r == (pytest.approx(45), pytest.approx(15), None, pytest.approx(60))
    o = SG.outcome_stats(st.db)["kol"]
    assert o["n"] == 1 and o["mc_5m"]["median_pct"] == pytest.approx(50.0) and o["mc_30m"]["up_share_pct"] == 0
    sg.on_event({"kind": "complete", "mint": "U", "ts": NOW + 3000})
    assert st.db.execute("SELECT migrated FROM signals WHERE id=?", (s["id"],)).fetchone()[0] == 1


def test_persistence_restart_and_states(world, tmp_path):
    st, sg, clk = world
    s = sg.on_event(tr("KTOP", "V", True, 1, NOW))
    st.db.close()
    st2 = Store(tmp_path / "g.db")
    sg2 = SG.SignalEngine(st2.db, {"KTOP": {"name": "ktop"}}, clock=Clock(NOW + 10), log=lambda *_: None)
    assert "V" in sg2.watch                                                  # outcomes keep being tracked
    sg2.on_event(tr("x", "V", True, 1, NOW + 400, vs=33 * L))
    assert st2.db.execute("SELECT mc_5m FROM signals WHERE id=?", (s["id"],)).fetchone()[0] == pytest.approx(33)
    assert sg2.on_event(tr("KTOP", "V", True, 1, NOW + 500)) is None           # dedup survives the restart
    assert SG.set_state(st2.db, s["id"], "dismissed") and not SG.set_state(st2.db, s["id"], "bought")
    L_ = SG.list_signals(st2.db, state="dismissed")
    assert L_["total"] == 1 and L_["rows"][0]["token_url"].endswith("/V") and L_["new"] == 0


def test_auto_trade_guard(monkeypatch):
    monkeypatch.delenv("AUTO_TRADE", raising=False)
    SG.refuse_auto_trade()                                                   # off: starts
    monkeypatch.setenv("AUTO_TRADE", "1")
    with pytest.raises(SystemExit):
        SG.refuse_auto_trade()


def test_state_route_is_post_only(world):
    st, sg, clk = world
    s = sg.on_event(tr("KTOP", "W", True, 1, NOW))
    from kolbot.web import make_routes, serve
    routes = make_routes(st.db, {}, signal_engine=sg)

    async def req(method, path):
        r, w = await asyncio.open_connection("127.0.0.1", 18791)
        w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n".encode())
        await w.drain()
        data = await r.read()
        w.close()
        return data.split(b" ", 2)[1].decode()

    async def main():
        task = asyncio.create_task(serve(routes, port=18791, log=lambda *_: None))
        await asyncio.sleep(0.2)
        try:
            assert await req("GET", f"/api/signals/state?id={s['id']}&state=dismissed") == "405"
            assert await req("POST", f"/api/signals/state?id={s['id']}&state=dismissed") == "200"
            assert await req("POST", "/api/signals") == "405"
            assert await req("GET", "/api/signals/top") == "200"
        finally:
            task.cancel()
    asyncio.run(main())
    assert st.db.execute("SELECT state FROM signals WHERE id=?", (s["id"],)).fetchone()[0] == "dismissed"


def test_signals_do_not_touch_paper_or_research_tables(world):
    st, sg, clk = world
    before = {t: st.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("trades", "sw_trades", "kols")}
    for k in range(20):
        sg.on_event(tr("KTOP", f"Z{k}", True, 1, NOW + k))
        sg.on_event(tr("SW1", f"Z{k}", True, 1, NOW + k))
    after = {t: st.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("trades", "sw_trades", "kols")}
    assert before == after and st.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 40


def test_dead_token_gets_an_outcome(world):
    """No trade after the signal: the horizons take the last market cap instead of staying empty (no survivorship)."""
    st, sg, clk = world
    s = sg.on_event(tr("KTOP", "QUIET", True, 1, NOW))
    sg.on_event(tr("x", "QUIET", False, 1, NOW + 30, vs=20 * L))          # last trade ever: MC 20
    sg.settle(NOW + 200)
    assert st.db.execute("SELECT mc_5m FROM signals WHERE id=?", (s["id"],)).fetchone()[0] is None   # not yet
    sg.settle(NOW + 8000)
    r = st.db.execute("SELECT mc_5m, mc_30m, mc_2h FROM signals WHERE id=?", (s["id"],)).fetchone()
    assert r == (pytest.approx(30), pytest.approx(30), pytest.approx(30))   # no tokens row yet: signal MC
    dv = DevTracker(st.db, clock=clk)                                     # with the token record: its last MC
    s2 = sg.on_event(tr("KTOP", "QUIET2", True, 1, NOW + 9000))
    for ev in (tr("x", "QUIET2", True, 1, NOW + 9000), tr("x", "QUIET2", False, 1, NOW + 9030, vs=12 * L)):
        dv.on_event(ev)
        sg.on_event(ev)
    dv.tick(force=True)
    sg.settle(NOW + 9000 + 8000)
    assert st.db.execute("SELECT mc_2h FROM signals WHERE id=?", (s2["id"],)).fetchone()[0] == pytest.approx(12)
