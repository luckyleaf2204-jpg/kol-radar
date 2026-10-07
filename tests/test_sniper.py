"""Sniper paper books: S1 launch filters, entry timing, SL / TP / time exits; S2 fast-bot sources."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import sniper as SN  # noqa: E402
from kolbot.signal_paper import TRADE_USD, SignalPaper  # noqa: E402
from kolbot.smart import SmartTracker  # noqa: E402
from kolbot.store import Store  # noqa: E402

L = 1_000_000_000
NOW = time.time()
PX = 125.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def create(m, dev, ts):
    return {"kind": "create", "mint": m, "name": m, "symbol": m, "creator": dev, "user": dev, "ts": ts}


def tr(w, m, buy, ts, tok=10 ** 12, sol=0.5, vs=30 * L, vt=10 ** 15):
    return {"kind": "trade", "mint": m, "user": w, "is_buy": buy, "sol": int(sol * L), "token": tok, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125, "fee_lamports": 0, "creator": None}


def s1(tmp_path, risks=None):
    clk = Clock(NOW)
    b = SignalPaper(tmp_path / "s1.db", lambda: PX, clock=clk, log=lambda *_: None, entry="launch", exit="none",
                    cfg_overrides=SN.S1_CFG)
    return b, SN.LaunchSniper(b, lambda w: (risks or {}).get(w), clock=clk), clk


def test_s1_enters_3s_after_creation_and_takes_profit(tmp_path):
    b, ls, clk = s1(tmp_path)
    ls.on_event(create("A", "DEV", NOW))
    ls.on_event(tr("DEV", "A", True, NOW, tok=2 * 10 ** 13))               # dev 2 %: fine
    ls.on_event(tr("x", "A", True, NOW + 1))
    assert not b.eng.positions                                            # too early
    ls.on_event(tr("y", "A", True, NOW + 3))                              # first trade >= +3 s: enter
    pos = b.eng.positions["A"]
    assert pos.spend_sol == pytest.approx(TRADE_USD / PX) and pos.entry_how == "after_trade"
    ls.on_event(tr("z", "A", True, NOW + 20, vs=60 * L, vt=5 * 10 ** 14))   # price x4: take profit
    assert not b.eng.positions and b.eng.closed[0]["exit_kind"] == "take_profit"


def test_s1_stop_loss_and_time_exit(tmp_path):
    b, ls, clk = s1(tmp_path)
    for m in ("B", "C"):
        ls.on_event(create(m, "DEV", NOW))
        ls.on_event(tr("y", m, True, NOW + 3))
    ls.on_event(tr("z", "B", False, NOW + 10, vs=15 * L, vt=2 * 10 ** 15))  # price / 4: stop loss
    assert b.eng.closed[-1]["exit_kind"] == "stop_loss"
    clk.t = NOW + 3 + 301
    b.tick()
    assert b.eng.closed[-1]["exit_kind"] == "max_hold" and not b.eng.positions


def test_s1_filters(tmp_path):
    b, ls, clk = s1(tmp_path, risks={"BAD": "REPEAT FAILURE"})
    ls.on_event(create("D", "DEV", NOW))
    ls.on_event(tr("DEV", "D", True, NOW, tok=2 * 10 ** 14))               # dev 20 % of supply
    ls.on_event(tr("y", "D", True, NOW + 3))
    ls.on_event(create("E", "DEV2", NOW))
    for w in ("b1", "b2", "b3"):                                         # 3 wallets in the creation second
        ls.on_event(tr(w, "E", True, NOW))
    ls.on_event(tr("y", "E", True, NOW + 4))
    ls.on_event(create("F", "BAD", NOW))
    ls.on_event(tr("y", "F", True, NOW + 3))
    sk = b.eng.counts["skipped"]
    assert not b.eng.positions and sk["dev_holds_gt_10pct"] == 1 and sk["bundled_launch"] == 1
    assert sk["bad_dev_history"] == 1 and b.eng.counts["kol_buys"] == 3


def test_s1_quiet_token_fills_by_wall_clock_and_max_open(tmp_path):
    b, ls, clk = s1(tmp_path)
    ls.on_event(create("Q", "DEV", NOW))
    ls.on_event(tr("DEV", "Q", True, NOW, tok=10 ** 12))
    clk.t = NOW + 3 + 4.5
    ls.tick()
    assert b.eng.positions["Q"].entry_how == "quiet"
    for k in range(12):
        ls.on_event(create(f"M{k}", f"D{k}", NOW + 10))
        ls.on_event(tr("y", f"M{k}", True, NOW + 13))
    assert len(b.eng.positions) == 10 and b.eng.counts["skipped"]["max_open"] == 3


def test_s2_sources_are_fast_profitable_non_kol_wallets(tmp_path):
    st = Store(tmp_path / "w.db")
    SmartTracker(st.db, {"KOLW": {}})
    rows = [("FAST", 150, 120, 150 * 13, 5.0, 0.0), ("SLOW", 150, 130, 150 * 200, 5.0, 0.0),
            ("LOSER", 150, 40, 150 * 10, -3.0, 0.0), ("SELF", 150, 140, 150 * 10, 5.0, 100),
            ("FEW", 50, 49, 50 * 10, 5.0, 0.0), ("KOLW", 150, 149, 150 * 10, 5.0, 0.0)]
    st.db.executemany("INSERT INTO sw_wallets (wallet, n, wins, hold_sum, ci_lo, self_trades) VALUES (?,?,?,?,?,?)",
                      rows)
    st.db.commit()
    src = SN.SniperSources(st.db)
    src.refresh(force=True)
    assert list(src.sources) == ["FAST"] and src.sources["FAST"]["rank"] == 1
    assert src.source_of(tr("FAST", "T", True, NOW, sol=0.1))["rank"] == 1
    assert src.source_of(tr("FAST", "T", True, NOW, sol=0.01)) is None
    assert src.source_of(tr("FAST", "T", False, NOW)) is None


def test_s2_top1_book_follows_only_rank_1(tmp_path):
    b = SignalPaper(tmp_path / "s2.db", lambda: PX, clock=Clock(NOW), log=lambda *_: None, top_n=1, entry="topn")
    b.on_event(tr("R2", "A", True, NOW, sol=0.5), None, {"rank": 2, "source": "sniper"})
    b.on_event(tr("R1", "B", True, NOW, sol=0.5), None, {"rank": 1, "source": "sniper"})
    assert set(b.eng.pending) == {"B"}


def test_single_source_book_gets_a_trade_level_ci(tmp_path):
    b = SignalPaper(tmp_path / "s2c.db", lambda: PX, clock=Clock(NOW), log=lambda *_: None, top_n=1, entry="topn")
    for k in range(5):
        m = f"T{k}"
        b.on_event(tr("R1", m, True, NOW + k * 100, sol=0.5), None, {"rank": 1, "source": "sniper"})
        b.on_event(tr("x", m, True, NOW + k * 100 + 4), None)
        b.on_event(tr("R1", m, False, NOW + k * 100 + 10, vs=20 * L, vt=15 * 10 ** 14), None)
        b.on_event(tr("x", m, False, NOW + k * 100 + 14, vs=20 * L, vt=15 * 10 ** 14), None)
    r = b.report()
    assert r["summary"]["n"] == 5 and r["summary"]["ci_by"] == "trade" and r["summary"]["ci95_by_kol"][1] < 0
