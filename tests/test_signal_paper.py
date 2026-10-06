"""Paper book following signals: entries only on signals, ~$50 each from $500, exits follow the source wallet
(even after it left the Top 10), 10 open at most, own database (KOL paper ledger untouched), restart-safe."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot.signal_paper import START_USD, TRADE_USD, SignalPaper  # noqa: E402

L = 1_000_000_000
NOW = time.time()
PX = 125.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def tr(w, mint, buy, sol, ts, vs=30 * L, vt=10 ** 15):
    return {"kind": "trade", "mint": mint, "user": w, "is_buy": buy, "sol": int(sol * L), "token": 1000, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125, "fee_lamports": 0, "creator": None}


def book(tmp_path, px=PX, clock=None):
    return SignalPaper(tmp_path / "sp.db", lambda: px, clock=clock or Clock(NOW), log=lambda *_: None)


def test_waits_for_price_then_sizes_in_usd(tmp_path):
    sp = book(tmp_path, px=None)
    assert sp.eng is None and sp.report()["started"] is False
    sp = book(tmp_path / "b")
    assert sp.eng.cfg.starting_sol == pytest.approx(START_USD / PX)
    assert sp.eng.cfg.position_sol == pytest.approx(TRADE_USD / PX) and sp.eng.cfg.max_open == 10


def test_enters_only_on_signals_and_exits_on_source_sell(tmp_path):
    clk = Clock(NOW)
    sp = book(tmp_path, clock=clk)
    sp.on_event(tr("SRC", "A", True, 1, NOW), None)                         # a buy without a signal: nothing
    assert not sp.eng.pending
    sp.on_event(tr("SRC", "A", True, 1, NOW + 1), {"id": 1})                 # signal: paper buy pending
    assert "A" in sp.eng.pending
    sp.on_event(tr("x", "A", True, 1, NOW + 5), None)                       # first trade >= +3 s fills
    pos = sp.eng.positions["A"]
    assert pos.kol == "SRC" and pos.spend_sol == pytest.approx(TRADE_USD / PX)
    sp.on_event(tr("OTHER", "A", False, 1, NOW + 50), None)                  # someone else selling: no exit
    assert sp.eng.positions["A"].sell_due_ts is None
    sp.on_event(tr("SRC", "A", False, 1, NOW + 60, vs=60 * L, vt=5 * 10 ** 14), None)   # source sells (price x4)
    sp.on_event(tr("x", "A", False, 0.1, NOW + 64, vs=60 * L, vt=5 * 10 ** 14), None)
    r = sp.report()
    assert not sp.eng.positions and r["summary"]["n"] == 1 and r["closed"][0]["exit_kind"] == "kol_sold"
    assert r["closed"][0]["net_pct"] > 200 and r["pnl_usd"] == pytest.approx(r["pnl_sol"] * PX)


def test_source_sells_before_fill_cancels(tmp_path):
    sp = book(tmp_path)
    sp.on_event(tr("SRC", "B", True, 1, NOW), {"id": 1})
    sp.on_event(tr("SRC", "B", False, 1, NOW + 1), None)
    assert not sp.eng.pending and sp.eng.counts["skipped"]["kol_sold_first"] == 1


def test_capital_limit_and_restart(tmp_path):
    clk = Clock(NOW)
    sp = book(tmp_path, clock=clk)
    for k in range(12):
        sp.on_event(tr(f"S{k}", f"M{k}", True, 1, NOW + k), {"id": k})
        sp.on_event(tr("x", f"M{k}", True, 1, NOW + k + 4), None)
    assert len(sp.eng.positions) == 10 and sp.eng.counts["skipped"]["max_open"] == 2
    cash = sp.eng.cash
    sp.store.save(sp.eng)
    sp.store.db.close()
    sp2 = book(tmp_path, px=999.0, clock=clk)                                # restart: price changed meanwhile
    assert len(sp2.eng.positions) == 10 and sp2.eng.cash == pytest.approx(cash)
    assert sp2.eng.cfg.starting_sol == pytest.approx(START_USD / PX)          # capital fixed at the first price
    sp2.on_event(tr("S3", "M3", False, 1, NOW + 100), None)                   # source of M3 still followed
    assert sp2.eng.positions["M3"].sell_due_ts is not None


def test_top_n_books_trigger_on_rank_and_dedup(tmp_path):
    clk = Clock(NOW)
    b5 = SignalPaper(tmp_path / "t5.db", lambda: PX, clock=clk, log=lambda *_: None, top_n=5)
    b20 = SignalPaper(tmp_path / "t20.db", lambda: PX, clock=clk, log=lambda *_: None, top_n=20)
    r3, r15 = {"rank": 3, "source": "smart"}, {"rank": 15, "source": "kol"}
    for b in (b5, b20):
        b.on_event(tr("W3", "A", True, 1, NOW), None, r3)
        b.on_event(tr("W15", "B", True, 1, NOW), None, r15)
        b.on_event(tr("W3", "A", True, 1, NOW + 1), None, r3)                 # same wallet + token: dedup
        b.on_event(tr("Z", "C", True, 1, NOW), {"id": 9}, None)               # a displayed signal does not matter
    assert set(b5.eng.pending) == {"A"} and set(b20.eng.pending) == {"A", "B"}
    assert b5.report()["top_n"] == 5 and b20.eng.counts["copied"] == 0 and b20.eng.counts["kol_buys"] == 2


def test_single_list_books_follow_only_their_source(tmp_path):
    clk = Clock(NOW)
    smart5 = SignalPaper(tmp_path / "s5.db", lambda: PX, clock=clk, log=lambda *_: None, top_n=5, source="smart")
    kol10 = SignalPaper(tmp_path / "k10.db", lambda: PX, clock=clk, log=lambda *_: None, top_n=10, source="kol")
    for b in (smart5, kol10):
        b.on_event(tr("SM2", "A", True, 1, NOW), None, {"rank": 2, "source": "smart"})
        b.on_event(tr("KO7", "B", True, 1, NOW), None, {"rank": 7, "source": "kol"})
        b.on_event(tr("SM8", "C", True, 1, NOW), None, {"rank": 8, "source": "smart"})
    assert set(smart5.eng.pending) == {"A"} and set(kol10.eng.pending) == {"B"}
    assert smart5.report()["label"] == "Top 5" and kol10.report()["source"] == "kol"
