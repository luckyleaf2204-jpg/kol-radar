"""S3 PRE_SNIPER_SIGNAL: windows on the receive clock, no future data, BwWK17cb never used, INVALID_BWWK, one entry
per token per window, fixed $50 / TP / SL / max hold, P&L, persistence, restart, no effect on S1 / S2 / S2b."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import s3 as S3  # noqa: E402
from kolbot.signal_paper import TRADE_USD, SignalPaper  # noqa: E402
from kolbot.sniper import PINNED_WALLET, S1_CFG  # noqa: E402

L = 1_000_000_000
T = time.time()
PX = 125.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def create(m, recv, dev="DEV"):
    return {"kind": "create", "mint": m, "name": m, "symbol": m, "creator": dev, "user": dev, "ts": int(recv),
            "recv": recv}


def tr(w, m, buy, sol, recv, vs=30 * L, vt=10 ** 15, tok=10 ** 12):
    return {"kind": "trade", "mint": m, "user": w, "is_buy": buy, "sol": int(sol * L), "token": tok, "ts": int(recv),
            "vsol": vs, "vtok": vt, "fee_bps": 125, "fee_lamports": 0, "creator": None, "recv": recv}


def make(tmp_path, risks=None):
    clk = Clock(T)
    books = {w: SignalPaper(tmp_path / f"b{w}.db", lambda: PX, clock=clk, log=lambda *_: None, entry="pre_sniper",
                            exit="none", cfg_overrides=S1_CFG) for w in S3.WINDOWS_MS}
    ps = S3.PreSniper(tmp_path / "s3.db", books, lambda c: (risks or {}).get(c), clock=clk)
    return ps, books, clk


def burst(ps, m, t0, n=3, sol=0.5, dt=0.05):
    for i in range(n):
        ps.on_event(tr(f"B{i}", m, True, sol, t0 + dt * (i + 1)))


def rows(ps, m=None):
    q = "SELECT window_ms, status, buyers, inflow_sol, signal_ts, bwwk_buy_recv FROM s3_signals"
    return ps.db.execute(q + (" WHERE mint=?" if m else ""), ((m,) if m else ())).fetchall()


def test_features_only_use_events_up_to_window_end():
    ev = [(1.00, "a", True, 0.5, 1), (1.20, "b", True, 0.5, 1), (1.40, "c", True, 0.5, 1), (5.0, "d", True, 9, 1)]
    f = S3.features(ev, "DEV", 1.30)
    assert f["buyers"] == 2 and f["inflow_sol"] == pytest.approx(1.0)        # 1.40 and 5.0 are in the future
    f = S3.features(ev, "DEV", 1.40)
    assert f["buyers"] == 3 and S3.fingerprint_ok(f)


def test_windows_fire_independently_and_once_per_token(tmp_path):
    ps, books, clk = make(tmp_path)
    ps.on_event(create("A", T))
    burst(ps, "A", T, n=3, sol=0.5, dt=0.1)                                   # 3 buyers by T + 0.3 s, 1.5 SOL
    clk.t = T + 3.5
    ps.tick()
    got = {w for w, st, *_ in rows(ps, "A")}
    assert got == {500, 1000, 2000, 3000}                                    # 100 / 250 ms: not yet 3 buyers
    burst(ps, "A", T + 5, n=5)                                               # later activity: no second entry
    clk.t = T + 10
    ps.tick()
    assert len(rows(ps, "A")) == 4


def test_signal_fills_after_1s_with_50_usd_and_fixed_exits(tmp_path):
    ps, books, clk = make(tmp_path)
    ps.on_event(create("B", T))
    burst(ps, "B", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "B", True, 0.01, T + 0.15))                          # evaluates the 100 ms window
    assert rows(ps, "B")[0][0] == 100
    ps.tick()
    assert not books[100].eng.positions                                      # 1 s latency not elapsed
    clk.t = T + 1.2
    ps.tick()
    pos = books[100].eng.positions["B"]
    assert pos.spend_sol == pytest.approx(TRADE_USD / PX) and pos.entry_how == "pre_sniper_signal"
    ps.on_event(tr("y", "B", True, 1, T + 2, vs=60 * L, vt=5 * 10 ** 14))    # x4: take profit
    c = books[100].eng.closed[0]
    assert c["exit_kind"] == "take_profit" and c["net_pct"] > 50 and c["pnl_sol"] > 0
    cfg = books[100].eng.cfg
    assert cfg.stop_loss_pct == 30 and cfg.take_profit_pct == 50 and cfg.max_hold_s == 300


def test_stop_loss_and_max_hold(tmp_path):
    ps, books, clk = make(tmp_path)
    for m in ("SL", "MH"):
        ps.on_event(create(m, T))
        burst(ps, m, T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "SL", True, 0.01, T + 0.15))
    clk.t = T + 1.2
    ps.tick()
    ps.on_event(tr("z", "SL", False, 1, T + 2, vs=15 * L, vt=2 * 10 ** 15))
    assert books[100].eng.closed[-1]["exit_kind"] == "stop_loss"
    clk.t = T + 1.2 + 301
    ps.tick()
    assert books[100].eng.closed[-1]["exit_kind"] == "max_hold" and not books[100].eng.positions


def test_sniper_wallet_is_invisible_and_invalidates_late_signals(tmp_path):
    ps, books, clk = make(tmp_path)
    ps.on_event(create("C", T))
    ps.on_event(tr(PINNED_WALLET, "C", True, 5, T + 0.05))                   # the sniper buys first
    burst(ps, "C", T + 0.05, n=3, sol=0.5, dt=0.02)
    clk.t = T + 3.5
    ps.tick()
    r = rows(ps, "C")
    assert r and all(x[1] == "INVALID_BWWK" for x in r)                      # signal came after the sniper buy
    assert all(x[2] == 3 and x[3] == pytest.approx(1.5) for x in r)          # its 5 SOL never counted
    clk.t = T + 5
    ps.tick()
    assert not any(b.eng and b.eng.positions for b in books.values())         # invalid: never traded
    ps.on_event(create("D", T + 10))
    ps.on_event(tr(PINNED_WALLET, "D", True, 9, T + 10.02))                  # alone it cannot make a signal
    clk.t = T + 14
    ps.tick()
    assert rows(ps, "D") == []


def test_sniper_buy_after_signal_only_feeds_latency(tmp_path):
    ps, books, clk = make(tmp_path)
    ps.on_event(create("E", T))
    burst(ps, "E", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.1
    ps.tick()
    clk.t = T + 3.1
    ps.tick()
    ps.on_event(tr(PINNED_WALLET, "E", True, 1, T + 9))
    assert all(x[1] in ("SIGNAL", "TRADED") for x in rows(ps, "E"))
    rep = ps.report()
    assert rep[100]["bwwk_after"] == 1 and rep[100]["median_signal_to_bwwk_s"] == pytest.approx(8.9, abs=0.05)


def test_creator_filters(tmp_path):
    ps, books, clk = make(tmp_path, risks={"BADDEV": "REPEAT FAILURE"})
    ps.on_event(create("F", T, dev="BADDEV"))
    burst(ps, "F", T)
    ps.on_event(create("G", T))
    ps.on_event(tr("DEV", "G", True, 5, T + 0.01, tok=2 * 10 ** 14))         # creator 20 % of supply
    burst(ps, "G", T)
    clk.t = T + 3.5
    ps.tick()
    assert rows(ps, "F") == [] and rows(ps, "G") == []


def test_persistence_and_restart(tmp_path):
    ps, books, clk = make(tmp_path)
    ps.on_event(create("H", T))
    burst(ps, "H", T)
    clk.t = T + 3.5
    ps.tick()
    n, ev = len(rows(ps)), ps.evaluated[3000]
    ps.db.close()
    ps2, books2, clk2 = make(tmp_path)
    assert len(rows(ps2)) == n and ps2.evaluated[3000] == ev
    ps2.on_event(create("H", T + 20))                                        # same token again: no duplicate
    burst(ps2, "H", T + 20)
    clk2.t = T + 25
    ps2.tick()
    assert len(rows(ps2)) == n


def test_s3_does_not_touch_other_books(tmp_path):
    other = SignalPaper(tmp_path / "s2b.db", lambda: PX, clock=Clock(T), log=lambda *_: None, top_n=1, entry="topn",
                        cfg_overrides={"delay_s": 1.0})
    ps, books, clk = make(tmp_path)
    ps.on_event(create("I", T))
    burst(ps, "I", T)
    clk.t = T + 3.5
    ps.tick()
    assert not other.eng.pending and not other.eng.positions and other.eng.cfg.delay_s == 1.0


# --- amendment 4: stream gaps and restarts ---------------------------------------------------------------------------
def make4(tmp_path, t=T):
    clk = Clock(t)
    books = {w: SignalPaper(tmp_path / f"b{w}.db", lambda: PX, clock=clk, log=lambda *_: None, entry="pre_sniper",
                            exit="none", cfg_overrides={**S1_CFG, "gap_flag_s": 0}) for w in S3.WINDOWS_MS}
    return S3.PreSniper(tmp_path / "s3.db", books, lambda c: None, clock=clk), books, clk


def status(ps, m):
    return {w: st for w, st, *_ in rows(ps, m)}


def test_gap_invalidates_open_windows_and_pending_fills(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("A", T))
    burst(ps, "A", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "A", True, 0.01, T + 0.15))               # 100 ms window signals, fill due T + 1.15
    ps.on_gap(T + 0.5, T + 20)                                    # stream lost, reported on reconnect
    st = status(ps, "A")
    assert st[100] == "GAP_INVALID" and all(st[w] == "GAP_INVALID" for w in (250, 500, 1000, 2000, 3000))
    clk.t = T + 21
    ps.tick()
    assert not any(b.eng.positions for b in books.values())       # never traded
    rep = ps.report()
    assert rep[100]["signals"] == 0 and rep[100]["gap_invalid"] == 1


def test_no_fill_while_stream_is_silent(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("B", T))
    burst(ps, "B", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "B", True, 0.01, T + 0.15))
    clk.t = T + 8                                                 # no event for > 5 s: the fill waits
    ps.tick()
    assert not books[100].eng.positions and status(ps, "B")[100] == "SIGNAL"
    ps.on_gap(T + 3, T + 9)                                       # data lost from the last event (T + 0.15)
    assert status(ps, "B")[100] == "GAP_INVALID"


def test_trade_holding_through_a_gap_is_excluded(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("C", T))
    burst(ps, "C", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "C", True, 0.01, T + 0.15))
    clk.t = T + 1.2
    ps.on_event(tr("y", "C", True, 0.01, T + 1.2))
    ps.tick()
    assert "C" in books[100].eng.positions
    ps.on_event(tr("z", "D", True, 0.01, T + 30))                 # stream alive elsewhere
    ps.on_gap(T + 40, T + 60)
    clk.t = T + 1.2 + 301
    ps.tick()
    c = books[100].eng.closed[-1]
    assert c["mint"] == "C" and c["gap"]
    assert books[100].report()["summary"]["n"] == 0               # excluded from the results


def test_gap_before_entry_flags_the_trade(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("E", T))
    burst(ps, "E", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "E", True, 0.01, T + 0.15))
    clk.t = T + 1.2
    ps.on_event(tr("y", "E", True, 0.01, T + 1.2))
    ps.tick()                                                     # filled at T + 1.2
    ps.on_gap(T + 0.2, T + 0.3)                                   # a short outage between signal and fill
    assert status(ps, "E")[100] == "TRADED_GAP"
    clk.t = T + 1.2 + 301
    ps.tick()
    assert books[100].eng.closed[-1]["gap"]


def test_pending_fill_survives_a_quick_restart(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("F", T))
    burst(ps, "F", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "F", True, 0.01, T + 0.15))
    ps.tick()                                                     # heartbeat T + 0.15, pending persisted
    ps.db.close()
    ps2, books2, clk2 = make4(tmp_path, t=T + 0.5)                # back within 5 s, fill still in the future
    assert [p[2] for p in ps2.pending] == ["F"] and status(ps2, "F")[100] == "SIGNAL"
    ps2.on_event(tr("y", "F", True, 0.01, T + 1.2))
    clk2.t = T + 1.2
    ps2.tick()
    assert "F" in books2[100].eng.positions and status(ps2, "F")[100] == "TRADED"


def test_restart_after_downtime_invalidates_pending_and_legacy_signals(tmp_path):
    ps, books, clk = make4(tmp_path)
    ps.on_event(create("G", T))
    burst(ps, "G", T, n=3, sol=0.5, dt=0.02)
    clk.t = T + 0.15
    ps.on_event(tr("x", "G", True, 0.01, T + 0.15))
    ps.tick()
    ps.db.execute("INSERT INTO s3_signals (window_ms, mint, status, signal_ts, create_recv) "
                  "VALUES (100, 'OLD', 'SIGNAL', ?, ?)", (T - 50, T - 51))   # written before amendment 4
    ps.db.commit()
    ps.db.close()
    ps2, books2, clk2 = make4(tmp_path, t=T + 120)                # down for 2 min
    assert ps2.pending == []
    assert status(ps2, "G")[100] in ("INVALID_RESTART", "GAP_INVALID")
    assert status(ps2, "OLD")[100] == "INVALID_RESTART"
    assert ps2.db.execute("SELECT reason FROM s3_gaps").fetchone()[0] == "downtime"
    assert ps2.db.execute("SELECT COUNT(*) FROM s3_pending").fetchone()[0] == 0
    rep = ps2.report()
    assert rep[100]["signals"] == 0 and rep[100]["invalid_restart"] + rep[100]["gap_invalid"] == 2
