"""Wallet trace (insiders/trace.py) on REAL on-chain data replayed offline.

tests/fixtures/trace_rpc.json.gz holds every RPC answer (getSignaturesForAddress / getTransaction) recorded by
tools/trace_fixtures.py on the public Solana RPC; tests/fixtures/trace_sets.json names the sets: the transactions
the Early Smart Money research cites as evidence, the cases E007 / 9Y2Es2V6… / HHLzvoz1… / CHUCHU creator, and the
Tier A list (RADAR score >= 5 + the 27 CREATOR_GROUP wallets). A replayed call that was not recorded fails the test:
nothing is guessed. Every flow the tracer reports is re-checked against the raw transaction (exact lamports of a
system transfer, or the owners' token balance deltas)."""
import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from insiders import esm  # noqa: E402
from insiders import trace as TR  # noqa: E402
from insiders.track import Budget, Store  # noqa: E402
from insiders.wallets import WalletHistory  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
BOOK = json.load(gzip.open(FIX / "trace_rpc.json.gz", "rt")) if (FIX / "trace_rpc.json.gz").exists() else None
SETS = json.loads((FIX / "trace_sets.json").read_text()) if (FIX / "trace_sets.json").exists() else None
need = pytest.mark.skipif(BOOK is None or SETS is None, reason="fixtures not recorded (python tools/trace_fixtures.py)")
TXP = {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}


def key(method, params):
    return json.dumps([method, params], sort_keys=True, separators=(",", ":"))


class Replay:
    def __init__(self):
        self.calls = 0

    def __call__(self, method, params):
        self.calls += 1
        k = key(method, params)
        if k not in BOOK:
            raise AssertionError(f"RPC call not recorded: {k[:160]}")
        return BOOK[k]


def tx_of(sig):
    return BOOK[key("getTransaction", [sig, TXP])]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    p = tmp_path_factory.mktemp("trace") / "t.db"
    st = Store(p)
    esm.import_research(st, ROOT / "insiders" / "esm_seed")
    WalletHistory(st, None, enabled=False).seed(json.load(open(ROOT / "insiders" / "result.json")))
    return st


def verify_flow(f) -> bool:
    """Independent check of one reported flow against the raw transaction."""
    tx = tx_of(f["sig"])
    meta = tx["meta"]
    if meta.get("err") is not None:
        return False
    if f["asset"] == "FEE":                                  # payer = first account, the other wallet a signer, same fee
        ks = tx["transaction"]["message"]["accountKeys"]
        signers = {k["pubkey"] for k in ks if k.get("signer")}
        owners = {}
        for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
            for b in rows or []:
                k = (b.get("owner"), b.get("mint"))
                owners[k] = owners.get(k, 0) + sign * int(b["uiTokenAmount"]["amount"])
        touched = signers | {o for (o, _), d in owners.items() if d}
        return ks[0]["pubkey"] == f["src"] and f["dst"] in touched and round(f["amount"] * 1e9) == int(meta["fee"])
    if f["asset"] == "SOL":
        lam = round(f["amount"] * 1e9)
        ins = list(tx["transaction"]["message"]["instructions"])
        for inner in meta.get("innerInstructions") or []:
            ins += inner["instructions"]
        for i in ins:
            p = i.get("parsed")
            if isinstance(p, dict) and p.get("type") in ("transfer", "createAccount", "transferWithSeed"):
                info = p["info"]
                if info.get("source") == f["src"] and (info.get("destination") or info.get("newAccount")) == f["dst"] \
                        and int(info.get("lamports") or 0) == lam:
                    return True
        return False
    d = {}
    for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
        for b in rows or []:
            if b.get("mint") == f["asset"]:
                k = b.get("owner")
                d[k] = d.get(k, 0) + sign * int(b["uiTokenAmount"]["amount"])
                dec = int(b["uiTokenAmount"]["decimals"])
    raw = f["amount"] * 10 ** dec
    return d.get(f["src"], 0) < 0 and d.get(f["dst"], 0) > 0 and raw <= -d[f["src"]] + 1 and raw <= d[f["dst"]] + 1


def run(st, root, hops, sigs):
    rp = Replay()
    tr = TR.Tracer(st, rp, budget=Budget(10_000), day_cap=10 ** 9)
    job = tr.start(root, hops, "both", 0.05, sigs, call_cap=2_000)
    for _ in range(5000):
        if tr.job_row(job["id"])["status"] != "running":
            break
        tr.step()
    return tr, tr.view(job["id"]), rp


# ---------------------------------------------------------------------------------------------------- decoder (synthetic)
def _tx(ins=(), inner=(), pre=(), post=(), err=None, keys=("W",)):
    return {"slot": 1, "blockTime": 100, "transaction": {"signatures": ["S"], "message": {"accountKeys": [{"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)],
            "instructions": list(ins)}}, "meta": {"err": err, "fee": 5000, "preBalances": [0] * len(keys), "postBalances": [0] * len(keys),
            "innerInstructions": [{"index": 0, "instructions": list(inner)}] if inner else [], "preTokenBalances": list(pre), "postTokenBalances": list(post)}}


def _sys(src, dst, lam, kind="transfer"):
    return {"program": "system", "parsed": {"type": kind, "info": {"source": src, "destination": dst, "lamports": lam}}}


def _tb(owner, mint, amount, dec=6, idx=0):
    return {"accountIndex": idx, "owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount), "decimals": dec}}


A = "3kxcF8wHKm4sEtnxjXXeUvNGeDjTmo47LgtSRg71YfG5"            # on-curve wallets (real public keys)
B = "HHLzvoz1A95eKAQu3qs5i6qLC7VEm8R9qhQmqpRDFSjZ"
C = "9Y2Es2V6QN4H637kTNd1LVZTthkRsint3WubyXEJzV7E"
MINT = "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump"
POOL = "So11111111111111111111111111111111111111112"          # stands for an off-curve program account below


def test_flows_sol_both_ways_and_inner():
    tx = _tx(ins=[_sys(A, B, 2_500_000_000)], inner=[_sys(C, A, 100_000_000)])
    f = TR.flows(tx, A)
    assert {(x["src"], x["dst"], x["asset"], x["amount"], x["dir"]) for x in f} == {(A, B, "SOL", 2.5, "out"), (C, A, "SOL", 0.1, "in")}


def test_flows_fee_paid_for_another_signer():
    tx = _tx(keys=(A, B))
    tx["transaction"]["message"]["accountKeys"][1]["signer"] = True
    assert TR.flows(tx, B) == [{"src": A, "dst": B, "asset": "FEE", "amount": 5e-06, "dir": "in"}]
    assert TR.flows(tx, A) == [{"src": A, "dst": B, "asset": "FEE", "amount": 5e-06, "dir": "out"}]


def test_flows_failed_tx_moves_nothing():
    assert TR.flows(_tx(ins=[_sys(A, B, 10**9)], err={"x": 1}), A) == []


def test_flows_token_transfer_between_wallets():
    tx = _tx(pre=[_tb(A, MINT, 5_000_000), _tb(B, MINT, 0, idx=1)], post=[_tb(A, MINT, 1_000_000), _tb(B, MINT, 4_000_000, idx=1)])
    assert TR.flows(tx, A) == [{"src": A, "dst": B, "asset": MINT, "amount": 4.0, "dir": "out"}]
    assert TR.flows(tx, B) == [{"src": A, "dst": B, "asset": MINT, "amount": 4.0, "dir": "in"}]


def test_flows_swap_against_pool_is_not_a_transfer():
    off = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"      # a PDA-like key off the curve
    from insiders.scan import on_curve
    if on_curve(off):
        pytest.skip("key happens to be on curve")
    tx = _tx(pre=[_tb(A, MINT, 0), _tb(off, MINT, 9_000_000, idx=1)], post=[_tb(A, MINT, 3_000_000), _tb(off, MINT, 6_000_000, idx=1)])
    assert TR.flows(tx, A) == []


def test_start_validation(tmp_path):
    tr = TR.Tracer(Store(tmp_path / "t.db"), lambda m, p: None)
    with pytest.raises(ValueError):
        tr.start("not-a-wallet")
    with pytest.raises(ValueError):
        tr.start(A, direction="sideways")
    j = tr.start(A, hops=9, sigs=1000)
    assert j["hops"] == TR.MAX_HOPS and j["sigs"] == TR.MAX_SIGS and j["status"] == "running"


def test_rpc_error_keeps_wallet_then_gives_up(tmp_path):
    def bad(m, p):
        raise RuntimeError("HTTP 503")
    tr = TR.Tracer(Store(tmp_path / "t.db"), bad, budget=Budget(1000))
    j = tr.start(A, 1)
    for _ in range(3):
        tr.step()
    n = tr.s.q("SELECT * FROM trace_node WHERE job=?", (j["id"],))[0]
    assert n["state"] == "error" and n["tries"] == 3 and "503" in n["reason"]


def test_job_call_cap_stops_safely(tmp_path):
    sigs = [{"signature": f"s{i}", "err": None, "blockTime": 1000 + i} for i in range(20)]
    tr = TR.Tracer(Store(tmp_path / "t.db"), lambda m, p: sigs if m == "getSignaturesForAddress" else None, budget=Budget(1000))
    j = tr.start(A, 1, sigs=20, call_cap=5)
    tr.step()
    r = tr.job_row(j["id"])
    assert r["status"] == "capped" and r["calls"] == 5


# ---------------------------------------------------------------------------------------------------- real data
@need
def test_esm_direct_transfer_evidence_is_found():
    """Every 'direct_transfer' the Early Smart Money research recorded (esm_link.sig) is found by the trace decoder
    in the same transaction, between the same two wallets."""
    ev = [e for e in SETS["sets"]["evidence"] if e[3] == "direct_transfer"]
    assert ev, "no direct_transfer evidence in the set"
    missing = []
    for sig, a, b, _ in ev:
        tx = tx_of(sig)
        pairs = {(f["src"], f["dst"]) for w in (a, b) for f in TR.flows(tx, w)}
        if (a, b) not in pairs and (b, a) not in pairs:
            missing.append(sig)
    assert not missing, f"{len(missing)}/{len(ev)} evidence transfers not found: {missing[:3]}"


@need
def test_e007_paid_by_evidence_on_chain(seeded):
    """E007: the 5 members are joined by 'paid_by' (one wallet pays the other's fee). On chain the fee payer of each
    cited transaction is one of the pair, and the trace annotates every member with entity E007."""
    ms = [r["wallet"] for r in seeded.q("SELECT wallet FROM esm_wallet_entity WHERE entity='E007'")]
    assert len(ms) == 5
    links = seeded.q("SELECT a, b, sig FROM esm_link WHERE signal='paid_by' AND (a IN ({0}) OR b IN ({0}))".format(",".join("?" * 5)), (*ms, *ms))
    assert links
    for lk in links:
        keys = tx_of(lk["sig"])["transaction"]["message"]["accountKeys"]
        payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
        assert payer in (lk["a"], lk["b"])
        tx = tx_of(lk["sig"])                                # the trace decoder finds the same relation (asset FEE)
        rel = {(f["src"], f["dst"]) for w in (lk["a"], lk["b"]) for f in TR.flows(tx, w) if f["asset"] == "FEE"}
        assert (lk["a"], lk["b"]) in rel or (lk["b"], lk["a"]) in rel, lk["sig"]
    ann = TR.Tracer(seeded, None).annotate(ms)
    assert all(ann[w].get("entity") == "E007" and "E007" in ann[w]["tags"] for w in ms)


def _case_jobs(name):
    return [j for j in SETS["jobs"] if j["name"] == name]


@need
@pytest.mark.parametrize("name", ["E007", "9Y2", "HHLz", "DEV_CHUCHU"])
def test_case_trace_replays_and_every_flow_is_on_chain(seeded, name):
    jb = _case_jobs(name)[0]
    tr, v, rp = run(seeded, jb["root"], jb["hops"], jb["sigs"])
    assert v["job"]["status"] == jb["status"]
    assert v["nodes"][0]["wallet"] == jb["root"] and v["nodes"][0]["state"] == "done"
    assert v["flows"], "a real trace with no flow at all"
    bad = [f for f in v["flows"] if not verify_flow(f)]
    assert not bad, f"{len(bad)} flows do not match the transaction: {bad[:2]}"
    for m in v["matches"]:                                  # each step of a path to a known wallet has its signatures
        for st in m["path"]:
            assert st["sigs"] and all(key("getTransaction", [s, TXP]) in BOOK for s in st["sigs"])


@need
def test_case_annotations_match_existing_data(seeded):
    """Cross-check with what the system already holds: 9Y2 / HHLz carry their ESM class, the CHUCHU creator is
    CREATOR + CREATOR_GROUP (E001); STANK cohort wallets are tagged STANK."""
    tr = TR.Tracer(seeded, None)
    a = tr.annotate([SETS["sets"]["cases"]["9Y2"], SETS["sets"]["cases"]["HHLz"], SETS["sets"]["cases"]["DEV_CHUCHU"]])
    assert a[SETS["sets"]["cases"]["9Y2"]]["esm_class"] == "INSUFFICIENT_DATA"
    assert a[SETS["sets"]["cases"]["HHLz"]]["esm_class"] == "INDEPENDENT"
    dev = a[SETS["sets"]["cases"]["DEV_CHUCHU"]]
    assert {"CREATOR", "CREATOR_GROUP", "E001"} <= set(dev["tags"]) and "CHUCHU" in dev["creator_of"]
    stank = [r["wallet"] for r in seeded.q("SELECT wallet FROM wallets WHERE cohort='STANK' LIMIT 5")]
    assert all("STANK" in x["tags"] for x in tr.annotate(stank).values())


@need
def test_tier_a_traces_replay_and_verify(seeded):
    """Tier A (RADAR score >= 5 and the 27 CREATOR_GROUP wallets): every recorded 1-hop trace replays with the same
    number of calls and every flow it reports is re-checked against its transaction."""
    jobs = [j for j in SETS["jobs"] if j["name"] == "tier"]
    assert len(jobs) >= 40
    total = 0
    for jb in jobs:
        tr, v, rp = run(seeded, jb["root"], jb["hops"], jb["sigs"])
        assert v["job"]["status"] == jb["status"] and v["job"]["calls"] == jb["calls"]
        bad = [f for f in v["flows"] if not verify_flow(f)]
        assert not bad, (jb["root"], bad[:2])
        total += len(v["flows"])
    assert total > 0


# ---------------------------------------------------------------------------------------------------- API
def test_trace_api(tmp_path):
    from insiders import site
    assert site.trace_get(None, "/api/insiders/trace/status", "/x")[0] == 503
    sigs = [{"signature": "S", "err": None, "blockTime": 100}]
    tx = _tx(ins=[_sys(A, B, 3 * 10**9)], keys=(A, B))
    tr = TR.Tracer(Store(tmp_path / "t.db"), lambda m, p: sigs if m == "getSignaturesForAddress" else tx, budget=Budget(1000))
    assert site.trace_post(tr, "/api/insiders/trace/start", "/x", b'{"wallet": "bad"}')[0] == 400
    assert site.trace_post(tr, "/api/insiders/trace/start", "/x", b"[1]")[0] == 400
    code, r = site.trace_post(tr, "/api/insiders/trace/start", "/x", json.dumps({"wallet": A, "hops": 1, "min_sol": 1}).encode())
    assert code == 200
    jid = r["job"]["id"]
    tr.step()
    tr.step()
    code, v = site.trace_get(tr, "/api/insiders/trace/job", f"/x?id={jid}")
    assert code == 200 and v["job"]["status"] == "done"
    assert [(f["src"], f["dst"], f["amount"], f["sig"]) for f in v["flows"]] == [(A, B, 3.0, "S")]
    assert {n["wallet"]: n["state"] for n in v["nodes"]} == {A: "done", B: "leaf"}
    assert site.trace_get(tr, "/api/insiders/trace/job", "/x?id=x")[0] == 400
    assert site.trace_get(tr, "/api/insiders/trace/job", "/x?id=99")[0] == 404
    assert site.trace_get(tr, "/api/insiders/trace/jobs", f"/x?wallet={A}")[1]["jobs"][0]["root"] == A
    assert tr.s.q("SELECT kind FROM edges")[0]["kind"] == "trace"            # the existing edges table, no new one
