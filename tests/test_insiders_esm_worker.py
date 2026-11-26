"""Early Smart Money worker (ON by default since 2026-11-26, INSIDER_ESM=0 = off). Fake chain only, no network, no thread."""
import json
from pathlib import Path

from insiders import esm_worker as EW
from insiders import track as T

MINT = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"
DEV = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
BUYER = "5uGbn3xfFEgmy3qhLmXPUGeBQee8hwTuAnHCktTfFRXC"
T0 = 1_000_000


def bal(owner, amount):
    return {"owner": owner, "mint": MINT, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}


def buy_tx(wallet, ts, sol, tokens, sig):
    return {"blockTime": ts, "slot": ts, "meta": {"err": None, "fee": 5000, "preBalances": [10 * 10**9], "postBalances": [10 * 10**9 - int(sol * 1e9)],
                                                  "preTokenBalances": [], "postTokenBalances": [bal(wallet, tokens)], "innerInstructions": [], "logMessages": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": wallet, "signer": True}], "instructions": []}}}


def create_tx():
    return {"blockTime": T0, "slot": 1, "meta": {"err": None, "fee": 5000, "preBalances": [10**10], "postBalances": [10**10 - 20_000_000],
                                                 "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": [], "logMessages": []},
            "transaction": {"signatures": ["c"], "message": {"accountKeys": [{"pubkey": DEV, "signer": True}], "instructions": [
                {"program": "spl-token", "parsed": {"type": "initializeMint2", "info": {"mint": MINT}}}]}}}


class Chain:
    """Newest first, like getSignaturesForAddress."""
    def __init__(self, txs, unserved=()):
        self.txs = txs                                       # sig -> tx
        self.order = sorted(txs, key=lambda s: -txs[s]["blockTime"])
        self.unserved, self.calls = set(unserved), []

    def __call__(self, method, params):
        self.calls.append((method, params[0]))
        if method == "getSignaturesForAddress":
            opts = params[1]
            i = self.order.index(opts["before"]) + 1 if opts.get("before") else 0
            return [{"signature": s, "blockTime": self.txs[s]["blockTime"], "err": None} for s in self.order[i:i + opts["limit"]]]
        if method == "getTransaction":
            return None if params[0] in self.unserved else self.txs[params[0]]
        raise AssertionError(method)


def chain(**kw):
    txs = {"c": create_tx(), "b1": buy_tx(BUYER, T0 + 60, 1.0, 1_000_000_000, "b1"), "b2": buy_tx(DEV, T0 + 1200, 0.5, 400_000_000, "b2"),
           "b3": buy_tx(BUYER, T0 + 3000, 1.0, 500_000_000, "b3"), "late": buy_tx(BUYER, T0 + 9000, 5.0, 10, "late")}
    return Chain(txs, **kw)


def store(tmp_path, verified="on-chain"):
    st = T.Store(tmp_path / "t.db")
    st.x("INSERT INTO mints (mint, name, verified) VALUES (?, 'X', ?)", (MINT, verified))
    return st


def run(w, rounds=20):
    for _ in range(rounds):
        w.step()


def test_off_when_asked_and_on_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("INSIDER_ESM", "0")
    st, c = store(tmp_path), chain()
    w = EW.EsmWorker(st, c)
    assert w.add(MINT)
    assert w.step() == {} and c.calls == []
    assert not EW.enabled()
    monkeypatch.delenv("INSIDER_ESM")
    assert EW.enabled()


def test_the_service_does_not_start_it():
    src = (Path(__file__).resolve().parents[1] / "insiders" / "service.py").read_text(encoding="utf-8")
    assert "esm_worker" not in src and "EsmWorker" not in src


def test_unverified_mint_is_not_queued(tmp_path):
    st = store(tmp_path, verified=None)
    assert not EW.EsmWorker(st, chain(), on=True).add(MINT)


def test_full_window_and_nothing_after_minute_120(tmp_path, monkeypatch):
    monkeypatch.setattr(EW, "PAGE", 2)
    now = [T0 + 3600]
    st, c = store(tmp_path), chain()
    w = EW.EsmWorker(st, c, now=lambda: now[0], on=True)
    w.add(MINT, "XXX")
    run(w)
    q = st.q("SELECT * FROM esm_queue")[0]
    assert q["state"] == "WAIT" and q["t_create"] == T0 and q["creator"] == DEV and q["creation_sig"] == "c"
    assert not st.q("SELECT 1 FROM esm_coin")                  # not scored before the window has closed
    now[0] = T0 + 7300
    run(w)
    assert st.q("SELECT state FROM esm_queue")[0]["state"] == "DONE"
    assert ("getTransaction", "late") not in c.calls           # the trade after minute 120 is never read
    coin = st.q("SELECT * FROM esm_coin")[0]
    assert coin["status"] == "DONE" and json.loads(coin["coverage"])["decoded"] == json.loads(coin["coverage"])["window_successful"]
    ww = {r["wallet"]: r for r in st.q("SELECT * FROM esm_window_wallet")}
    assert ww[BUYER]["bucket"] == "0-15" and ww[BUYER]["buys"] == 2 and ww[BUYER]["points"] >= 2
    assert ww[DEV]["bucket"] == "15-60"


def test_unserved_transaction_is_counted_not_guessed(tmp_path):
    now = [T0 + 7300]
    st, c = store(tmp_path), chain(unserved={"b3"})
    w = EW.EsmWorker(st, c, now=lambda: now[0], on=True)
    w.add(MINT)
    run(w)
    coin = st.q("SELECT * FROM esm_coin")[0]
    cov = json.loads(coin["coverage"])
    assert cov["not_served"] == 1 and coin["status"] == "INSUFFICIENT_DATA"
    assert [x for x in c.calls if x == ("getTransaction", "b3")].__len__() == EW.MAX_TRIES


def test_history_too_long_is_insufficient_data(tmp_path, monkeypatch):
    monkeypatch.setattr(EW, "PAGE", 1)
    monkeypatch.setattr(EW, "MAX_PAGES", 2)
    st = store(tmp_path)
    w = EW.EsmWorker(st, chain(), on=True)
    w.add(MINT)
    run(w)
    q = st.q("SELECT state, note FROM esm_queue")[0]
    assert q["state"] == "INSUFFICIENT_DATA" and "chưa tới giao dịch tạo" in q["note"]


def test_each_step_is_bounded_by_its_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(EW, "PAGE", 1)
    st, c = store(tmp_path), chain()
    w = EW.EsmWorker(st, c, on=True, budget_per_step=2)
    w.add(MINT)
    w.step()
    assert len(c.calls) == 2


def test_oldest_signature_that_is_not_a_creation_stops(tmp_path):
    st = store(tmp_path)
    c = chain()
    c.txs["c"] = buy_tx(DEV, T0, 0.1, 5, "c")                 # history starts after the creation: not the mint's birth
    w = EW.EsmWorker(st, c, on=True)
    w.add(MINT)
    run(w)
    assert st.q("SELECT state FROM esm_queue")[0]["state"] == "INSUFFICIENT_DATA"


# ------------------------------------------------------------------ auto tracing (2026-11-26)
WHALE = "AcysJyF3VVkS6pUU2AgMYNaNrkjXAuTW4Q2LPkPp6TCT"                     # not on the watchlist file
FUNDER = "7uqfEqdd9strJVYiju6kasjCj6AzjBF2S1GmWRhdpump"


def fund_tx(src, dst, sol, ts, sig):
    return {"blockTime": ts, "slot": ts, "meta": {"err": None, "fee": 5000, "preBalances": [], "postBalances": [], "preTokenBalances": [],
                                                  "postTokenBalances": [], "innerInstructions": [], "logMessages": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": src, "signer": True}], "instructions": [
                {"program": "system", "parsed": {"type": "transfer", "info": {"source": src, "destination": dst, "lamports": int(sol * 1e9)}}}]}}}


class WalletChain(Chain):
    """Mint history + one wallet history (its funding before the buy)."""
    def __init__(self, txs, wallet_txs):
        super().__init__({**txs, **wallet_txs})
        self.mint_order = [s for s in self.order if s not in wallet_txs]
        self.wallet_order = sorted(wallet_txs, key=lambda s: -wallet_txs[s]["blockTime"])

    def __call__(self, method, params):
        if method == "getSignaturesForAddress" and params[0] == WHALE:
            self.calls.append((method, params[0]))
            return [{"signature": s, "blockTime": self.txs[s]["blockTime"], "err": None} for s in self.wallet_order]
        if method == "getSignaturesForAddress":
            self.calls.append((method, params[0]))
            opts = params[1]
            i = self.mint_order.index(opts["before"]) + 1 if opts.get("before") else 0
            return [{"signature": s, "blockTime": self.txs[s]["blockTime"], "err": None} for s in self.mint_order[i:i + opts["limit"]]]
        return super().__call__(method, params)


def test_a_coin_added_on_the_page_is_traced_and_its_big_wallet_watched(tmp_path, monkeypatch):
    from insiders import esm as E
    from insiders.newcoins import SmartBook
    st = T.Store(tmp_path / "t.db")
    st.x("INSERT INTO esm_pending_mint VALUES ('NEWC', 'REQUESTED', ?, 'page', 1)", (json.dumps([{"mint": MINT}]),))
    txs = {"c": create_tx(), "w1": buy_tx(WHALE, T0 + 30, 8.0, 3_000_000_000, "w1"), "b1": buy_tx(BUYER, T0 + 60, 1.0, 1_000_000_000, "b1")}
    c = WalletChain(txs, {"f1": fund_tx(FUNDER, WHALE, 9.0, T0 - 3600, "f1")})
    now = [T0 + 7300]
    w = EW.EsmWorker(st, c, now=lambda: now[0], on=True)
    run(w, 30)
    assert st.q("SELECT state FROM esm_queue")[0]["state"] == "DONE"
    req = st.q("SELECT status, note FROM esm_pending_mint WHERE symbol='NEWC'")[0]
    assert req["status"] == "DONE" and "1 ví lớn" in req["note"]                 # WHALE (8 SOL); BUYER (1 SOL) is small
    assert st.q("SELECT src, dst, amount FROM edges WHERE kind='research_sol'") == [{"src": FUNDER, "dst": WHALE, "amount": 9.0}]
    assert st.q("SELECT ref FROM esm_flag WHERE wallet=? AND flag='INDEPENDENCE'", (WHALE,))[0]["ref"] == "INDEPENDENT"
    assert st.q("SELECT SUM(calls) n FROM rpc_usage WHERE worker='esm'")[0]["n"] == len(c.calls)   # every call metered
    b = SmartBook(st)
    b.refresh(1e12)
    assert WHALE in b.smart and b.smart[WHALE]["sources"] == ["auto:NEWC"] and BUYER not in b.smart
    assert WHALE in {x["wallet"] for x in __import__("insiders.site", fromlist=["x"]).nc_get(None, st, "/api/nc/wallets", "")[1]["wallets"]}


def test_a_wrong_address_ends_rejected(tmp_path):
    st = T.Store(tmp_path / "t.db")
    st.x("INSERT INTO esm_pending_mint VALUES ('BAD', 'REQUESTED', ?, 'page', 1)", (json.dumps([{"mint": MINT}]),))
    txs = {"x": buy_tx(BUYER, T0, 1.0, 10, "x")}                             # oldest transaction does not create the mint
    run(EW.EsmWorker(st, Chain(txs), on=True), 5)
    r = st.q("SELECT status, note FROM esm_pending_mint")[0]
    assert r["status"] == "REJECTED" and "không truy vết được" in r["note"]


def test_discovery_keeps_only_signatures_that_can_be_in_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(EW, "PAGE", 2)
    txs = {"c": create_tx(), "b1": buy_tx(BUYER, T0 + 60, 1.0, 10, "b1")}
    txs.update({f"old{i}": buy_tx(BUYER, T0 + 50_000 + i, 0.1, 1, f"old{i}") for i in range(9)})   # long after the window
    st = store(tmp_path)
    w = EW.EsmWorker(st, Chain(txs), on=True)
    w.add(MINT)
    run(w, 10)
    assert {r["sig"] for r in st.q("SELECT sig FROM esm_sig")} == {"c", "b1"}


def test_daily_public_rpc_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(EW, "MAX_CALLS_DAY", 3)
    monkeypatch.setattr(EW, "PAGE", 1)
    st, c = store(tmp_path), chain()
    w = EW.EsmWorker(st, c, on=True, budget_per_step=2)
    w.add(MINT)
    w.step()
    w.step()
    assert w.step() == {"_capped": "ESM_MAX_CALLS_DAY"} and len(c.calls) == 4
