"""The Helius probe, tested offline: the 50-request hard stop (ledger + anchor + lock, reset / tamper / crash /
concurrency / restarts), the per-phase budgets, the late-transaction classification and the credit report.
Fake HTTP only: a real network call fails the test. No test touches the real anchor in the home folder."""
import io
import json
import socket
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from insiders import probe as PR

from test_insiders_gate_b2 import Chain

ROOT = Path(PR.__file__).parent.parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def refuse(*a, **k):
        raise AssertionError("network call in a probe test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(PR, "ANCHOR", tmp_path / "home" / "anchor.json")       # never the real one


class Crash(BaseException):
    pass


class Resp:
    def __init__(self, obj, status=200):
        self.status, self.headers = status, {}
        self._b = json.dumps(obj).encode()

    def read(self):
        return self._b


class Opener:
    """JSON-RPC from a fake chain; mode: every answer a 429 / a timeout instead."""

    def __init__(self, chain=None, mode=None):
        self.chain, self.mode, self.n = chain or Chain(), mode, 0

    def __call__(self, req):
        self.n += 1
        if self.mode == "429":
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {"Retry-After": "0"}, io.BytesIO(b""))
        if self.mode == "timeout":
            raise TimeoutError("timed out")
        body = json.loads(req.data)
        if body["method"] == "getAccountInfo":
            return Resp({"jsonrpc": "2.0", "result": None, "id": 1})
        return Resp({"jsonrpc": "2.0", "result": self.chain(body["method"], body["params"]), "id": 1})


def new_ledger(d: Path, **kw) -> PR.Ledger:
    """A fresh, initialised ledger under d with its own anchor and a held lock."""
    lock = PR.ProcessLock(d / "home" / "probe.lock")
    lock.acquire()
    lg = PR.Ledger(d / "out" / "ledger.jsonl", anchor=d / "home" / "anchor.json", lock=lock, **kw)
    lg.init()
    return lg


def reopen(d: Path, repair: bool = False) -> PR.Ledger:
    """A later process reading the ledger (with its own lock when it may move the anchor forward)."""
    lock = PR.ProcessLock(d / "home" / "probe2.lock") if repair else None
    if lock:
        lock.acquire()
    try:
        return PR.Ledger(d / "out" / "ledger.jsonl", anchor=d / "home" / "anchor.json", lock=lock).open()
    finally:
        if lock:
            lock.release()


def transport(lg, opener, log):
    return PR.Transport("https://example.invalid/", lg, log, opener=opener, sleep=lambda s: None)


# ------------------------------------------------------------------------------------------------ budget
def test_phase_caps_are_hard_sum_under_50_and_cover_the_plan():
    assert PR.CAP == 50 and sum(PR.PHASE_CAPS.values()) == 48 <= PR.CAP
    assert all(sum(PR.PLAN[p][1].values()) <= c for p, c in PR.PHASE_CAPS.items())
    assert PR.PLANNED == 36


def test_an_inconsistent_budget_refuses_to_load():
    code = ("import insiders.probe as p, importlib, sys; src=open(p.__file__,encoding='utf-8').read()"
            ".replace('PHASE_CAPS = {1: 8,', 'PHASE_CAPS = {1: 18,');"
            "exec(compile(src, 'probe_mut', 'exec'), {'__name__': 'probe_mut'})")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    assert out.returncode != 0 and "inconsistent" in out.stderr


def test_a_phase_stops_at_its_own_cap_and_leftovers_never_move(tmp_path):
    lg = new_ledger(tmp_path)
    op = Opener(mode="429")
    r = PR.Runner(transport(lg, op, tmp_path / "req.jsonl"), 2, tmp_path)
    with pytest.raises(PR.HardStop):
        for _ in range(10):
            r.call("getAccountInfo", ["x", {}], "e")
    assert lg.used(2) == PR.PHASE_CAPS[2] == 3 and op.n == 3
    # phase 4 (cap 2) cannot borrow from what phases 1, 3, 5, 6 have not used
    r4 = PR.Runner(transport(lg, op, tmp_path / "req.jsonl"), 4, tmp_path)
    with pytest.raises(PR.HardStop):
        for _ in range(10):
            r4.call("getTransaction", ["x"], "d")
    assert lg.used(4) == 2 and lg.used() == 5 and lg.left(1) == 8


def test_each_request_is_recorded_before_it_is_sent(tmp_path):
    lg = new_ledger(tmp_path)
    seen = []

    def opener(req):
        seen.append((reopen(tmp_path).used(), json.loads(req.data)["id"]))
        return Resp({"jsonrpc": "2.0", "result": None, "id": 1})
    t = transport(lg, opener, tmp_path / "req.jsonl")
    for _ in range(3):
        t.post("getTransaction", ["s"], 3, "x")
    assert seen == [(1, 1), (2, 2), (3, 3)]                      # on disk before the request, ids = slots


# ------------------------------------------------------------------------------------------------ restarts
def test_no_sequence_of_restarts_gets_past_50_or_a_phase_cap(tmp_path):
    new_ledger(tmp_path).lock.release()
    op = Opener(mode="429")
    for k in range(200):                                          # 200 process starts, every phase, all 429
        phase = 1 + k % 6
        lock = PR.ProcessLock(tmp_path / "home" / "probe.lock")
        lock.acquire()
        try:
            lg = PR.Ledger(tmp_path / "out" / "ledger.jsonl", anchor=tmp_path / "home" / "anchor.json", lock=lock).open()
            r = PR.Runner(transport(lg, op, tmp_path / "req.jsonl"), phase, tmp_path)
            try:
                for _ in range(20):
                    r.call("getSignaturesForAddress", ["x", {}], "s")
            except PR.HardStop:
                pass
        finally:
            lock.release()
    lg = reopen(tmp_path)
    assert op.n == 48 == lg.used() <= PR.CAP
    assert all(lg.used(p) == c for p, c in PR.PHASE_CAPS.items())
    assert len((tmp_path / "req.jsonl").read_text(encoding="utf-8").splitlines()) == 48


# ------------------------------------------------------------------------------------------------ reset / tamper
def test_deleting_the_ledger_or_the_output_folder_is_refused(tmp_path):
    lg = new_ledger(tmp_path)
    lg.path.unlink()
    with pytest.raises(PR.LedgerError, match="mất"):
        lg.open()
    with pytest.raises(PR.LedgerError, match="không khởi tạo lại"):  # and no fresh start while the anchor exists
        lg.init()


def test_a_truncated_ledger_is_refused(tmp_path):
    lg = new_ledger(tmp_path)
    t = transport(lg, Opener(), tmp_path / "req.jsonl")
    for _ in range(3):
        t.post("getTransaction", ["s"], 3, "x")
    lines = lg.path.read_text(encoding="utf-8").splitlines()
    lg.path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    with pytest.raises(PR.LedgerError, match="ngắn hơn"):
        lg.open()


def test_an_edited_line_breaks_the_hash_chain(tmp_path):
    lg = new_ledger(tmp_path)
    transport(lg, Opener(), tmp_path / "req.jsonl").post("getTransaction", ["s"], 3, "x")
    s = lg.path.read_text(encoding="utf-8").replace('"phase": 3', '"phase": 4')
    lg.path.write_text(s, encoding="utf-8")
    with pytest.raises(PR.LedgerError, match="chuỗi băm"):
        lg.open()


def test_a_replaced_ledger_from_a_new_genesis_is_refused(tmp_path):
    lg = new_ledger(tmp_path)
    transport(lg, Opener(), tmp_path / "req.jsonl").post("getTransaction", ["s"], 3, "x")
    other = tmp_path / "other"
    fake = new_ledger(other)                                  # a perfectly valid ledger of another campaign
    lg.path.write_bytes(fake.path.read_bytes())
    with pytest.raises(PR.LedgerError):
        lg.open()


def test_a_moved_output_folder_is_refused(tmp_path):
    lg = new_ledger(tmp_path)
    moved = PR.Ledger(tmp_path / "elsewhere" / "ledger.jsonl", anchor=lg.anchor, lock=lg.lock)
    moved.path.parent.mkdir()
    moved.path.write_bytes(lg.path.read_bytes())
    with pytest.raises(PR.LedgerError, match="trỏ tới ledger khác"):
        moved.open()


def test_missing_or_unreadable_anchor_or_ledger_refuses(tmp_path):
    lg = new_ledger(tmp_path)
    lg.anchor.write_text("{not json", encoding="utf-8")
    with pytest.raises(PR.LedgerError, match="anchor hỏng"):
        lg.open()
    lg.anchor.unlink()
    with pytest.raises(PR.LedgerError, match="anchor bị xoá"):
        lg.open()
    fresh = tmp_path / "f"
    with pytest.raises(PR.LedgerError, match="chưa --init"):
        PR.Ledger(fresh / "ledger.jsonl", anchor=fresh / "a.json").open()


def test_caps_changed_in_code_refuse_the_old_ledger(tmp_path):
    lg = new_ledger(tmp_path)
    more = PR.Ledger(lg.path, anchor=lg.anchor, lock=lg.lock, phase_caps={**PR.PHASE_CAPS, 1: 20})
    with pytest.raises(PR.LedgerError, match="trần"):
        more.open()


def test_main_init_twice_and_run_without_init_are_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HELIUS_API_KEY", "test-not-a-key")
    out = str(tmp_path / "out")
    assert PR.main(["--run", "--phase", "1", "--approval", "ok", "--out", out]) == 3   # no ledger yet
    assert PR.main(["--init", "--out", out]) == 0
    assert PR.main(["--init", "--out", out]) == 3                                      # no reset
    assert PR.main(["--init", "--out", str(tmp_path / "out2")]) == 3                   # nor a second campaign
    assert "không khởi tạo lại" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ crashes
def test_crash_just_before_the_ledger_write_sends_nothing_counts_nothing(tmp_path):
    lg = new_ledger(tmp_path)
    op = Opener()
    lg.before_append = lambda rec: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        transport(lg, op, tmp_path / "req.jsonl").post("getTransaction", ["s"], 3, "x")
    assert op.n == 0 and reopen(tmp_path).used() == 0


def test_crash_after_the_ledger_write_before_the_anchor_counts_the_slot_and_sends_nothing(tmp_path):
    lg = new_ledger(tmp_path)
    op = Opener()
    lg.after_append = lambda rec: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        transport(lg, op, tmp_path / "req.jsonl").post("getTransaction", ["s"], 3, "x")
    assert op.n == 0
    assert json.loads(lg.anchor.read_text(encoding="utf-8"))["n"] == 0       # anchor one behind
    lg2 = reopen(tmp_path, repair=True)                                       # accepted, anchor moved forward
    assert lg2.used() == 1 and json.loads(lg.anchor.read_text(encoding="utf-8"))["n"] == 1


def test_crash_after_the_record_during_the_request_counts_the_slot(tmp_path):
    lg = new_ledger(tmp_path)

    def dying(req):
        raise Crash()
    with pytest.raises(Crash):
        transport(lg, dying, tmp_path / "req.jsonl").post("getTransaction", ["s"], 3, "x")
    assert reopen(tmp_path).used() == 1                                       # never under-counted


def test_a_torn_last_line_refuses_to_run(tmp_path):
    lg = new_ledger(tmp_path)
    with open(lg.path, "a", encoding="utf-8") as f:
        f.write('{"i": 1, "kind": "slot", "pha')                              # power cut mid-write
    with pytest.raises(PR.LedgerError, match="hỏng"):
        lg.open()


def test_a_timeout_takes_its_slot(tmp_path):
    lg = new_ledger(tmp_path)
    op = Opener(mode="timeout")
    r = PR.Runner(transport(lg, op, tmp_path / "req.jsonl"), 3, tmp_path)
    out = r.call("getTransaction", ["x", {}], "e")
    assert out["_failed"] and out["class"] == "TIMEOUT" and op.n == 3 and lg.used() == 3


# ------------------------------------------------------------------------------------------------ concurrency
def test_a_second_probe_is_refused_while_one_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test-not-a-key")
    lock = PR.ProcessLock(PR.ANCHOR.parent / "probe.lock")
    lock.acquire()
    try:
        assert PR.main(["--run", "--phase", "1", "--approval", "ok", "--out", str(tmp_path / "o")]) == 3
        assert PR.main(["--init", "--out", str(tmp_path / "o")]) == 3
    finally:
        lock.release()


def test_two_processes_racing_for_the_lock_only_one_wins(tmp_path):
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from insiders import probe as PR
        l = PR.ProcessLock(__import__('pathlib').Path({str(tmp_path / 'race.lock')!r}))
        while time.time() < float(sys.argv[1]): pass
        try:
            l.acquire(); print("WON"); time.sleep(1.5); l.release()
        except PR.LedgerError:
            print("REFUSED")
    """)
    import time as _t
    go = str(_t.time() + 2.5)
    ps = [subprocess.Popen([sys.executable, "-c", script, go], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    res = sorted(p.communicate(timeout=30)[0].strip() for p in ps)
    assert res.count("WON") == 1 and res.count("REFUSED") == 3


def test_a_leftover_lock_after_a_crash_fails_closed(tmp_path):
    p = tmp_path / "home" / "probe.lock"
    p.parent.mkdir(parents=True)
    p.write_text('{"pid": 1}', encoding="utf-8")
    with pytest.raises(PR.LedgerError, match="tự xoá"):
        PR.ProcessLock(p).acquire()


def test_take_without_the_lock_is_refused(tmp_path):
    lg = new_ledger(tmp_path)
    lg.lock.release()
    with pytest.raises(PR.LedgerError, match="khoá"):
        lg.take(1, "getSignaturesForAddress", "x")


# ------------------------------------------------------------------------------------------------ late transactions
def test_attempt_classification():
    assert PR.classify(None, "TimeoutError: timed out", None) == "TIMEOUT"
    assert PR.classify(None, "URLError: refused", None) == "NETWORK_ERROR"
    assert PR.classify(429, None, None) == "HTTP_429"
    assert PR.classify(200, None, None) == "INVALID_BODY"
    assert PR.classify(200, None, {"error": {"code": -32602}}) == "RPC_ERROR"
    assert PR.classify(200, None, {"result": None}) == "NULL"
    assert PR.classify(200, None, {"result": {"slot": 1}}) == "FOUND"


def test_late_verdicts_never_turn_null_into_not_existing():
    v = PR.late_verdict
    assert v([{"class": "FOUND", "t": 0.2}])["verdict"] == "SERVED_FIRST_ANSWER"
    assert v([{"class": "TIMEOUT", "t": 0.1}, {"class": "FOUND", "t": 1.3}])["verdict"] == "SERVED_FIRST_ANSWER"
    late = v([{"class": "NULL", "t": 0.1}, {"class": "NULL", "t": 1.2}, {"class": "FOUND", "t": 3.4}])
    assert late["verdict"] == "SERVED_AFTER_NULL" and (late["null_until_s"], late["found_at_s"]) == (1.2, 3.4)
    nf = v([{"class": "NULL", "t": 0.1}, {"class": "HTTP_429", "t": 1.2}, {"class": "NULL", "t": 7.5}])
    assert nf["verdict"] == "NOT_SERVED_IN_WINDOW" and "KHÔNG kết luận" in nf["note"]
    assert v([{"class": "TIMEOUT", "t": 0.1}, {"class": "HTTP_503", "t": 2}])["verdict"] == "UNDETERMINED"


def test_phase5_classifies_each_try_without_hidden_retries(tmp_path):
    lg = new_ledger(tmp_path)
    answers = iter([[{"signature": "new1", "slot": 9, "err": None, "blockTime": 1}], None, None, {"slot": 9},
                    [{"signature": "new2", "slot": 10, "err": None, "blockTime": 1}], None, None, None, None])

    def opener(req):
        return Resp({"jsonrpc": "2.0", "result": next(answers), "id": 1})
    r = PR.Runner(transport(lg, opener, tmp_path / "req.jsonl"), 5, tmp_path)
    r.phase5()
    assert r.raw["C1"]["verdict"] == "SERVED_AFTER_NULL" and [a["class"] for a in r.raw["C1"]["attempts"]] == ["NULL", "NULL", "FOUND"]
    assert r.raw["C2"]["verdict"] == "NOT_SERVED_IN_WINDOW"
    assert lg.used(5) == 9 <= PR.PHASE_CAPS[5]                                   # 2 listings + 3 + 4 tries
    assert all(c["result"] != "PASS" for c in r.checks if c["check"].startswith("C"))   # observations, not proofs


# ------------------------------------------------------------------------------------------------ pagination / null
def test_phase1_checks_pass_on_a_correct_chain_and_catch_an_inclusive_until(tmp_path):
    lg = new_ledger(tmp_path / "a")
    r = PR.Runner(transport(lg, Opener(Chain({PR.MINT: 1300})), tmp_path / "a.jsonl"), 1, tmp_path)
    r.phase1()
    assert r.checks and all(c["result"] == "PASS" for c in r.checks), r.checks
    assert lg.used(1) == 6

    class Inclusive(Chain):                                                   # a node returning the checkpoint
        def __call__(self, m, p):
            out = super().__call__(m, p)
            u = p[1].get("until")
            return out + [s for s in self.by[p[0]] if s["signature"] == u] if u else out
    lg2 = new_ledger(tmp_path / "b")
    r2 = PR.Runner(transport(lg2, Opener(Inclusive({PR.MINT: 1300})), tmp_path / "b.jsonl"), 1, tmp_path)
    r2.phase1()
    assert any(c["result"] == "FAIL" and "until" in c["check"] for c in r2.checks)


def test_phase3_4_null_and_bad_signature_are_told_apart(tmp_path):
    lg = new_ledger(tmp_path)
    calls = []

    def opener(req):
        b = json.loads(req.data)
        calls.append(b["params"][0])
        if b["params"][0] == "không-phải-chữ-ký":
            return Resp({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid param"}, "id": 1})
        return Resp({"jsonrpc": "2.0", "result": None, "id": 1})
    r = PR.Runner(transport(lg, opener, tmp_path / "req.jsonl"), 3, tmp_path)
    r.phase3([{"signature": f"s{i}", "blockTime": 1, "slot": 1} for i in range(5)])
    r4 = PR.Runner(transport(lg, opener, tmp_path / "req.jsonl"), 4, tmp_path)
    r4.phase4()
    res = {c["check"][:2]: c["result"] for c in r.checks + r4.checks if c["check"].startswith("D")}
    assert res == {"D1": "PASS", "D2": "PASS"} and len(calls) == 7


# ------------------------------------------------------------------------------------------------ credits
def _log(tmp_path, rows):
    p = tmp_path / "req.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_credit_report_per_method_only_when_isolated_and_single_method(tmp_path):
    log = _log(tmp_path, [{"phase": 1, "method": "getSignaturesForAddress", "outcome": "FOUND", "bytes": 10}] * 6 +
                         [{"phase": 5, "method": "getSignaturesForAddress", "outcome": "FOUND", "bytes": 5}] * 2 +
                         [{"phase": 5, "method": "getTransaction", "outcome": "NULL", "bytes": 5}] * 8 +
                         [{"phase": 4, "method": "getTransaction", "outcome": "RPC_ERROR", "bytes": 5}])
    rep = PR.credit_report(log, {"baseline": [1000, 1000, 1000],
                                 "phases": {"1": {"before": 1000, "after": [1004, 1006, 1006]},
                                            "4": {"before": 1006, "after": [1006, 1006]},
                                            "5": {"before": 1006, "after": [1016, 1016]},
                                            "6": {"before": 1016, "after": [1020]}}})
    p = rep["phases"]
    assert rep["isolated"] and p["1"]["result"] == "ĐƠN GIÁ THEO METHOD" and p["1"]["credits_per_attempt"] == 1.0
    assert p["4"]["delta_total"] == 0                                         # the error request was not billed
    assert p["5"]["result"].startswith("CHỈ TỔNG DELTA") and "credits_per_attempt" not in p["5"]
    assert p["6"]["result"].startswith("CHƯA ỔN ĐỊNH")
    assert p["1"]["attempts"] == {"getSignaturesForAddress": 6} and p["1"]["bytes"] == {"getSignaturesForAddress": 60}


def test_credit_report_without_a_flat_baseline_gives_no_unit_price(tmp_path):
    log = _log(tmp_path, [{"phase": 1, "method": "getSignaturesForAddress", "outcome": "FOUND", "bytes": 1}] * 6)
    rep = PR.credit_report(log, {"baseline": [1000, 1012], "phases": {"1": {"before": 1012, "after": [1030, 1030]}}})
    assert not rep["isolated"] and rep["phases"]["1"]["result"].startswith("KHÔNG CÔ LẬP")
    assert "credits_per_attempt" not in rep["phases"]["1"]


# ------------------------------------------------------------------------------------------------ isolation / safety
def test_dry_run_sends_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HELIUS_API_KEY", "test-not-a-key")
    assert PR.main(["--out", str(tmp_path / "o")]) == 0
    assert not (tmp_path / "o").exists() and not PR.ANCHOR.exists()
    assert "36 lần đầu" in capsys.readouterr().out


def test_missing_approval_or_key_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test-not-a-key")
    out = str(tmp_path / "o")
    assert PR.main(["--init", "--out", out]) == 0
    assert PR.main(["--run", "--phase", "1", "--out", out]) == 2
    monkeypatch.delenv("HELIUS_API_KEY")
    assert PR.main(["--run", "--phase", "1", "--approval", "ok", "--out", out]) == 2
    assert not (tmp_path / "o" / "requests.jsonl").exists()


def test_the_probe_process_loads_no_stream_worker_or_websocket():
    code = ("import sys, threading, insiders.probe as p; p.Runner.phase3; p.Runner.phase6;"
            "from insiders.track import parse_mint_events; from insiders.wallets import pair_pnl;"
            "bad=[m for m in ('insiders.vip','insiders.service','insiders.site','websockets') if m in sys.modules];"
            "print(bad, threading.active_count())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    assert out.stdout.strip() == "[] 1", out.stdout + out.stderr


def test_the_log_never_holds_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "SECRETKEY123")
    lg = new_ledger(tmp_path)

    def opener(req):
        raise urllib.error.URLError("cannot reach https://mainnet.helius-rpc.com/?api-key=SECRETKEY123")
    PR.Transport("https://mainnet.helius-rpc.com/?api-key=SECRETKEY123", lg, tmp_path / "r.jsonl",
                 opener=opener, sleep=lambda s: None).post("getHealth", [], 1, "x")
    for f in (tmp_path / "r.jsonl", lg.path, lg.anchor):
        assert "SECRETKEY123" not in f.read_text(encoding="utf-8")
