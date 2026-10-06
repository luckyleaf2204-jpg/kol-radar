"""Paper engine on synthetic events (no network)."""
import base64
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import stream as S  # noqa: E402
from kolbot.config import Config  # noqa: E402
from kolbot.engine import LAMPORTS, Curve, Engine, buy_tokens, sell_sol  # noqa: E402
from kolbot.report import summarize  # noqa: E402
from kolbot.store import Store  # noqa: E402

KOL, OTHER = "KOLwallet", "someone"
V0S, V0T = 30 * LAMPORTS, 1_000_000_000_000_000        # pump.fun start: 30 SOL / 1e9 tokens (6 decimals)


class Clock:
    def __init__(self, t=1_791_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def trade(mint, user, buy, sol, ts, vs=V0S, vt=V0T, fee_bps=125):
    return {"kind": "trade", "mint": mint, "sol": int(sol * LAMPORTS), "token": 1, "is_buy": buy, "user": user,
            "ts": ts, "vsol": vs, "vtok": vt, "fee_bps": fee_bps}


def eng(tmp_path=None, **kw):
    clock = Clock()
    cfg = Config(**kw)
    store = Store(tmp_path / "p.db") if tmp_path else None
    return Engine(cfg, {KOL}, store, clock=clock, log=lambda *_: None), clock


def test_decode_live_layout():
    d = S.TRADE_DISC + bytes(32) + struct.pack("<QQ", 2 * LAMPORTS, 5000) + b"\x01" + bytes(range(32)) \
        + struct.pack("<q", 1_791_000_000) + struct.pack("<QQQQ", V0S, V0T, 1, 1) + bytes(32) \
        + struct.pack("<QQ", 95, 0) + bytes(32) + struct.pack("<QQ", 30, 0)
    ev = S.decode("Program data: " + base64.b64encode(d).decode())
    assert ev["is_buy"] and ev["sol"] == 2 * LAMPORTS and ev["vsol"] == V0S and ev["fee_bps"] == 125
    bad = d[:89] + struct.pack("<q", -5) + d[97:]
    assert S.decode("Program data: " + base64.b64encode(bad).decode()) is None


def test_round_trip_at_same_curve_loses_only_costs():
    cfg = Config(extra_slippage_pct=0)
    c = Curve(V0S, V0T, 125, 0)
    tok = buy_tokens(c, 0.1, cfg)
    after = Curve(c.vsol + int((0.1 - 0.00511) / 1.0125 * LAMPORTS), int(c.vsol * c.vtok / (c.vsol + (0.1 - 0.00511)
                                                                                           / 1.0125 * LAMPORTS)), 125, 0)
    back = sell_sol(after, tok, cfg)
    assert back == pytest.approx((0.1 - 0.00511) / 1.0125 * (1 - 0.0125) - 0.00511, rel=1e-6)


def test_copy_buy_then_exit_when_kol_sells():
    e, clock = eng(extra_slippage_pct=0)
    e.on_event(trade("M", KOL, True, 1.0, 100))
    assert "M" in e.pending
    e.on_event(trade("M", OTHER, True, 0.5, 102))          # before the delay: no fill yet
    assert "M" not in e.positions
    e.on_event(trade("M", OTHER, True, 0.5, 103))          # first trade >= trigger + 3 s: fill on this curve
    pos = e.positions["M"]
    assert pos.entry_how == "after_trade" and e.cash == pytest.approx(4.9)
    e.on_event(trade("M", KOL, False, 1.0, 200, vs=2 * V0S, vt=V0T // 2))   # KOL sells; price 4x
    assert e.positions["M"].sell_due_ts == 203
    e.on_event(trade("M", OTHER, False, 0.1, 203, vs=2 * V0S, vt=V0T // 2))
    assert not e.positions and e.closed[0]["exit_kind"] == "kol_sold" and e.closed[0]["net_pct"] > 250


def test_quiet_token_fills_after_grace_and_max_hold_exits():
    e, clock = eng(max_hold_s=60)
    e.on_event(trade("Q", KOL, True, 1.0, 100))
    clock.t += 3 + 4
    e.tick()
    assert e.positions["Q"].entry_how == "quiet"
    clock.t += 61
    e.tick()
    assert e.closed[0]["exit_kind"] == "max_hold" and e.closed[0]["net_pct"] < 0     # same curve: costs only


def test_gates_and_kol_selling_first():
    e, _ = eng(max_open=1)
    e.on_event(trade("A", KOL, True, 0.01, 1))             # too small
    e.on_event(trade("B", KOL, True, 1.0, 1))
    e.on_event(trade("C", KOL, True, 1.0, 1))              # max_open reached by the pending B
    assert e.counts["skipped"] == {"small_buy": 1, "max_open": 1}
    e.on_event(trade("B", KOL, False, 1.0, 2))             # KOL dumps before our buy lands
    assert not e.pending and e.counts["skipped"]["kol_sold_first"] == 1


def test_migration_exits_at_last_curve_price():
    e, _ = eng()
    e.on_event(trade("M", KOL, True, 1.0, 100))
    e.on_event(trade("M", OTHER, True, 1.0, 104))
    e.on_event({"kind": "complete", "mint": "M", "ts": 110})
    assert e.closed[0]["exit_kind"] == "migrated" and "M" in e.completed
    e.on_event(trade("M", KOL, True, 1.0, 120))
    assert e.counts["skipped"]["completed"] == 1


def test_daily_loss_limit_blocks_new_entries():
    e, clock = eng(daily_loss_limit_sol=0.01, max_hold_s=1)
    e.on_event(trade("L", KOL, True, 1.0, 1))
    e.on_event(trade("L", OTHER, True, 1.0, 5))
    e.on_event(trade("L", OTHER, False, 1.0, 6, vs=V0S // 2, vt=V0T * 2))          # price collapses
    clock.t += 2
    e.tick()
    assert e.closed[0]["pnl_sol"] < -0.01
    e.on_event(trade("N", KOL, True, 1.0, 10))
    assert e.counts["skipped"]["daily_loss_limit"] == 1


def test_state_survives_restart(tmp_path):
    e, _ = eng(tmp_path)
    e.on_event(trade("M", KOL, True, 1.0, 100))
    e.on_event(trade("M", OTHER, True, 1.0, 104))
    e2, _ = eng(tmp_path)
    assert "M" in e2.positions and e2.cash == pytest.approx(e.cash)
    e2.on_event(trade("M", KOL, False, 1.0, 200))
    e2.on_event(trade("M", OTHER, False, 0.1, 204))
    e3, _ = eng(tmp_path)
    assert len(e3.closed) == 1 and not e3.positions


def test_gap_flag_and_report():
    e, clock = eng(max_hold_s=1000)
    e.on_event(trade("M", KOL, True, 1.0, int(clock.t)))
    e.on_event(trade("M", OTHER, True, 1.0, int(clock.t) + 4))
    e.on_gap(clock.t + 10, clock.t + 400)
    clock.t += 1010
    e.tick()
    assert e.closed[0]["gap"] is True
    s = summarize(e.closed)
    assert s["n"] == 0 and s["excluded_gap"] == 1 and s["status"] == "INSUFFICIENT"
