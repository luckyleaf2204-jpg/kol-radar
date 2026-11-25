"""Gate B audit: the failure cases found in the final review, pinned here. A mutation run (each bug put back in a
throw-away copy) showed 7 of the fixes make their test fail when removed; the migration rollback is belt and braces
(SQLite already drops an uncommitted transaction). Fake RPCs and local files only: a real network call fails."""
import json
import socket
import sqlite3
import threading
import time
import urllib.request

import pytest

from insiders import backup as B
from insiders import track as T
from insiders import vip as VP
from insiders import wallets as WH
from insiders import watch as WT

from test_insiders_gate_b2 import MINT_B, P, WAL, Chain, empty_tx, stream

M = MINT_B[:4]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call in a Gate B test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Crash(BaseException):
    """The process dies (not an exception the code may catch)."""


# ------------------------------------------------------------------------------- B2: crash / race / ordering
def test_a_crash_mid_backfill_keeps_the_old_checkpoint_and_the_restart_loses_nothing(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    w.save()
    chain.grow(WAL, 1500)
    done = []

    def dying_handle(a, sig, observed=None, source="poll"):
        if len(done) == 700:
            raise Crash()
        done.append(sig)
        vs.seen.append(sig)
        w.save()                                           # the real handle() saves the state as it goes
    vs.handle = dying_handle
    vs._run_items = lambda items: [vs.handle(*i) for i in items] and len(items)   # one thread: a clean crash point
    with pytest.raises(Crash):
        vs.catch_up(chain, "start")
    saved = json.loads((tmp_path / "w.json").read_text(encoding="utf-8"))
    assert saved["vip"]["poll_last"][WAL] == f"{P}-0"      # the checkpoint did not move past unhandled ones
    w2, vs2 = stream(tmp_path, chain)                      # restart
    vs2.backfill_round(chain)
    assert set(done) | {s for _, s, _ in vs2.got} == {f"{P}-{i}" for i in range(1, 1501)}
    assert vs2.state["poll_last"][WAL] == f"{P}-1500"


def test_a_crash_mid_poll_round_keeps_the_checkpoint(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    vs.wallets = lambda: {WAL: "x"}
    chain.grow(WAL, 10)

    def dying(*a, **k):
        raise Crash()
    vs.handle = dying
    vs._run_items = lambda items: [vs.handle(*i) for i in items] and len(items)
    with pytest.raises(Crash):
        vs.poll_once(chain)
    assert vs.state["poll_last"][WAL] == f"{P}-0"


def test_a_gap_cursor_moves_only_after_its_page_is_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(VP, "CATCHUP_PAGES", 1)
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 1500)
    vs.catch_up(chain, "reconnect")
    before = dict(vs.gaps(open_only=True)[0])
    vs.handle = lambda *a, **k: (_ for _ in ()).throw(Crash())
    vs._run_items = lambda items: [vs.handle(*i) for i in items] and len(items)
    with pytest.raises(Crash):
        vs.fill_gaps(chain)
    g = vs.gaps(open_only=True)[0]
    assert (g["before"], g["filled"], g["closed"]) == (before["before"], 0, None)


def test_socket_and_backfill_racing_on_one_signature_handle_it_once(tmp_path):
    chain = Chain()
    w, vs = stream(tmp_path, chain, fake_handle=False)
    calls = []

    def slow(m, p):
        calls.append(m)
        time.sleep(0.05)
        return empty_tx(p[0])
    vs.rpc = slow
    ts = [threading.Thread(target=vs.handle, args=(WAL, "same", 1.0, src)) for src in ("ws", "backfill", "poll")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls.count("getTransaction") == 1 and vs.stat["handled"] == 1


def test_an_analysis_crash_is_retried_not_lost(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain, fake_handle=False)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    chain.grow(WAL, 1)
    chain.txs[f"{P}-1"] = empty_tx(f"{P}-1")
    real = VP.analyse
    boom = {"on": True}

    def flaky(tx, wallet):
        if boom["on"]:
            raise KeyError("odd transaction")
        return real(tx, wallet)
    VP.analyse = flaky
    try:
        vs.catch_up(chain, "reconnect")
        assert [r["sig"] for r in vs.state["retry"]] == [f"{P}-1"]
        boom["on"] = False
        vs.fill_gaps(chain)
    finally:
        VP.analyse = real
    assert vs.state["retry"] == [] and f"{P}-1" in vs.seen and vs.stat["handled"] == 2


def test_junk_rpc_answer_is_an_incomplete_read_not_a_crash(tmp_path):
    chain = Chain({WAL: 1})
    w, vs = stream(tmp_path, chain)
    vs.state["poll_last"] = {WAL: f"{P}-0"}
    vs.catch_up(lambda m, p: {"error": "odd"}, "reconnect")
    assert vs.state["poll_last"][WAL] == f"{P}-0" and vs.bstat["errors"] == 1


def test_the_periodic_catch_up_is_hourly_by_default():
    assert VP.CATCHUP_EVERY_S == 3600


# ------------------------------------------------------------------------------- B2: the mint tail
def _tracker(tmp_path, chain):
    st = T.Store(tmp_path / "t.db", now=lambda: 1_000_000.0)
    t = T.Tracker(st, chain, now=lambda: 1_000_000.0, budget=T.Budget(10_000))
    t.s.x("INSERT INTO mints (mint, status, live, history, history_state, verified, newest_sig, removed, errors) "
          "VALUES (?, 'live', 1, 0, 'done', 'mint', ?, 0, 4)", (MINT_B, f"{M}-0"))
    return t


def test_an_rpc_failure_on_page_two_keeps_page_one_and_opens_a_gap(tmp_path):
    chain = Chain({MINT_B: 1})
    t = _tracker(tmp_path, chain)
    chain.grow(MINT_B, 1500)
    real = chain.__call__
    n = {"k": 0}

    def flaky(m, p):
        n["k"] += 1
        if n["k"] == 2:
            raise RuntimeError("HTTP 503")
        return real(m, p)
    t.rpc = flaky
    t.step_live(t.s.mint(MINT_B))
    assert t.s.q("SELECT COUNT(*) n FROM queue")[0]["n"] == 1000
    assert len(t.s.gaps(scope="mint")) == 1 and t.coins()[0]["gaps_open"] == 1    # never shown as complete
    assert t.s.mint(MINT_B)["errors"] == 0                                         # a good poll resets the count
    t.rpc = chain
    t.step_gaps()
    assert t.s.q("SELECT COUNT(*) n FROM queue")[0]["n"] == 1500 and t.coins()[0]["gaps_open"] == 0


def test_a_transaction_that_always_fails_does_not_block_the_queue(tmp_path):
    chain = Chain()
    t = _tracker(tmp_path, chain)
    for i, sig in enumerate(["bad", "good1", "good2"]):
        t.s.x("INSERT INTO queue (sig, mint, why, added) VALUES (?, ?, 'live', ?)", (sig, MINT_B, i))
    chain.txs = {"good1": empty_tx("good1"), "good2": empty_tx("good2")}
    real = chain.__call__
    t.rpc = lambda m, p: (_ for _ in ()).throw(RuntimeError("final error")) if p[0] == "bad" else real(m, p)
    for _ in range(3):
        t.step_parse(limit=8)
    assert t.s.q("SELECT COUNT(*) n FROM queue")[0]["n"] == 0                      # good ones went through
    assert [g["before_sig"] for g in t.s.gaps(scope="mint-tx")] == ["bad"]


def test_a_decoding_crash_is_a_gap_not_a_lost_transaction(tmp_path, monkeypatch):
    chain = Chain()
    t = _tracker(tmp_path, chain)
    t.s.x("INSERT INTO queue (sig, mint, why, added) VALUES ('odd', ?, 'live', 1)", (MINT_B,))
    chain.txs["odd"] = empty_tx("odd")
    monkeypatch.setattr(T, "parse_mint_events", lambda tx, mint: (_ for _ in ()).throw(KeyError("x")))
    t.step_parse(limit=8)
    g = t.s.gaps(scope="mint-tx")
    assert [x["before_sig"] for x in g] == ["odd"] and "giải mã" in g[0]["reason"]


def test_a_gap_already_waiting_in_the_queue_is_not_queued_twice(tmp_path):
    chain = Chain()
    t = _tracker(tmp_path, chain)
    t.s.x("INSERT INTO queue (sig, mint, why, added) VALUES ('s', ?, 'live', 1)", (MINT_B,))
    t.s.open_gap("mint-tx", MINT_B, "s", None, "test")
    t.step_gaps()
    assert t.s.q("SELECT why FROM queue") == [{"why": "live"}] and len(t.s.gaps(scope="mint-tx")) == 1


# ------------------------------------------------------------------------------- B1: backup / restore / migration
def _files(d):
    (d / "insider_watch.json").write_text('{"v": 1}', encoding="utf-8")
    st = T.Store(d / "insiders_track.db")
    st.x("INSERT INTO mints (mint, name) VALUES ('M', 'ONE')")
    st.db.close()


def test_restoring_the_oldest_backup_works_when_the_folder_is_full(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "KEEP", 3)
    _files(tmp_path)
    oldest = B.backup(tmp_path, keep=3, now=lambda: 1000.0)
    for i in range(2):
        B.backup(tmp_path, keep=3, now=lambda i=i: 2000.0 + i)
    (tmp_path / "insider_watch.json").write_text('{"v": 2}', encoding="utf-8")
    B.restore(oldest, tmp_path, now=lambda: 3000.0)       # its own pre-restore copy must not prune it away
    assert json.loads((tmp_path / "insider_watch.json").read_text(encoding="utf-8")) == {"v": 1}
    assert oldest.exists()


def test_a_corrupt_database_with_a_matching_manifest_is_refused(tmp_path):
    _files(tmp_path)
    d = B.backup(tmp_path)
    db = d / "insiders_track.db"
    raw = bytearray(db.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)                # page 1 smashed
    db.write_bytes(bytes(raw))
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    man["files"]["insiders_track.db"].update(sha256=B.sha256(db), bytes=db.stat().st_size)   # a "consistent" manifest
    (d / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    probs = B.verify(d)
    assert probs and "insiders_track.db" in probs[0]
    with pytest.raises(ValueError):
        B.restore(d, tmp_path / "safe")


def test_a_missing_manifest_or_file_is_refused(tmp_path):
    _files(tmp_path)
    d = B.backup(tmp_path)
    (d / "insider_watch.json").unlink()
    assert any("thiếu file" in p for p in B.verify(d))
    (d / "manifest.json").unlink()
    assert B.verify(d) == ["manifest.json thiếu hoặc hỏng"]


def test_a_failing_migration_leaves_the_old_schema_and_rows(tmp_path, monkeypatch):
    p = tmp_path / "insiders_track.db"
    st = T.Store(p)
    st.x("INSERT INTO mints (mint, name) VALUES ('M', 'ONE')")
    st.db.close()
    monkeypatch.setattr(T, "MIGRATIONS", T.MIGRATIONS + ["CREATE TABLE fine (a); ALTER TABLE nope ADD COLUMN x;"])
    with pytest.raises(sqlite3.OperationalError):
        T.Store(p)
    con = sqlite3.connect(p)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 6
    assert con.execute("SELECT name FROM mints").fetchall() == [("ONE",)]
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='fine'").fetchall()    # all or nothing
    con.close()
    assert (tmp_path / "insiders_track.db.v6.bak").exists()


def test_v1_database_with_data_migrates_to_v4_and_keeps_everything(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(T.MIGRATIONS[0])
    con.execute("PRAGMA user_version=1")
    con.execute("INSERT INTO mints (mint, name, newest_sig, pages) VALUES ('M', 'OLD', 'CP', 7)")
    con.execute("INSERT INTO events (sig, wallet, mint, kind) VALUES ('s', 'w', 'M', 'BUY')")
    con.execute("INSERT INTO edges (src, dst, asset, amount, sig) VALUES ('a', 'b', 'SOL', 1, 'e')")
    con.commit()
    before = {t: n for t, n in B.table_rows(p).items()}
    con.close()
    st = T.Store(p)
    after = B.table_rows(p)
    assert st.version() == 6 and all(after[t] == n for t, n in before.items())
    assert st.mint("M")["newest_sig"] == "CP" and st.mint("M")["pages"] == 7
    assert {"gaps", "rpc_usage", "wallets", "wallet_queue", "wallet_sigs", "wallet_trades", "wallet_pnl"} <= set(after)


# ------------------------------------------------------------------------------- B3: PnL edge cases / failures
A = "7GSMwnWB5YWmCwQax4bzfeDMvSZEogp6TAehxrJddDyr"
COIN = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"


def test_selling_while_holding_received_tokens_is_insufficient_even_if_buys_cover_it():
    r = WH.pair_pnl([{"kind": "BUY", "tokens": 1000, "sol": 1.0, "ts": 1},
                     {"kind": "RECEIVE", "tokens": 1000, "sol": 0, "ts": 2},
                     {"kind": "SELL", "tokens": 1000, "sol": 3.0, "ts": 3}])
    assert r["status"] == WH.INSUFFICIENT and "không rõ giá vốn" in r["reason"]


def test_a_launch_without_tokens_is_not_a_receive():
    r = WH.pair_pnl([{"kind": "CREATE", "tokens": 0, "sol": 0, "ts": 1},
                     {"kind": "BUY", "tokens": 10, "sol": 1.0, "ts": 2},
                     {"kind": "SELL", "tokens": 10, "sol": 2.0, "ts": 3}])
    assert r["receives"] == 0 and r["status"] == "OK" and r["realized_sol"] == pytest.approx(1.0)


def test_fees_are_not_counted_twice():
    t = [{"kind": "BUY", "tokens": 10, "sol": 1.0, "ts": 1, "fee_sol": 0.001, "fee_in_sol": 1},    # read off the balance
         {"kind": "SELL", "tokens": 10, "sol": 2.0, "ts": 2, "fee_sol": 0.001, "fee_in_sol": 0}]   # pool side: apart
    r = WH.pair_pnl(t)
    assert r["fees_sol"] == pytest.approx(0.002) and r["fees_outside_sol"] == pytest.approx(0.001)
    assert r["realized_sol"] == pytest.approx(1.0) and r["realized_net_sol"] == pytest.approx(0.999)


def test_ingest_marks_whether_the_fee_is_inside_the_trade_sol(tmp_path):
    st = T.Store(tmp_path / "t.db")
    h = WH.WalletHistory(st, lambda m, p: None, enabled=True)
    tx = {"blockTime": 5, "meta": {"err": None, "fee": 5000, "logMessages": [], "innerInstructions": [],
                                   "preTokenBalances": [{"owner": A, "mint": COIN, "uiTokenAmount": {"amount": "0"}}],
                                   "postTokenBalances": [{"owner": A, "mint": COIN, "uiTokenAmount": {"amount": "10"}}],
                                   "preBalances": [3 * 10 ** 9], "postBalances": [2 * 10 ** 9 - 5000]},
          "transaction": {"signatures": ["s"], "message": {"accountKeys": [{"pubkey": A}], "instructions": []}}}
    h.ingest(A, tx)
    r = st.q("SELECT * FROM wallet_trades")[0]
    assert r["kind"] == "BUY" and r["fee_sol"] == pytest.approx(5e-6) and r["fee_in_sol"] == 1


class WChain:
    def __init__(self):
        self.calls = []

    def __call__(self, m, p):
        self.calls.append((m, p[0]))
        if m == "getSignaturesForAddress":
            return [{"signature": s, "blockTime": t, "err": None} for s, t in (("good", 20), ("bad", 10))]
        if p[0] == "bad":
            raise RuntimeError("HTTP 503")
        return {"blockTime": 20, "meta": {"err": None, "fee": 0, "preTokenBalances": [], "postTokenBalances": [],
                                          "preBalances": [0], "postBalances": [0], "innerInstructions": []},
                "transaction": {"signatures": ["good"], "message": {"accountKeys": [{"pubkey": A}], "instructions": []}}}


def _wh(tmp_path, chain):
    st = T.Store(tmp_path / "t.db", now=lambda: 1_000_000.0)
    h = WH.WalletHistory(st, chain, now=lambda: 1_000_000.0, enabled=True, budget=T.Budget(10_000))
    st.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (A,))
    st.x("INSERT INTO wallet_queue (wallet, priority, state) VALUES (?, 0, 'todo')", (A,))
    return h


def test_a_failing_wallet_transaction_is_tried_5_times_then_a_gap_and_the_wallet_is_incomplete(tmp_path):
    chain = WChain()
    h = _wh(tmp_path, chain)
    h.step_sigs()
    for _ in range(12):
        h.step_parse()
    assert [c for c in chain.calls if c == ("getTransaction", "bad")].__len__() == 5
    assert h.s.q("SELECT parsed FROM wallet_sigs WHERE sig='good'")[0]["parsed"] == 1        # not blocked
    q = h.s.q("SELECT state FROM wallet_queue")[0]
    assert q["state"] == "incomplete"                                                         # never "done"
    sm = h.summary(A)
    assert sm["gaps_open"] == 1 and sm["realized_sol"] == WH.INSUFFICIENT and sm["win_rate"] == WH.INSUFFICIENT
    assert h.retry_missing(A) == 1 and h.summary(A)["gaps_open"] == 0
    assert h.s.q("SELECT state FROM wallet_queue")[0]["state"] == "parsing"


def test_an_interrupted_wallet_history_resumes_from_its_cursor(tmp_path):
    class Pages:
        def __init__(self):
            self.sigs = [{"signature": f"w{i}", "blockTime": 9000 - i, "err": None} for i in range(2500)]
            self.calls = []

        def __call__(self, m, p):
            self.calls.append(p[1].get("before"))
            names = [s["signature"] for s in self.sigs]
            out = self.sigs[names.index(p[1]["before"]) + 1:] if p[1].get("before") else self.sigs
            return out[:1000]
    chain = Pages()
    h = _wh(tmp_path, chain)
    h.step_sigs()                                          # page 1, then the service stops
    h2 = WH.WalletHistory(T.Store(tmp_path / "t.db"), chain, enabled=True, budget=T.Budget(10_000))
    h2.step_sigs()
    h2.step_sigs()
    assert chain.calls == [None, "w999", "w1999"]
    assert h2.s.q("SELECT COUNT(*) n FROM wallet_sigs")[0]["n"] == 2500


def test_legacy_pnl_never_makes_a_wallet_verified(tmp_path):
    st = T.Store(tmp_path / "t.db")
    h = WH.WalletHistory(st, lambda m, p: None, enabled=False)
    r = {"vip": {"holders": [{"wallet": A}]}, "pnl": {"wallets": [{"wallet": A, "net_sol": 99999, "wins": 50, "coins": 50}]}}
    h.seed(r)
    sm = h.summary(A)
    assert sm["verified"] is False and sm["realized_sol"] == WH.INSUFFICIENT and sm["legacy"]["net_sol"] == 99999
    assert not [x for x in st.q("SELECT label FROM wallets") if "VERIFIED" in (x["label"] or "").upper()]


def test_gate_b_modules_have_no_signing_or_sending_code():
    import re
    from pathlib import Path
    bad = re.compile(r"sendTransaction|sendRawTransaction|signTransaction|Keypair|secretKey|private_key|/swap/v1/swap\b")
    for f in ("backup.py", "wallets.py", "track.py", "vip.py"):
        assert not bad.search((Path(WT.__file__).parent / f).read_text(encoding="utf-8")), f
