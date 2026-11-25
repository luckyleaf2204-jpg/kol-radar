"""Gate B3 / B4: wallet history + PnL with its own budget and caps (off by default), cohorts DEPTH / STANK first and
INSIDERS apart, the old PnL as a reference only. Fake RPCs only: a real network call fails the test."""
import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from insiders import track as T
from insiders import wallets as WH

SOL = 10 ** 9
A = "7GSMwnWB5YWmCwQax4bzfeDMvSZEogp6TAehxrJddDyr"
B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
C = "9qhkWvC7K2wtsn3H8sbEWAGv82LD12uKxmyN7YdmxS8D"
D = "Cm6fNnMk7NfzStP9CZpsQA2v3jjzbcYGAxdJySmHpump"
COIN = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"
COIN2 = "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call in a Gate B test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def result():
    return {"vip": {"external": [{"wallet": A, "group": "DEPTH", "role": "dev"}, {"wallet": B, "group": "STANK", "role": "kol"}],
                    "holders": [{"wallet": C, "profit_sol": 3.0}, {"wallet": A, "profit_sol": 1.0}],
                    "successors": [{"wallet": D, "kind": "person"}, {"wallet": COIN2, "kind": "deposit"}]},
            "pnl": {"wallets": [{"wallet": C, "net_sol": 12.5, "spent_sol": 2, "received_sol": 14.5, "coins": 1, "wins": 1}]}}


def tx(sig, ts, wallet, mint, tok, lamports, fee=5000, signer=True):
    keys = [{"pubkey": wallet}] if signer else [{"pubkey": "11111111111111111111111111111111"}, {"pubkey": wallet}]
    i = 0 if signer else 1
    pre_b, post_b = [0] * len(keys), [0] * len(keys)
    pre_b[i], post_b[i] = lamports
    return {"blockTime": ts, "meta": {"err": None, "fee": fee, "logMessages": [], "innerInstructions": [],
                                      "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(tok[0])}}],
                                      "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(tok[1])}}],
                                      "preBalances": pre_b, "postBalances": post_b},
            "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": []}}}


class Chain:
    def __init__(self):
        self.sigs, self.txs, self.calls = {}, {}, []

    def __call__(self, method, params):
        self.calls.append((method, params[0]))
        if method == "getTransaction":
            return self.txs.get(params[0])
        sigs, o = self.sigs.get(params[0], []), params[1]
        names = [s["signature"] for s in sigs]
        if o.get("before"):
            sigs = sigs[names.index(o["before"]) + 1:] if o["before"] in names else []
        return sigs[:o.get("limit", 1000)]


def history(tmp_path, chain=None, **kw):
    st = T.Store(tmp_path / "t.db", now=lambda: 1_000_000.0)
    kw.setdefault("enabled", True)
    kw.setdefault("budget", T.Budget(10_000))
    return WH.WalletHistory(st, chain or Chain(), now=lambda: 1_000_000.0, **kw)


# ------------------------------------------------------------------------------------------------------------ B4
def test_cohorts_keep_depth_stank_first_and_insiders_apart():
    cs = {c["wallet"]: c for c in WH.cohorts(result())}
    assert cs[A]["cohort"] == "DEPTH" and "holder" in cs[A]["source"]        # in two lists: DEPTH wins, both kept
    assert cs[B]["cohort"] == "STANK"
    assert cs[C]["cohort"] == cs[D]["cohort"] == "INSIDERS"
    assert cs[COIN2]["skip"] and not cs[D]["skip"]                          # a deposit address is listed, not read


def test_real_lists_give_81_depth_39_stank_and_an_insiders_cohort():
    r = json.loads((Path(__file__).parent.parent / "insiders" / "result.json").read_text(encoding="utf-8"))
    by = {}
    for c in WH.cohorts(r):
        by[c["cohort"]] = by.get(c["cohort"], 0) + 1
    assert by["DEPTH"] == 81 and by["STANK"] == 39
    v = r["vip"]
    insiders = {h["wallet"] for h in v["holders"]} | {s["wallet"] for s in v["successors"]}
    base = {e["wallet"] for e in v["external"]}
    assert by["INSIDERS"] == len(insiders - base) and by["INSIDERS"] > 0


def test_seed_once_legacy_is_reference_only_and_nothing_verified(tmp_path):
    h = history(tmp_path)
    assert h.seed(result()) == 5
    h.s.x("UPDATE wallet_queue SET state='done', before_sig='KEEP' WHERE wallet=?", (C,))
    assert h.seed(result()) == 0                                            # a second seed changes nothing
    assert h.s.q("SELECT state, before_sig FROM wallet_queue WHERE wallet=?", (C,))[0] == {"state": "done", "before_sig": "KEEP"}
    sm = h.summary(C)
    assert sm["legacy"]["source"] == "legacy_pnl_95coins" and sm["legacy"]["net_sol"] == 12.5
    assert sm["verified"] is False and sm["realized_sol"] == WH.INSUFFICIENT  # the old figure is never the new one
    assert sm["cohort"] == "INSIDERS"
    assert h.cohort_counts() == {"DEPTH": {"todo": 1}, "STANK": {"todo": 1}, "INSIDERS": {"done": 1, "todo": 1, "skipped": 1}}


# ------------------------------------------------------------------------------------------------------------ B3
def test_worker_is_off_by_default_and_spends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("INSIDER_WALLET_HISTORY", raising=False)

    def refuse(m, p):
        raise AssertionError("RPC call with the worker off")
    st = T.Store(tmp_path / "t.db")
    h = WH.WalletHistory(st, refuse)
    assert h.enabled is False and h.budget.per_min == 15
    h.seed(result())
    for _ in range(5):
        h.tick()
    assert st.q("SELECT COUNT(*) n FROM rpc_usage")[0]["n"] == 0


def test_depth_and_stank_are_read_before_insiders(tmp_path):
    chain = Chain()
    h = history(tmp_path, chain)
    h.seed(result())
    order = []
    for _ in range(4):
        h.step_sigs()
        order.append(chain.calls[-1][1])
    assert order[:2] == sorted([A, B]) and set(order[2:]) == {C, D}           # the deposit address is never read


def test_paging_full_history_and_the_page_cap_is_partial_with_a_gap(tmp_path, monkeypatch):
    chain = Chain()
    chain.sigs[A] = [{"signature": f"a{i}", "blockTime": 5000 - i, "err": None} for i in range(2500)]
    h = history(tmp_path, chain)
    h.s.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (A,))
    h.s.x("INSERT INTO wallet_queue (wallet, priority, state) VALUES (?, 0, 'todo')", (A,))
    for _ in range(3):
        h.step_sigs()
    q = h.s.q("SELECT * FROM wallet_queue WHERE wallet=?", (A,))[0]
    assert (q["state"], q["coverage"], q["sigs"], q["pages"]) == ("parsing", "full", 2500, 3)
    assert h.s.q("SELECT COUNT(*) n FROM wallet_sigs")[0]["n"] == 2500
    monkeypatch.setattr(WH, "WALLET_MAX_PAGES", 2)
    h.s.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (B,))
    h.s.x("INSERT INTO wallet_queue (wallet, priority, state) VALUES (?, 0, 'todo')", (B,))
    h.s.x("UPDATE wallet_queue SET state='done' WHERE wallet=?", (A,))
    chain.sigs[B] = [{"signature": f"b{i}", "blockTime": 5000 - i, "err": None} for i in range(2500)]
    h.step_sigs()
    h.step_sigs()
    q = h.s.q("SELECT * FROM wallet_queue WHERE wallet=?", (B,))[0]
    assert q["coverage"].startswith("partial") and q["state"] == "parsing"
    g = h.s.gaps(scope="wallet", address=B)
    assert len(g) == 1 and g[0]["before_sig"] == "b1999"


def test_pnl_average_cost_realised_fees_apart_and_unrealised_needs_a_price(tmp_path):
    chain = Chain()
    chain.sigs[A] = [{"signature": s, "blockTime": t, "err": None} for s, t in
                     (("sell", 400), ("send", 300), ("buy2", 200), ("buy1", 100))]
    chain.txs = {"buy1": tx("buy1", 100, A, COIN, (0, 1000), (10 * SOL, 9 * SOL)),          # 1 SOL -> 1000
                 "buy2": tx("buy2", 200, A, COIN, (1000, 2000), (9 * SOL, 6 * SOL)),        # 3 SOL -> 1000
                 "send": tx("send", 300, A, COIN, (2000, 1500), (6 * SOL, 6 * SOL), signer=False),   # moved, not sold
                 "sell": tx("sell", 400, A, COIN, (1500, 500), (6 * SOL, 10 * SOL))}       # 1000 for 4 SOL
    h = history(tmp_path, chain)
    h.s.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (A,))
    h.s.x("INSERT INTO wallet_queue (wallet, priority, state) VALUES (?, 0, 'todo')", (A,))
    h.step_sigs()
    for _ in range(3):
        h.step_parse()
    p = h.s.q("SELECT * FROM wallet_pnl WHERE wallet=?", (A,))[0]
    assert (p["buys"], p["sells"], p["sends"]) == (2, 1, 1)
    assert p["sol_in"] == 4.0 and p["sol_out"] == 4.0
    assert p["cost_sold_sol"] == pytest.approx(2.0) and p["realized_sol"] == pytest.approx(2.0)   # avg cost 2 SOL / 1000
    assert p["fees_sol"] == pytest.approx(3 * 5000 / SOL)                   # the send was paid by someone else
    assert p["tok_left"] == "500" and p["unrealized_sol"] is None
    assert p["status"] == WH.INSUFFICIENT and "giá" in p["reason"]
    sm = h.summary(A)
    assert sm["realized_sol"] == WH.INSUFFICIENT and sm["multiples_2x_10x"] == WH.INSUFFICIENT
    h.price = lambda m: (0.004, "test-quote")                               # with a named price: unrealised
    p = h.compute(A)[0]
    assert p["status"] == "OK" and p["unrealized_sol"] == pytest.approx(500 * 0.004 - 1.0) and p["price_src"] == "test-quote"
    sm = h.summary(A)
    assert sm["realized_sol"] == pytest.approx(2.0) and sm["roi"] == pytest.approx(1.0) and sm["win_rate"] == 1.0


def test_selling_received_tokens_has_no_guessed_cost():
    trades = [{"kind": "RECEIVE", "tokens": 1000, "sol": 0, "ts": 1},
              {"kind": "SELL", "tokens": 1000, "sol": 5.0, "ts": 2}]
    r = WH.pair_pnl(trades)
    assert r["status"] == WH.INSUFFICIENT and "không rõ giá vốn" in r["reason"]
    assert r["realized_sol"] == 0 and r["sol_out"] == 5.0 and r["buys"] == 0   # a receive is not a buy


def test_a_partial_history_makes_every_pair_insufficient(tmp_path):
    h = history(tmp_path)
    h.s.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (A,))
    h.s.x("INSERT INTO wallet_queue (wallet, priority, state, coverage) VALUES (?, 0, 'done', 'partial (10 trang x 1000)')", (A,))
    cols = "INSERT INTO wallet_trades (wallet, sig, mint, kind, tokens, sol, fee_sol, ts) VALUES "
    h.s.x(cols + "(?, 's1', ?, 'BUY', '10', 1.0, 0, 1)", (A, COIN))
    h.s.x(cols + "(?, 's2', ?, 'SELL', '10', 2.0, 0, 2)", (A, COIN))
    p = h.compute(A)[0]
    assert p["status"] == WH.INSUFFICIENT and "chưa đầy đủ" in p["reason"]


def test_daily_cap_is_a_safe_stop_and_the_budget_is_its_own(tmp_path):
    chain = Chain()
    h = history(tmp_path, chain, day_cap=3, budget=T.Budget(15))
    h.seed(result())
    for _ in range(10):
        h.tick()
    assert len(chain.calls) == 3 and "trần ngày" in h.stat["safe_stop"]
    assert h.s.q("SELECT SUM(calls) n FROM rpc_usage WHERE worker='wallets'")[0]["n"] == 3
    h2 = history(tmp_path / "x", Chain(), budget=None)
    assert h2.budget.per_min == WH.WALLET_CALLS_PER_MIN == 15


def test_paused_means_no_call(tmp_path):
    def refuse(m, p):
        raise AssertionError("RPC call while paused")
    h = history(tmp_path, refuse, paused=lambda: True)
    h.seed(result())
    h.tick()


def test_api_history_and_cohorts(tmp_path):
    from insiders import site
    h = history(tmp_path)
    h.seed(result())

    class Svc:
        tracker = T.Tracker(h.s, lambda m, p: None)
        wallet_history = h
    code, body = site.track_get(Svc, "/api/insiders/track/cohorts", "/api/insiders/track/cohorts")
    assert code == 200 and body["enabled"] is True and "INSIDERS" in body["cohorts"]
    code, body = site.track_get(Svc, "/api/insiders/track/history", f"/api/insiders/track/history?wallet={C}")
    assert code == 200 and body["cohort"] == "INSIDERS" and body["legacy"]["source"] == "legacy_pnl_95coins"
    assert site.track_get(Svc, "/api/insiders/track/history", "/api/insiders/track/history?wallet=bad")[0] == 400


def test_service_seeds_cohorts_without_starting_the_worker(tmp_path, monkeypatch):
    from insiders import service as SV
    monkeypatch.delenv("INSIDER_WALLET_HISTORY", raising=False)
    calls = []
    svc = SV.InsiderService.__new__(SV.InsiderService)
    svc.result = result()
    stop = threading.Event()
    stop.set()                                             # nothing loops
    svc.start_track(tmp_path / "insiders_track.db", rpc=lambda m, p: calls.append(m), stop=stop)
    assert svc.wallet_history.enabled is False
    assert svc.wallet_history.cohort_counts()["DEPTH"] == {"todo": 1}
    assert calls == []
