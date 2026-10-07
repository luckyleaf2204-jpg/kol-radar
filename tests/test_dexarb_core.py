"""Detection, quoting, costs and paper model A on deterministic fakes."""
import json

import pytest

from dexarb_fakes import CBBTC, SOL, USDC, VENUES, WETH, FakeEvm, FakeJupiter
from dexarb import config as C
from dexarb import engine as E
from dexarb.app import Lab
from dexarb.fees import Cost, EvmFees, SolanaFees, to_quote
from dexarb.protocols import Quote, evm_quotes, jupiter_quote
from dexarb.registry import ATOMIC, CHAINS
from dexarb.rpc import Rpc
from dexarb.store import Store

VER_ALL = {f"{c}/{p.key}": {"ok": True} for c, ch in CHAINS.items() for p in ch.protocols}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def lab(tmp_path, chains=("base",), evm=None, jup=None, clk=None):
    evm = evm or FakeEvm()
    clk = clk or Clock()
    st = Store(tmp_path / "dexarb.db", clock=clk)
    lb = Lab(st, list(chains), rpc_factory=lambda c: Rpc("fake", transport=evm.transport), jup_get=jup or FakeJupiter(),
             clock=clk, log=lambda *_: None, ver=VER_ALL, sleep=lambda s: None)
    return lb, st, evm, clk


def q(chain, proto, tin, tout, ain, aout, t, ctx=10, impact=0.001):
    return Quote(chain, proto, tin, tout, ain, aout, context=ctx, impact=impact, fetched_at=t)


# --- detection -------------------------------------------------------------------------------------------------------
def test_sell_quote_uses_exact_buy_output_and_pairs_are_directional(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)            # WETH cheaper on uniswap_v2 by 2 %
    lb.scan("base")
    rows = st.db.execute("SELECT buy_protocol, sell_protocol, tokens_mid, amount_out, gross_spread, status, reason, "
                         "buy_quote_id, sell_quote_id FROM opportunities WHERE token='WETH' AND size=1000").fetchall()
    fwd = [r for r in rows if r[0] == "uniswap_v2"]
    back = [r for r in rows if r[1] == "uniswap_v2"]
    assert fwd and all(r[4] > 0 for r in fwd) and all(r[5] == "CANDIDATE" for r in fwd)
    assert not back or all(r[4] <= 0 for r in back)                # the reverse direction is its own (losing) cycle
    b_in = st.db.execute("SELECT amount_out FROM quote_snapshots WHERE id=?", (fwd[0][7],)).fetchone()[0]
    s_in = st.db.execute("SELECT amount_in FROM quote_snapshots WHERE id=?", (fwd[0][8],)).fetchone()[0]
    assert b_in == s_in                                             # sell input = exact raw buy output
    assert fwd[0][2] == pytest.approx(int(b_in) / 1e18)


def test_rejections_chain_token_stale_context_impact_unknown():
    t = 100.0
    ok = Cost(0.01, "ESTIMATED", "x")
    b = q("base", "uniswap_v2", "USDC", "WETH", 10 ** 9, 4 * 10 ** 17, t)
    s = q("base", "uniswap_v3", "WETH", "USDC", 4 * 10 ** 17, 1_010 * 10 ** 6, t)
    ev = lambda bq, sq, now=t, g=ok: E.evaluate_cycle("base", WETH, 1000, bq, sq, g, g, ok, now)["reason"]  # noqa
    assert ev(b, s) == ""
    assert ev(q("polygon", "uniswap_v2", "USDC", "WETH", 10 ** 9, 4 * 10 ** 17, t), s) == "chain_mismatch"
    assert ev(b, q("base", "uniswap_v3", "WETH", "USDC", 3 * 10 ** 17, 10 ** 9, t)) == "token_mismatch"
    assert ev(b, s, now=t + 6) == "quote_stale"
    assert ev(b, q("base", "uniswap_v3", "WETH", "USDC", 4 * 10 ** 17, 10 ** 9, t, ctx=5)) == "context_mismatch"
    assert ev(q("base", "uniswap_v2", "USDC", "WETH", 10 ** 9, 4 * 10 ** 17, t, impact=0.02), s) == "impact_high"
    assert ev(b, s, g=Cost(None, "UNKNOWN", "no sample")) == "cost_unknown"
    assert ev(b, q("base", "uniswap_v3", "WETH", "USDC", 4 * 10 ** 17, 999 * 10 ** 6, t)) == "negative_spread"
    assert ev(b, q("base", "uniswap_v3", "WETH", "USDC", 4 * 10 ** 17, 1_000_500_000, t)) == "below_threshold"
    nf = Quote("base", "uniswap_v2", "USDC", "WETH", 10 ** 9, None, "NO_ROUTE")
    assert ev(nf, s) == "no_route"


def test_net_formula_and_buffer():
    t, g = 100.0, Cost(0.30, "ESTIMATED", "gas")
    b = q("base", "uniswap_v2", "USDC", "WETH", 10 ** 9, 4 * 10 ** 17, t)
    s = q("base", "uniswap_v3", "WETH", "USDC", 4 * 10 ** 17, 1_010 * 10 ** 6, t)
    r = E.evaluate_cycle("base", WETH, 1000, b, s, g, g, Cost(0.10, "SIMULATED", "approve"), t)
    assert r["gross_spread"] == pytest.approx(10.0) and r["estimated_costs"] == pytest.approx(0.70)
    assert r["net_profit_estimate"] == pytest.approx(9.30)
    assert r["uncertainty_buffer"] == pytest.approx(1000 * 0.001 + 0.5 * 0.70)
    assert r["net_profit_after_buffer"] == pytest.approx(9.30 - 1.35) and r["status"] == "CANDIDATE"


def test_decimals_and_wrapped_native():
    assert E.raw(1.5, CBBTC) == 150_000_000 and E.human(150_000_000, CBBTC) == 1.5
    assert E.raw(1000, USDC) == 10 ** 9 and E.raw(1, WETH) == 10 ** 18
    assert CHAINS["base"].wrapped.address == WETH.address and CHAINS["solana"].wrapped.symbol == "SOL"


# --- quotes ----------------------------------------------------------------------------------------------------------
def test_evm_quotes_batch_route_tier_impact_and_context():
    evm = FakeEvm()
    rpc = Rpc("fake", transport=evm.transport)
    protos = list(CHAINS["base"].protocols)
    qs = evm_quotes(rpc, "base", protos, [(p.key, USDC, WETH, 10 ** 9) for p in protos])
    assert all(x.ok for x in qs) and evm.calls.count("eth_blockNumber") == 1 and rpc.timing["batch"][0] == 1
    v3 = next(x for x in qs if x.protocol == "uniswap_v3")
    assert v3.route[0]["fee_tier"] == 500 and v3.context == 1000 and 0 < v3.impact < 0.01
    assert "INCLUDED_IN_QUOTE" in v3.fee_note
    from dexarb.registry import Asset
    nr = evm_quotes(rpc, "base", protos, [("uniswap_v2", USDC, Asset("XYZ", "0x" + "ee" * 20, 18), 10 ** 9)])[0]
    assert nr.status == "NO_ROUTE"


def test_jupiter_single_dex_route_check():
    jup = FakeJupiter()
    p = CHAINS["solana"].protocols[2]                                # Whirlpool
    ok = jupiter_quote(p, SOL.quote_asset, SOL.tokens[0], 10 ** 9, get=jup)
    assert ok.ok and ok.route[0]["venue"] == "Whirlpool" and ok.context and "onlyDirectRoutes=true" in jup.calls[0]
    jup.wrong_label.add("Whirlpool")
    bad = jupiter_quote(p, SOL.quote_asset, SOL.tokens[0], 10 ** 9, get=jup)
    assert bad.status == "NO_ROUTE" and "route_not_on_dex" in bad.error


# --- costs -----------------------------------------------------------------------------------------------------------
def test_evm_costs_provenance_and_conversion():
    evm = FakeEvm()
    f = EvmFees("base", Rpc("fake", transport=evm.transport))
    gp = f.gas_price()
    assert gp.status == "MEASURED" and gp.native == pytest.approx(1.1e-9)
    p = CHAINS["base"].protocols[0]
    c = f.swap_cost(p, USDC.address, WETH.address, None, gp)
    assert c.status == "ESTIMATED" and c.native == pytest.approx(150_000 * 1.1e-9) and "median gasUsed" in c.source
    a = f.approval_cost(USDC.address, p.address, gp)
    assert a.status == "SIMULATED" and a.native == pytest.approx(46_000 * 1.1e-9)
    px = Cost(3000.0, "MEASURED", "quoted")
    assert to_quote(c, px).native == pytest.approx(150_000 * 1.1e-9 * 3000)
    assert to_quote(c, Cost(None, "UNKNOWN", "stale")).status == "UNKNOWN"     # no price -> no conversion
    depeg = to_quote(c, Cost(3000.0 / 0.97, "MEASURED", "USDC at 0.97"))      # quote asset depegged: more units
    assert depeg.native > to_quote(c, px).native


def test_solana_costs():
    class R:
        def call(self, m, p):
            return [{"slot": i, "prioritizationFee": v} for i, v in enumerate((0, 10, 20))] \
                if m == "getRecentPrioritizationFees" else 2_039_280
    f = SolanaFees(R())
    c = f.swap_cost()
    assert c.status == "ESTIMATED" and c.native == pytest.approx((5000 + 10 * 1_400_000 / 1e6) / 1e9)
    assert f.ata_rent().status == "MEASURED" and f.ata_rent().native == pytest.approx(0.00203928)


def test_approval_and_rent_charged_once_per_paper_account(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    lb.native_price("base", list(CHAINS["base"].protocols))
    arm = lb.arms["A_fast"]
    qq = q("base", "uniswap_v2", "USDC", "WETH", 10 ** 9, 4 * 10 ** 17, clk())
    _, _, s1 = lb.leg_costs("base", "uniswap_v2", USDC, WETH, qq, arm)
    arm._mark_setup("base", "uniswap_v2", USDC)
    _, _, s2 = lb.leg_costs("base", "uniswap_v2", USDC, WETH, qq, arm)
    assert s1.native > 0 and s1.status == "SIMULATED" and s2.native == 0


# --- paper model A ---------------------------------------------------------------------------------------------------
def run_until(lb, clk, seconds, step=1.0):
    end = clk.t + seconds
    while clk.t < end:
        clk.t += step
        lb.tick()


def cycles(st):
    return st.db.execute("SELECT status, reason, net, received, spent, gas_cost, baseline FROM paper_cycles "
                         "WHERE baseline=0 ORDER BY id").fetchall()


def test_sequential_cycle_closes_with_fresh_quotes_and_fees(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, 30)
    cs = cycles(st)
    closed = [c for c in cs if c[0] == "CLOSED"]
    assert closed and all(c[5] > 0 for c in closed)                # gas of both legs charged
    legs = st.db.execute("SELECT leg, status, exec_quote_id FROM paper_legs").fetchall()
    assert {(lg, s) for lg, s, _ in legs} >= {(1, "FILLED"), (2, "FILLED")}
    assert len({qid for _, _, qid in legs}) == len(legs)            # every leg used its own fresh quote
    bal = dict(st.db.execute("SELECT token, amount FROM paper_balances WHERE account='A:A_fast' AND chain='base'"))
    assert bal["WETH"] == pytest.approx(0, abs=1e-12) and bal["ETH"] < C.NATIVE_FLOAT_QUOTE / 1   # gas spent in ETH


def test_opportunity_gone_after_latency_is_aborted_without_cost(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    evm.price(VENUES["uniswap_v2"], WETH.address, 1 / 0.98)          # gap closes before leg 1
    run_until(lb, clk, 10)
    cs = cycles(st)
    assert cs and all(c[0] == "ABORTED" for c in cs if c[1] != "max_open_token")
    assert any(c[1] == "opportunity_gone_after_latency" for c in cs)
    assert st.db.execute("SELECT COUNT(*) FROM paper_legs").fetchone()[0] == 0


def test_leg2_revert_open_exposure_then_retry_with_new_quote(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, C.ARMS["A_fast"][0] + 0.5)                     # leg 1 filled for the fast arm
    for v in VENUES.values():
        evm.price(v, WETH.address, 0.9)                              # price drops 10 % before leg 2 executes
    run_until(lb, clk, C.ARMS["A_fast"][1] + 1)
    st_ = [r[0] for r in st.db.execute("SELECT status FROM paper_cycles WHERE account='A:A_fast' AND baseline=0")]
    assert "OPEN_EXPOSURE" in st_
    rev = st.db.execute("SELECT status, gas_native FROM paper_legs WHERE leg=2 AND status='REVERTED'").fetchall()
    assert rev and all(g > 0 for _, g in rev)                         # a reverted tx still pays gas
    assert st.db.execute("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'").fetchone()[0] >= 1
    run_until(lb, clk, 2 * C.RETRY_S + 5)                             # retries take the new price: closes at a loss
    closed = st.db.execute("SELECT net FROM paper_cycles WHERE account='A:A_fast' AND status='CLOSED' AND baseline=0"
                           ).fetchall()
    assert closed and closed[0][0] < 0


def test_low_balance_and_max_exposure(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CAPITAL", 1500.0)         # one 1000-size cycle fits; the second one must wait
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    evm.price(VENUES["uniswap_v2"], CBBTC.address, 0.98)
    lb.scan("base")
    reasons = [r[0] for r in st.db.execute("SELECT reason FROM paper_cycles WHERE status='SKIPPED'")]
    assert "insufficient_paper_balance" in reasons and "max_open_token" in reasons
    run_until(lb, clk, 60)
    assert min(r[0] for r in st.db.execute("SELECT amount FROM paper_balances")) >= 0     # never overdrawn


def test_duplicate_opportunity_creates_no_second_cycle(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    n = st.db.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0]
    oid = st.db.execute("SELECT id FROM opportunities WHERE status='CANDIDATE' LIMIT 1").fetchone()[0]
    r = {"chain": "base", "token": "WETH", "size": 1000, "buy_protocol": "uniswap_v2", "sell_protocol": "uniswap_v3",
         "detected_at": clk(), "net_profit_after_buffer": 1.0}
    assert lb.arms["A_fast"].on_candidate(oid, r) is None and st.db.execute(
        "SELECT COUNT(*) FROM paper_cycles").fetchone()[0] == n


def test_no_lookahead_stored_opportunity_never_rewritten(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    snap = st.db.execute("SELECT * FROM opportunities ORDER BY id").fetchall()
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.5)               # the future changes a lot
    run_until(lb, clk, 60)
    assert st.db.execute("SELECT * FROM opportunities WHERE id <= ? ORDER BY id", (snap[-1][0],)).fetchall() == snap


def test_rpc_outage_records_gap_and_creates_nothing(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.fail = 100
    lb.scan("base")
    assert st.db.execute("SELECT COUNT(*) FROM opportunities WHERE status='CANDIDATE'").fetchone()[0] == 0
    assert st.db.execute("SELECT reason FROM data_gaps").fetchall()
    h = dict(((r[0], r[1]), r[2]) for r in st.db.execute("SELECT chain, protocol, status FROM feed_health"))
    assert not h or "UNAVAILABLE" in h.values()


def test_baseline_cycles_and_atomic_not_supported(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    lb.scan("base")                                                  # no edge at all: baselines still run
    run_until(lb, clk, 40)
    b = st.db.execute("SELECT COUNT(*), SUM(status='CLOSED') FROM paper_cycles WHERE baseline=1").fetchone()
    assert b[0] == len(C.ARMS) and b[1] == len(C.ARMS)
    assert all(v.startswith("NOT_SUPPORTED") for v in ATOMIC.values())


def test_solana_scan_end_to_end(tmp_path):
    jup = FakeJupiter()
    jup.price[("Whirlpool", "USDC", "JUP")] = 2.05                   # JUP 2.5 % cheaper on Whirlpool

    class SolRpc:
        calls = 0

        def call(self, m, p):
            return [{"slot": 1, "prioritizationFee": 0}] if m == "getRecentPrioritizationFees" else 2_039_280
    clk = Clock()
    st = Store(tmp_path / "dexarb.db", clock=clk)
    lb = Lab(st, ["solana"], rpc_factory=lambda c: SolRpc(), jup_get=jup, clock=clk, log=lambda *_: None,
             ver=VER_ALL, sleep=lambda s: None)
    lb.scan("solana")
    c = st.db.execute("SELECT buy_protocol, cost_detail, setup_cost FROM opportunities WHERE status='CANDIDATE'"
                      ).fetchall()
    assert c and all(x[0] == "orca_whirlpool" for x in c)
    assert all("rent" in json.loads(x[1])["setup"] for x in c) and all(x[2] > 0 for x in c)
    assert all("onlyDirectRoutes=true" in u for u in jup.calls)


def test_gas_fallback_to_same_protocol_median_when_pool_is_idle():
    evm = FakeEvm()
    f = EvmFees("base", Rpc("fake", transport=evm.transport))
    p = CHAINS["base"].protocols[0]
    gp = f.gas_price()
    assert f.swap_cost(p, USDC.address, WETH.address, None, gp).status == "ESTIMATED"      # sampled pool
    f.swap_gas[(p.key, "0xidle")] = ((None, 0.0, "no swap in pool 0xidle in the last 400 blocks"), f.clock())
    units, _, src = f.swap_gas_units(p, "0xidle")
    assert units == 150_000 and "fallback" in src
    other = CHAINS["base"].protocols[2]
    f.swap_gas[(other.key, "0xidle2")] = ((None, 0.0, "no swap"), f.clock())
    assert f.swap_gas_units(other, "0xidle2")[0] is None                                # no sample of that protocol


# --- unsigned-swap simulation (state override, read-only) -------------------------------------------------------------
VER_SIM = {k: dict(v, sim_ok=True) for k, v in VER_ALL.items()}


def lab_sim(tmp_path, ver=None):
    evm, clk = FakeEvm(), Clock()
    st = Store(tmp_path / "dexarb.db", clock=clk)
    lb = Lab(st, ["base"], rpc_factory=lambda c: Rpc("fake", transport=evm.transport), clock=clk,
             log=lambda *_: None, ver=ver or VER_SIM, sleep=lambda s: None)
    return lb, st, evm, clk


def test_simulator_finds_storage_layout_and_matches_quote():
    from dexarb.simulate import Simulator
    evm = FakeEvm()
    rpc = Rpc("fake", transport=evm.transport)
    sim = Simulator("base", rpc)
    assert sim.find_slots(USDC.address) == (9, 10)
    p = CHAINS["base"].protocols[0]
    q = evm_quotes(rpc, "base", [p], [(p.key, USDC, WETH, 10 ** 9)])[0]
    r = sim.swap(p, USDC, WETH, 10 ** 9, 0, None)
    assert r.status == "SIMULATED_OK" and r.amount_out == q.amount_out and r.gas_units == 121_000
    assert sim.swap(p, USDC, WETH, 10 ** 9, q.amount_out + 1, None).status == "SIM_REVERT"   # min-out enforced
    evm.layout_known = False
    assert Simulator("base", rpc).swap(p, USDC, WETH, 10 ** 9, 0, None).status == "UNAVAILABLE"


def test_paper_legs_are_simulated_when_verified(tmp_path):
    lb, st, evm, clk = lab_sim(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, 30)
    notes = [r[0] for r in st.db.execute("SELECT note FROM paper_legs WHERE status='FILLED'")]
    assert notes and all(n.startswith("fill_basis=SIMULATED sim_id=") for n in notes)
    sims = st.db.execute("SELECT status, detail FROM simulation_results").fetchall()
    assert sims and all(s == "SIMULATED_OK" for s, _ in sims)
    g = st.db.execute("SELECT cost_status, gas_native FROM paper_legs WHERE status='FILLED'").fetchall()
    assert all(c == "SIMULATED" for c, _ in g) and all(x == pytest.approx(121_000 * 1.1e-9) for _, x in g)


def test_simulated_revert_charges_gas_and_keeps_position_open(tmp_path):
    lb, st, evm, clk = lab_sim(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, C.ARMS["A_fast"][0] + 0.5)
    for v in VENUES.values():
        evm.price(v, WETH.address, 0.9)
    run_until(lb, clk, C.ARMS["A_fast"][1] + 1)
    rev = st.db.execute("SELECT note, gas_native FROM paper_legs WHERE leg=2 AND status='REVERTED'").fetchall()
    assert rev and all("fill_basis=SIMULATED revert" in n and g > 0 for n, g in rev)
    assert st.db.execute("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'").fetchone()[0] >= 1


def test_unverified_simulation_stays_quote_only(tmp_path):
    lb, st, evm, clk = lab_sim(tmp_path, ver=VER_ALL)            # live check never confirmed the simulation
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, 30)
    notes = [r[0] for r in st.db.execute("SELECT note FROM paper_legs WHERE status='FILLED'")]
    assert notes and all(n.startswith("fill_basis=QUOTE_ONLY") for n in notes)
    assert st.db.execute("SELECT COUNT(*) FROM simulation_results").fetchone()[0] == 0


def test_18_decimal_simulation_amount_is_stored(tmp_path):
    from dexarb.simulate import SimResult
    lb, st, evm, clk = lab_sim(tmp_path)
    arm = lb.arms["A_fast"]
    arm.simulate = lambda *a: (SimResult("SIMULATED_OK", 10 ** 21, 120_000, "", "0xr", clk()), None, None)
    qq = Quote("base", "uniswap_v2", "WETH", "USDC", 10 ** 21, 10 ** 21, fetched_at=clk())
    ok, got, *_ = arm._fill("base", "uniswap_v2", WETH, WETH, qq, 0.0, Cost(1, "ESTIMATED", ""), Cost(1, "ESTIMATED", ""))
    assert ok and got == 1000.0 and st.db.execute("SELECT value FROM simulation_results").fetchone()[0] == 1e21


def test_a_failing_event_is_retried_not_fatal(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    arm = lb.arms["A_fast"]
    real = arm._leg2
    calls = {"n": 0}

    def flaky(cid, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return real(cid, now)
    arm._leg2 = flaky
    run_until(lb, clk, 120)
    assert st.db.execute("SELECT COUNT(*) FROM audit_events WHERE kind='paper_error'").fetchone()[0] == 1
    assert st.db.execute("SELECT COUNT(*) FROM paper_cycles WHERE account='A:A_fast' AND status='CLOSED' AND "
                         "baseline=0").fetchone()[0] >= 1
