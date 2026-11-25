"""Coin watchlist, buy detection, idempotency, funding graph, ranking / bots and paper-only measurement (track.py)."""
import json
import random
import re
import threading
from pathlib import Path

import pytest

from insiders import scan as S
from insiders import track as T

SOL = 10 ** 9
MINT = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"
POOL = "6zZHQtPqiBzwm4qR21BHaKp5VJEYhzjccamfd6x8cjsP"        # program account (off-curve)
WSOL = T.WSOL
_A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw: bytes) -> str:
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _A[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def wallets(n, seed=7):
    rnd, out = random.Random(seed), []
    while len(out) < n:
        a = b58(bytes(rnd.getrandbits(8) for _ in range(32)))
        if S.on_curve(a) and T.valid_mint(a):
            out.append(a)
    return out


W = wallets(20)
KNOWN, MID, BUYER = W[0], W[1], W[2]


def swap(sig, ts, wallet, tok, pool_tok, pool_wsol, wallet_sol, err=None, slot=100):
    rows = [(wallet, MINT, *tok), (POOL, MINT, *pool_tok), (POOL, WSOL, *pool_wsol)]
    pre = [{"owner": o, "mint": m, "uiTokenAmount": {"amount": str(a), "decimals": 6}} for o, m, a, _ in rows]
    post = [{"owner": o, "mint": m, "uiTokenAmount": {"amount": str(b), "decimals": 6}} for o, m, _, b in rows]
    return {"slot": slot, "blockTime": ts, "meta": {"err": err, "preTokenBalances": pre, "postTokenBalances": post,
                                                    "preBalances": [wallet_sol[0]], "postBalances": [wallet_sol[1]]},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": wallet, "signer": True}], "instructions": []}}}


def transfer(sig, ts, src, dst, lamports):
    return {"slot": 50, "blockTime": ts, "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [],
                                                  "preBalances": [0, 0], "postBalances": [0, 0], "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": src, "signer": True}, {"pubkey": dst}],
                            "instructions": [{"parsed": {"type": "transfer", "info": {"source": src, "destination": dst, "lamports": lamports}}}]}}}


def token_move(sig, ts, src, dst, amount):
    pre = [{"owner": src, "mint": MINT, "uiTokenAmount": {"amount": str(amount), "decimals": 6}},
           {"owner": dst, "mint": MINT, "uiTokenAmount": {"amount": "0", "decimals": 6}}]
    post = [{"owner": src, "mint": MINT, "uiTokenAmount": {"amount": "0", "decimals": 6}},
            {"owner": dst, "mint": MINT, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}]
    return {"slot": 60, "blockTime": ts, "meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post,
                                                  "preBalances": [5 * SOL, 1 * SOL], "postBalances": [5 * SOL - 5000, 1 * SOL]},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": src, "signer": True}, {"pubkey": dst}], "instructions": []}}}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def tracker(tmp_path, rpc=None, known=None, clock=None, meta=None):
    clock = clock or Clock()
    st = T.Store(tmp_path / "t.db", now=clock)
    return T.Tracker(st, rpc or (lambda m, p: None), known=lambda: known or {}, group=lambda a: "DEPTH" if a in (known or {}) else None,
                     meta=meta, now=clock, budget=T.Budget(10_000))


# ---------------------------------------------------------------------------------------------------- coins
def test_add_validates_dedups_and_reports_every_line(tmp_path):
    t = tracker(tmp_path)
    text = f"mint,name\n{MINT},CA\nnot-a-mint\n{MINT}\n0OIl{'1' * 40}\n{W[5]},Other"
    res = t.add(T.parse_bulk(text))
    assert [r["result"] for r in res] == ["added", "invalid", "already tracked", "invalid", "added"]
    assert t.add([{"mint": MINT}])[0]["result"] == "already tracked"
    assert {m["mint"] for m in t.s.mints()} == {MINT, W[5]}
    assert t.add([{"mint": MINT}] * (T.BULK_MAX + 1))[0]["result"] == "failed"


def test_pause_resume_remove_and_state_survives_a_restart(tmp_path):
    t = tracker(tmp_path)
    t.add([{"mint": MINT, "name": "X", "note": "n", "tags": ["depth"]}])
    t.s.x("INSERT INTO events (sig, wallet, mint, kind) VALUES ('s', 'w', ?, 'BUY')", (MINT,))
    assert t.action(MINT, "pause")["status"] == "paused"
    assert t.action(MINT, "resume")["status"] == "queued"
    t.s.set_mint(MINT, newest_sig="CHK", back_before="B", pages=3)
    t.action(MINT, "remove")
    assert t.s.mints() == [] and t.s.q("SELECT COUNT(*) n FROM events")[0]["n"] == 1     # events kept
    t2 = tracker(tmp_path)                                                               # restart: same file
    m = t2.s.mint(MINT)
    assert m["removed"] == 1 and m["newest_sig"] == "CHK" and m["tags"] == "DEPTH"
    assert t2.add([{"mint": MINT}])[0]["result"] == "added" and t2.s.mint(MINT)["removed"] == 0
    assert t2.s.version() == len(T.MIGRATIONS)


CHU = "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump"


def test_initial_list_seeds_verified_mints_once_and_lists_the_unknown_ones(tmp_path):
    t = tracker(tmp_path)
    t.seed()
    assert {m["name"] for m in t.s.mints()} == {"CHUCHU", "MACCA", "SCINU"}
    assert [p["name"] for p in t.pending()] == ["COGE", "FRENS", "MOONX", "STANK"]       # never guessed
    t.action(CHU, "remove")
    t.seed()
    assert CHU not in {m["mint"] for m in t.s.mints()} and len(t.pending()) == 4         # a removal sticks
    assert t.add([{"mint": W[3], "name": "moonx"}])[0]["result"] == "added"              # the address arrives later
    assert [p["name"] for p in t.pending()] == ["COGE", "FRENS", "STANK"]


def test_an_older_build_that_seeded_chuchu_keeps_it_and_gets_the_new_seeds(tmp_path):
    t = tracker(tmp_path)
    t.add([{"mint": CHU, "name": "CHUCHU"}])
    t.s.put("seeded", True)
    t.s.set_mint(CHU, newest_sig="KEEP", pages=7, history_state="done", status="live")
    t.action(CHU, "remove")                                                              # the user removed it
    t.seed()
    assert {m["name"] for m in t.s.mints()} == {"MACCA", "SCINU"}
    assert t.s.mint(CHU)["newest_sig"] == "KEEP" and t.s.mint(CHU)["pages"] == 7


def test_migration_to_v2_keeps_every_coin_event_and_checkpoint(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(T.MIGRATIONS[0])
    db.execute("PRAGMA user_version=1")
    db.execute("INSERT INTO mints (mint, name, status, added_ts, newest_sig, back_before, pages, history_state, removed) "
               "VALUES (?, 'OLD', 'live', 5, 'NEWEST', 'BACK', 9, 'done', 0)", (MINT,))
    db.execute("INSERT INTO events (sig, wallet, mint, kind, block_time) VALUES ('s1', 'w', ?, 'BUY', 7)", (MINT,))
    db.execute("INSERT INTO queue (sig, mint, why, added) VALUES ('q1', ?, 'live', 8)", (MINT,))
    db.commit()
    db.close()
    st = T.Store(path)
    assert st.version() == len(T.MIGRATIONS) == 7
    m = st.mint(MINT)
    assert (m["name"], m["status"], m["newest_sig"], m["back_before"], m["pages"], m["history_state"]) == \
        ("OLD", "live", "NEWEST", "BACK", 9, "done")
    assert m["verified"] is None                                                         # checked later, not reset
    assert st.q("SELECT sig FROM events") == [{"sig": "s1"}] and st.q("SELECT sig FROM queue") == [{"sig": "q1"}]
    assert (tmp_path / "old.db.v1.bak").exists()
    T.Store(path)                                                                        # reopening changes nothing
    assert st.mint(MINT)["newest_sig"] == "NEWEST"


def test_a_non_mint_or_missing_address_is_flagged_not_scanned(tmp_path):
    acct = {MINT: {"value": {"owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}}}},
            W[3]: {"value": {"owner": S.SYSTEM, "data": ["", "base64"]}}, W[4]: {"value": None}}
    calls = []

    def rpc(method, params):
        calls.append(method)
        return acct.get(params[0]) if method == "getAccountInfo" else []
    t = tracker(tmp_path, rpc=rpc)
    t.add([{"mint": MINT}, {"mint": W[3]}, {"mint": W[4]}])
    t.tick()
    t.tick()
    v = {m["mint"]: m for m in t.coins()}
    assert v[MINT]["verified"] == "mint" and v[MINT]["token_program"] == "Token-2022" and v[MINT]["decimals"] == 6
    assert v[W[3]]["status"] == "error" and "không phải token mint" in v[W[3]]["last_error"]
    assert v[W[4]]["status"] == "error" and "không tồn tại" in v[W[4]]["last_error"]
    assert t.action(W[4], "retry")["verified"] is None


# ---------------------------------------------------------------------------------------------------- buys
def test_a_priced_swap_is_a_buy_and_transfers_or_failures_are_not():
    buy = swap("b", 10, BUYER, (0, 1000), (5000, 4000), (50 * SOL, 52 * SOL), (10 * SOL, 8 * SOL))
    evs = T.parse_mint_events(buy, MINT)
    assert [(e["wallet"], e["kind"], e["sol"], e["confidence"]) for e in evs] == [(BUYER, "BUY", 2.0, "high")]
    assert evs[0]["slot"] == 100 and evs[0]["parser"] == T.PARSER_VERSION
    failed = swap("f", 10, BUYER, (0, 1000), (5000, 4000), (50 * SOL, 52 * SOL), (10 * SOL, 8 * SOL), err={"x": 1})
    assert T.parse_mint_events(failed, MINT) == []
    mv = T.parse_mint_events(token_move("m", 11, BUYER, MID, 500), MINT)
    assert sorted((e["wallet"], e["kind"]) for e in mv) == sorted([(BUYER, "SEND"), (MID, "RECEIVE")])
    assert all(e["kind"] != "BUY" for e in mv)


def test_the_same_signature_from_socket_and_poll_is_one_event(tmp_path):
    t = tracker(tmp_path)
    buy = swap("b", 10, BUYER, (0, 1000), (5000, 4000), (50 * SOL, 52 * SOL), (10 * SOL, 8 * SOL))
    assert len(t.ingest(buy, MINT, "ws", 11)) == 1
    assert t.ingest(buy, MINT, "poll", 12) == []
    assert t.s.q("SELECT COUNT(*) n FROM events")[0]["n"] == 1
    assert t.s.q("SELECT COUNT(*) n FROM fund_todo")[0]["n"] == 1                       # a new buyer: funding queued


# ---------------------------------------------------------------------------------------------------- worker
class FakeChain:
    """getSignaturesForAddress / getTransaction over a fixed list of transactions (newest first per address)."""

    def __init__(self, by_addr, txs):
        self.by_addr, self.txs, self.calls = by_addr, txs, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getTransaction":
            return self.txs.get(params[0])
        if method == "getAccountInfo":
            return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                              "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}}}}
        addr, o = params[0], params[1]
        sigs = self.by_addr.get(addr, [])
        if o.get("until"):
            sigs = sigs[:[s["signature"] for s in sigs].index(o["until"])] if o["until"] in [s["signature"] for s in sigs] else sigs
        if o.get("before"):
            names = [s["signature"] for s in sigs]
            sigs = sigs[names.index(o["before"]) + 1:] if o["before"] in names else []
        return sigs[:o.get("limit", 1000)]


def test_history_then_live_then_funding_finds_a_linked_buyer_with_evidence(tmp_path):
    clock = Clock(10_000)
    txs = {"buy1": swap("buy1", 9_000, BUYER, (0, 1000), (5000, 4000), (50 * SOL, 53 * SOL), (10 * SOL, 7 * SOL)),
           "f1": transfer("f1", 8_800, MID, BUYER, 3 * SOL),
           "f0": transfer("f0", 8_700, KNOWN, MID, 4 * SOL),
           "buy2": swap("buy2", 9_500, W[6], (0, 10), (4000, 3990), (53 * SOL, 54 * SOL), (5 * SOL, 4 * SOL))}
    chain = FakeChain({MINT: [{"signature": "buy1", "blockTime": 9_000, "err": None}],
                       BUYER: [{"signature": "buy1", "blockTime": 9_000}, {"signature": "f1", "blockTime": 8_800}],
                       MID: [{"signature": "f1", "blockTime": 8_800}, {"signature": "f0", "blockTime": 8_700}]}, txs)
    t = tracker(tmp_path, rpc=chain, known={KNOWN: 1}, clock=clock, meta=lambda m: {"price_usd": 0.001, "src": "test"})
    t.add([{"mint": MINT, "name": "C"}])
    for _ in range(5):
        t.tick()
    m = t.s.mint(MINT)
    assert m["history_state"] == "done" and m["newest_sig"] == "buy1" and "đầy đủ" in m["history_note"]
    assert t.s.q("SELECT wallet, kind FROM events")[0] == {"wallet": BUYER, "kind": "BUY"}
    chain.by_addr[MINT].insert(0, {"signature": "buy2", "blockTime": 9_500, "err": None})   # a new buy, live
    clock.t += 100
    for _ in range(6):
        t.tick()
    assert t.s.q("SELECT COUNT(*) n FROM events WHERE kind='BUY'")[0]["n"] == 2
    assert t.coins()[0]["status"] == "live"
    edges = {(e["src"], e["dst"]) for e in t.s.edges()}
    assert (MID, BUYER) in edges and (KNOWN, MID) in edges
    lb = t.s.q("SELECT * FROM signals WHERE rule='LINKED_BUY'")
    assert len(lb) == 1
    ev = json.loads(lb[0]["evidence"])
    assert ev["hops"] == 2 and ev["paths"][0][0]["sigs"] == ["f0"] and ev["paths"][0][1]["sigs"] == ["f1"]
    d = t.coin_detail(MINT)
    b = next(x for x in d["buyers"] if x["wallet"] == BUYER)
    assert b["candidate"] and not b["known"]                                             # a hypothesis, not "insider"
    assert any(n["id"] == KNOWN and n["known"] for n in d["graph"]["nodes"])
    t.s.x("UPDATE edges SET hidden=1 WHERE sig='f0'")                                    # a wrong edge can be hidden
    assert (KNOWN, MID) not in {(e["src"], e["dst"]) for e in t.s.edges()}
    assert t.s.q("SELECT COUNT(*) n FROM events")[0]["n"] == 2                           # raw events untouched


def test_paused_stream_makes_no_helius_call(tmp_path):
    chain = FakeChain({MINT: []}, {})
    t = tracker(tmp_path, rpc=chain)
    t.paused = lambda: True
    t.add([{"mint": MINT}])
    t.tick()
    assert chain.calls == []


def test_budget_bounds_the_calls_per_minute():
    clock = Clock(0)
    b = T.Budget(3, now=clock)
    assert [b.take() for _ in range(5)] == [True, True, True, False, False]
    clock.t += 20
    assert b.take() and not b.take()


# ---------------------------------------------------------------------------------------------------- graph
def test_fan_out_source_intermediaries_many_leaves_is_one_dispersal_with_evidence():
    src, mids, leaves = W[0], W[1:3], W[3:13]
    edges, i = [], 0
    for m in mids:
        edges.append({"id": i, "src": src, "dst": m, "asset": "SOL", "amount": 70.0, "ts": 1000, "sig": f"s{i}", "kind": "fund"})
        i += 1
    for k, leaf in enumerate(leaves):
        edges.append({"id": i, "src": mids[k % 2], "dst": leaf, "asset": "SOL", "amount": 1.2, "ts": 1100, "sig": f"s{i}", "kind": "fund"})
        i += 1
    buys = {leaf: 2000 + k for k, leaf in enumerate(leaves)}
    rows = T.dispersal(edges, buys, min_leaves=5)
    assert rows[0]["source"] == src and rows[0]["n_leaves"] == 10 and rows[0]["max_hops"] == 2
    cl = T.clusters(edges, now=2000)
    assert len({cl[a] for a in [src, *mids]}) == 1                                       # one soft cluster
    exch = next(a for a, (_, k) in S.KNOWN_LABELS.items() if k == "exchange")
    e2 = [{"id": 99, "src": exch, "dst": W[14], "asset": "SOL", "amount": 50, "ts": 1000, "sig": "x", "kind": "fund"},
          {"id": 98, "src": exch, "dst": W[15], "asset": "SOL", "amount": 50, "ts": 1000, "sig": "y", "kind": "fund"}]
    assert T.clusters(e2, now=2000) == {}                                                 # an exchange never merges wallets
    assert T.evidence_paths(e2, W[14], {exch}) == []


def test_evidence_path_respects_time_order():
    e = [{"id": 1, "src": KNOWN, "dst": MID, "asset": "SOL", "amount": 5, "ts": 900, "sig": "a", "kind": "fund"},
         {"id": 2, "src": MID, "dst": BUYER, "asset": "SOL", "amount": 4, "ts": 800, "sig": "b", "kind": "fund"}]
    assert T.evidence_paths(e, BUYER, {KNOWN}, before_ts=1000) == []                     # the money came after
    e[0]["ts"] = 700
    assert len(T.evidence_paths(e, BUYER, {KNOWN}, before_ts=1000)) == 1


# ---------------------------------------------------------------------------------------------------- rank / bots
def test_low_balance_wallet_with_evidence_outranks_a_rich_idle_one(tmp_path):
    clock = Clock(100_000)
    t = tracker(tmp_path, clock=clock)

    class Vip:
        base = {KNOWN: "x"}
        state = {"children": {BUYER: {"parent": KNOWN, "sol": 5, "since": 99_000, "bal": 0.01},
                              W[7]: {"parent": KNOWN, "sol": 0.5, "since": 1_000, "bal": 40.0}},
                 "last_seen": {BUYER: 99_900}}
    for k, m in enumerate(W[8:11]):
        t.s.add_event({"sig": f"k{k}", "wallet": KNOWN, "mint": m, "kind": "BUY", "ts": 90_000 + k})
        t.s.add_event({"sig": f"b{k}", "wallet": BUYER, "mint": m, "kind": "BUY", "ts": 90_060 + k})
        t.s.x("INSERT INTO mints (mint, status, added_ts, removed) VALUES (?, 'live', 0, 0)", (m,))
    r = t.rank_children(Vip())
    assert r[BUYER]["score"] > r[W[7]]["score"]
    assert r[BUYER]["features"]["coord_mints"] == 3 and any("số dư 0.01" in w for w in r[BUYER]["why"])


def test_low_balance_ranked_child_is_followed_by_the_stream():
    from insiders import vip as VP
    from tests.test_insiders_core import WALLET

    class Wt:
        state = {}
        lock = threading.Lock()

        def bots(self):
            return []
    w = Wt()
    r = {"vip": {"holders": [{"wallet": WALLET, "coins": []}]}}
    vs = VP.VipStream(w, r, rpc=lambda m, p: None, now=lambda: 2_000.0)
    vs.state["children"] = {BUYER: {"parent": WALLET, "sol": 2, "since": 1_900, "bal": 0.0},
                            W[9]: {"parent": WALLET, "sol": 2, "since": 1_900, "bal": 0.0}}
    assert BUYER not in vs.wallets()
    vs.rank = {BUYER: {"score": 0.9, "why": ["x"], "certainty": "high"}}
    assert BUYER in vs.wallets() and W[9] not in vs.wallets()
    assert vs.info(BUYER)["rank"]["score"] == 0.9


def test_bot_labels_have_features_and_reasons_and_never_hide_events():
    evs = [{"kind": "BUY", "mint": f"m{i}", "block_time": 1000 + i * 5, "quote_sol": 0.5} for i in range(30)]
    first = {f"m{i}": 1000 + i * 5 - 1 for i in range(30)}
    b = T.bot_features(evs, first)
    assert b["label"] == "bot-like" and b["why"] and b["features"]["mints_per_day"] >= 8 and b["confidence"] == "high"
    human = [{"kind": k, "mint": "a", "block_time": t, "quote_sol": s} for k, t, s in
             [("BUY", 0, 1.3), ("BUY", 7200, 2.1), ("SELL", 86400, 3), ("BUY", 90000, 0.7), ("SELL", 170000, 1)]]
    assert T.bot_features(human)["label"] == "human-like"
    assert T.bot_features(human[:2])["label"] == "unknown"


# ---------------------------------------------------------------------------------------------------- paper / shadow
def test_signal_snapshot_and_outcomes_are_measured_without_interpolation(tmp_path):
    clock = Clock(1_000)
    prices = {"p": 1.0}
    t = tracker(tmp_path, clock=clock, meta=lambda m: {"price_usd": prices["p"], "src": "test"})
    sid = t.record_signal("KNOWN_BUY", MINT, [KNOWN], 990, {"sig": "s"}, dedup="d1")
    assert sid and t.record_signal("KNOWN_BUY", MINT, [KNOWN], 990, {}, dedup="d1") is None   # once
    sg = t.s.q("SELECT * FROM signals")[0]
    assert sg["ref_price"] == 1.0 and sg["price_src"] == "test" and sg["latency_s"] == 10 and sg["rules_version"]
    clock.t += 61
    prices["p"] = 1.5
    t.px.clear()
    t.step_outcomes()
    clock.t += 300
    prices["p"] = None
    t.px.clear()
    t.step_outcomes()
    oc = {r["horizon"]: r for r in t.s.q("SELECT * FROM outcomes WHERE done IS NOT NULL")}
    assert oc[60]["change"] == pytest.approx(0.5) and oc[300]["change"] is None and oc[300]["note"] == "không có giá"
    m = t.metrics()["rules"][0]
    assert m["signals"] == 1 and m["horizons"][0]["median"] == pytest.approx(0.5)
    t.set_shadow(False)
    assert t.record_signal("KNOWN_BUY", MINT, [KNOWN], 990, {}, dedup="d2")
    assert t.s.q("SELECT COUNT(*) n FROM outcomes WHERE signal_id=(SELECT MAX(id) FROM signals)")[0]["n"] == 0


def test_no_code_path_signs_or_sends_a_transaction():
    root = Path(__file__).resolve().parents[1] / "insiders"
    bad = re.compile(r"sendTransaction|sendRawTransaction|signTransaction|Keypair|secretKey|private_key|/swap/v1/swap\b|swap-instructions", re.I)
    for p in [*root.glob("*.py"), root / "page" / "app.js"]:
        assert not bad.search(p.read_text(encoding="utf-8")), p.name


# ---------------------------------------------------------------------------------------------------- API
def test_track_api_add_list_action_and_validation(tmp_path, monkeypatch):
    from tests.test_insiders_site import call
    from insiders import site
    from insiders.service import InsiderService
    import urllib.request
    monkeypatch.setenv("APP_ACCESS_CODE", "c")
    monkeypatch.setenv("INSIDERS_STATE_DIR", str(tmp_path))
    svc = InsiderService(result_path=tmp_path / "r.json", key="HELIUS-SECRET-track-9")
    svc.result = {"tokens": []}
    svc.tracker = tracker(tmp_path)
    httpd = site.serve(host="127.0.0.1", port=0, watch=False, svc=svc, callers_app=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def post(path, obj):
            req = urllib.request.Request(url + path, method="POST", data=json.dumps(obj).encode(), headers={"X-Access-Code": "c"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        assert call(url + "/api/insiders/track/coins")[0] == 401
        res = post("/api/insiders/track/coins", {"text": f"{MINT}\nbad", "history": True, "live": True, "tags": ["STANK"]})
        assert [r["result"] for r in res["results"]] == ["added", "invalid"]
        code, body = call(url + "/api/insiders/track/coins", "c")
        assert code == 200 and json.loads(body)["coins"][0]["tags"] == "STANK"
        assert call(url + "/api/insiders/track/coin?mint=bad!", "c")[0] == 400
        assert call(url + f"/api/insiders/track/coin?mint={MINT}&op=pause", "c", "POST")[0] == 200
        assert json.loads(call(url + f"/api/insiders/track/coin?mint={MINT}", "c")[1])["coin"]["status"] == "paused"
        for p in ("status", "events", "graph", "paper", "wallets"):
            assert call(url + f"/api/insiders/track/{p}", "c")[0] == 200, p
        assert call(url + "/api/insiders/track/events?kind=bad;", "c")[0] == 400
        h = json.loads(call(url + "/healthz")[1])
        assert h["track"]["mints"] == 1 and "HELIUS-SECRET" not in json.dumps(h)
    finally:
        httpd.shutdown()


def test_radar_row_has_paid_sol_mc_at_buy_rank_funding_and_latency(tmp_path):
    clock = Clock(10_000)
    t = tracker(tmp_path, known={KNOWN: 1}, clock=clock)
    t.sol_usd = lambda: 200.0
    t.add([{"mint": MINT, "name": "C"}])
    t.s.set_mint(MINT, decimals=6, supply=str(10 ** 9 * 10 ** 6), verified="mint")
    buy = swap("b", 9_990, BUYER, (0, 1000 * 10 ** 6), (5000 * 10 ** 6, 4000 * 10 ** 6), (50 * SOL, 52 * SOL), (10 * SOL, 8 * SOL))
    t.ingest(buy, MINT, "mint-poll", 9_993)
    t.s.add_edge(KNOWN, BUYER, "SOL", 3.0, 9_000, "f", "fund")
    t.rank_cache = {BUYER: {"score": 0.7, "why": ["x"], "certainty": "medium"}}
    r = t.radar()["buys"][0]
    assert (r["wallet"], r["mint"], r["sol"], r["usd"], r["latency_s"]) == (BUYER, MINT, 2.0, 400.0, 3.0)
    assert r["mc_usd"] == 400_000_000                    # 0.002 SOL per token x $200 x 1e9 supply
    assert r["funding"]["src"] == KNOWN and r["funding"]["known"] and r["rank"]["score"] == 0.7
    t.s.set_mint(MINT, supply=None)
    assert t.radar()["buys"][0]["mc_usd"] is None        # unknown supply: no guess
    assert "Chưa quét toàn bộ" in t.radar()["coverage"]["note"]


def test_form_name_goes_to_the_one_valid_address_and_resolves_a_pending_coin(tmp_path):
    from insiders import site
    from insiders.service import InsiderService
    svc = InsiderService(result_path=tmp_path / "r.json", key="")
    svc.tracker = tracker(tmp_path)
    svc.tracker.seed()
    code, res = site.track_post(svc, "/api/insiders/track/coins", "/api/insiders/track/coins",
                                json.dumps({"text": f"not-a-mint\n{W[3]}", "name": "MOONX"}).encode())
    assert code == 200 and [r["result"] for r in res["results"]] == ["invalid", "added"]
    assert svc.tracker.s.mint(W[3])["name"] == "MOONX" and "MOONX" not in [p["name"] for p in svc.tracker.pending()]
