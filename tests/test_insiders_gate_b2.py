"""Gate B2: backfill after a restart / socket reconnect / resume, pagination past 1000 signatures, checkpoints, dedupe,
retries and explicit gaps (vip.py stream + track.py mint tail). Fake RPCs only: a real network call fails the test."""
import asyncio
import socket
import threading
import urllib.request

import pytest

from insiders import track as T
from insiders import vip as VP
from insiders import watch as WT

WAL = "7GSMwnWB5YWmCwQax4bzfeDMvSZEogp6TAehxrJddDyr"
MINT_B = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"
P = WAL[:4]
M = MINT_B[:4]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call in a Gate B test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def empty_tx(sig):
    return {"blockTime": 1, "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [],
                                     "preBalances": [0], "postBalances": [0], "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": WAL}], "instructions": []}}}


class Chain:
    """getSignaturesForAddress with until / before / limit over a growing list (newest first), plus failures."""

    def __init__(self, sigs=None):
        self.by, self.calls, self.fail_next, self.txs = {}, [], 0, {}
        for a, n in (sigs or {}).items():
            self.grow(a, n)

    def grow(self, a, n):
        cur = self.by.setdefault(a, [])
        k = len(cur)
        new = [{"signature": f"{a[:4]}-{k + i}", "blockTime": 1000 + k + i, "err": None} for i in range(n)]
        self.by[a] = list(reversed(new)) + cur

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("HTTP 429")                 # what Rpc raises once its own retries are spent
        if method == "getTransaction":
            return self.txs.get(params[0])
        a, o = params[0], params[1]
        sigs = self.by.get(a, [])
        names = [s["signature"] for s in sigs]
        if o.get("until") in names:
            sigs = sigs[:names.index(o["until"])]
            names = names[:len(sigs)]
        if o.get("before"):
            sigs = sigs[names.index(o["before"]) + 1:] if o["before"] in names else []
        return sigs[:o.get("limit", 1000)]


def stream(tmp_path, chain, fake_handle=True):
    r = {"tokens": [], "tickers": {}, "vip": {"holders": [{"wallet": WAL, "profit_sol": 1.0, "coins": ["X"]}]}}
    w = WT.Watcher(r, tmp_path / "w.json", rpc=lambda m, p: [], now=lambda: 2_000.0)
    vs = VP.VipStream(w, r, rpc=chain, now=lambda: 2_000.0, focus="INSIDERS")
    vs.retry_sleep = False
    vs.got = []
    if fake_handle:
        vs.handle = lambda a, sig, observed=None, source="poll": vs.got.append((a, sig, source)) or vs.seen.append(sig)
    vs.ws_set = lambda: [WAL]
    w.vip = vs
    return w, vs


def sigs_of(vs):
    return [s for _, s, _ in vs.got]


def test_restart_reads_1500_missed_signatures_back_to_the_checkpoint(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.catch_up(chain, "start")                            # first look: the position only
    assert vs.state["poll_last"][WAL] == f"{P}-0" and vs.got == []
    w.save()
    chain.grow(WAL, 1500)                                  # downtime: 1 500 transactions
    w2, vs2 = stream(tmp_path, chain)                      # restart: the checkpoint is read back from disk
    assert vs2.catchup_due == "start" and vs2.state["poll_last"][WAL] == f"{P}-0"
    submitted = []
    run = vs2._run_items
    vs2._run_items = lambda items: submitted.extend(s for _, s, _, _ in items) or run(items)
    assert vs2.backfill_round(chain) == 1500
    want = [f"{P}-{i}" for i in range(1, 1501)]
    assert submitted == want                                               # handed over oldest first
    assert sorted(sigs_of(vs2)) == sorted(want) and len(vs2.got) == 1500   # every one, once (3 workers: any finish order)
    assert vs2.state["poll_last"][WAL] == f"{P}-1500" and vs2.gaps(open_only=True) == []
    pages = [p[1] for m, p in chain.calls if m == "getSignaturesForAddress" and p[1].get("until")]
    assert len(pages) == 2 and pages[1]["before"] == f"{P}-501"          # 1000 + 500, paged with `before`


def test_past_the_page_cap_the_rest_is_an_explicit_gap_then_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(VP, "CATCHUP_PAGES", 1)
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 1500)
    vs.catch_up(chain, "reconnect")
    assert len(vs.got) == 1000
    g = vs.gaps(open_only=True)
    assert len(g) == 1 and g[0]["until"] == f"{P}-0" and g[0]["before"] == f"{P}-501" and "reconnect" in g[0]["reason"]
    vs.fill_gaps(chain)
    assert vs.gaps(open_only=True) == [] and vs.gaps()[0]["filled"] == 500
    assert sorted(sigs_of(vs)) == sorted(f"{P}-{i}" for i in range(1, 1501))
    assert len(vs.got) == len(set(sigs_of(vs)))                            # no duplicate


def test_socket_and_backfill_do_not_handle_a_transaction_twice(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 5)
    vs.state["ws_handled"] = {WAL: [f"{P}-2", f"{P}-3"]}                   # handled live (kept in the saved state)
    vs.seen.append(f"{P}-4")                                                # and one still in memory
    vs.catch_up(chain, "reconnect")
    assert sorted(sigs_of(vs)) == [f"{P}-1", f"{P}-5"]
    assert vs.state["ws_handled"] == {}                                     # the checkpoint moved past them


def test_ws_handle_records_the_signature_for_the_next_catch_up(tmp_path):
    chain = Chain()
    w, vs = stream(tmp_path, chain, fake_handle=False)
    chain.txs["s1"] = empty_tx("s1")
    vs.handle(WAL, "s1", 1.0, "ws")
    assert vs.state["ws_handled"][WAL] == ["s1"]
    vs.handle(WAL, "s1", 1.0, "backfill")                                  # a duplicate is not handled again
    assert vs.stat["handled"] == 1


def test_rpc_failure_keeps_the_checkpoint_and_the_next_round_gets_everything(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 30)
    chain.fail_next = 1                                    # 429 after Rpc's own retries
    vs.catch_up(chain, "reconnect")
    assert vs.got == [] and vs.state["poll_last"][WAL] == f"{P}-0" and vs.bstat["errors"] == 1
    vs.catch_up(chain, "periodic")
    assert len(vs.got) == 30 and vs.state["poll_last"][WAL] == f"{P}-30"


def test_failure_mid_way_records_a_gap_for_the_unread_part(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 1200)
    n = {"k": 0}

    def flaky(m, p):
        n["k"] += 1
        if n["k"] == 2:
            raise RuntimeError("HTTP 503")
        return chain(m, p)
    vs.catch_up(flaky, "reconnect")
    assert len(vs.got) == 1000 and len(vs.gaps(open_only=True)) == 1
    vs.fill_gaps(chain)
    assert len(set(sigs_of(vs))) == 1200 == len(vs.got) and vs.gaps(open_only=True) == []


def test_reconnect_and_resume_schedule_a_catch_up(tmp_path):
    chain = Chain()
    w, vs = stream(tmp_path, chain)
    vs.catchup_due = None
    stop = threading.Event()

    def boom():
        stop.set()
        raise ConnectionError("socket dropped")
    vs.connect = boom
    asyncio.run(vs.run(stop))
    assert vs.catchup_due == "reconnect"
    vs.catchup_due = None
    vs.set_paused(True)
    assert vs.catchup_due is None
    vs.set_paused(False)
    assert vs.catchup_due == "resume"


def test_poll_more_than_one_round_pages_back_instead_of_dropping(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    vs.wallets = lambda: {WAL: "x"}
    chain.grow(WAL, 50)                                    # POLL_LIMIT is 20
    vs.poll_once(chain)
    assert sorted(sigs_of(vs)) == sorted(f"{P}-{i}" for i in range(1, 51))


def test_a_transaction_not_served_is_retried_then_kept_as_a_gap(tmp_path):
    chain = Chain()
    w, vs = stream(tmp_path, chain, fake_handle=False)
    vs.handle(WAL, "lost", 1.0, "ws")                      # getTransaction gives nothing
    assert [r["sig"] for r in vs.state["retry"]] == ["lost"] and "lost" not in vs.seen
    for _ in range(VP.RETRY_TRIES + 1):
        vs.fill_gaps(chain)
    assert vs.state["retry"] == []
    g = vs.gaps(open_only=True)
    assert len(g) == 1 and g[0]["before"] == "lost" and g[0]["until"] is None
    vs.handle(WAL, "late", 1.0, "ws")
    chain.txs["late"] = empty_tx("late")                   # served on the retry: done, no new gap
    vs.fill_gaps(chain)
    assert vs.state["retry"] == [] and len(vs.gaps(open_only=True)) == 1 and "late" in vs.seen


# ---- the mint tail (track.py)
def _tracker(tmp_path, chain):
    st = T.Store(tmp_path / "t.db", now=lambda: 1_000_000.0)
    return T.Tracker(st, chain, now=lambda: 1_000_000.0, budget=T.Budget(10_000))


def _live_mint(t, newest=None):
    t.s.x("INSERT INTO mints (mint, status, live, history, history_state, verified, newest_sig, removed) "
          "VALUES (?, 'live', 1, 0, 'done', 'mint', ?, 0)", (MINT_B, newest))


def test_mint_tail_over_1000_signatures_is_paged_and_queued_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "LIVE_MAX_PAGES", 2)
    monkeypatch.setattr(T, "QUEUE_MAX", 10_000)
    chain = Chain({MINT_B: 1})
    t = _tracker(tmp_path, chain)
    _live_mint(t, f"{M}-0")
    chain.grow(MINT_B, 2500)
    t.step_live(t.s.mint(MINT_B))
    assert t.s.q("SELECT COUNT(*) n FROM queue")[0]["n"] == 2000
    g = t.s.gaps(scope="mint")
    assert len(g) == 1 and g[0]["until_sig"] == f"{M}-0" and g[0]["before_sig"] == f"{M}-501"
    assert t.s.mint(MINT_B)["newest_sig"] == f"{M}-2500"
    t.step_gaps()
    assert t.s.gaps(scope="mint") == []
    assert {r["sig"] for r in t.s.q("SELECT sig FROM queue")} == {f"{M}-{i}" for i in range(1, 2501)}


def test_a_missing_transaction_and_a_full_queue_are_open_gaps_requeued_later(tmp_path, monkeypatch):
    chain = Chain()
    t = _tracker(tmp_path, chain)
    _live_mint(t)
    t.s.x("INSERT INTO queue (sig, mint, why, added) VALUES ('gone', ?, 'live', 1)", (MINT_B,))
    t.step_parse(limit=5)                                  # getTransaction returns nothing
    g = t.s.gaps(scope="mint-tx")
    assert [x["before_sig"] for x in g] == ["gone"] and "getTransaction" in g[0]["reason"]
    t.step_gaps()                                          # tried again through the queue
    assert t.s.q("SELECT sig, why FROM queue") == [{"sig": "gone", "why": "gap"}]
    for _ in range(6):                                     # never served: given up after 5, still listed as open
        t.step_parse(limit=5)
        t.step_gaps()
    assert any(x["note"] for x in t.s.gaps(scope="mint-tx"))
    t.s.x("DELETE FROM queue")
    monkeypatch.setattr(T, "QUEUE_MAX", 3)
    for i in range(6):
        t.s.x("INSERT INTO queue (sig, mint, why, added) VALUES (?, ?, 'live', ?)", (f"q{i}", MINT_B, 10 + i))
    assert t.trim_queue() == 3
    over = [x for x in t.s.gaps(scope="mint-tx") if x["reason"] == "hàng đợi đầy"]
    assert len(over) == 3 and all(x["before_sig"].startswith("q") for x in over)   # each one recorded, none silent


def test_rpc_calls_are_metered_by_worker_method_and_bytes(tmp_path):
    chain = Chain({MINT_B: 3})
    t = _tracker(tmp_path, chain)
    t._call("getSignaturesForAddress", [MINT_B, {"limit": 1000}])
    chain.fail_next = 1
    with pytest.raises(RuntimeError):
        t._call("getTransaction", ["x", {}])
    rows = {r["method"]: r for r in t.s.usage_rows()}
    assert rows["getSignaturesForAddress"]["calls"] == 1 and rows["getSignaturesForAddress"]["bytes"] > 100
    assert rows["getTransaction"]["errors"] == 1 and rows["getTransaction"]["worker"] == "track"
