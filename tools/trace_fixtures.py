"""Record the on-chain test data of the wallet trace (development only, public RPC by default, read-only calls).

Writes tests/fixtures/trace_rpc.json.gz: every RPC answer used, keyed by (method, params), so tests/test_insiders_trace.py
replays the real traces offline. Sets:
  evidence   the transactions the Early Smart Money research cites as a direct transfer / fee payment between wallets
             (esm_link.sig): the trace decoder must find the same transfer in each;
  cases      E007 (all 5 members), 9Y2Es2V6…, HHLzvoz1…, the CHUCHU creator (DEV / CREATOR_GROUP E001): 2 hops, both ways;
  tier_a     the RADAR Smart Money list at score >= 5 (26 wallets) + the 27 CREATOR_GROUP wallets: 1 hop.

usage: python tools/trace_fixtures.py [--sigs-case 10] [--sigs-tier 8]"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from insiders import esm, esm_view  # noqa: E402
from insiders.trace import Tracer  # noqa: E402
from insiders.track import Budget, Store  # noqa: E402
from insiders.wallets import WalletHistory  # noqa: E402
from insiders.watch import Rpc  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "trace_rpc.json.gz"
CASE_ROOTS = {"9Y2": "9Y2Es2V6QN4H637kTNd1LVZTthkRsint3WubyXEJzV7E", "HHLz": "HHLzvoz1A95eKAQu3qs5i6qLC7VEm8R9qhQmqpRDFSjZ",
              "E007": "A9Dam8J4vcmPmzvnHdwJAwoBhim73WyuQe3hF4u6AJ4w", "DEV_CHUCHU": "5uGbn3xfFEgmy3qhLmXPUGeBQee8hwTuAnHCktTfFRXC"}


def key(method: str, params: list) -> str:
    return json.dumps([method, params], sort_keys=True, separators=(",", ":"))


class Recorder:
    def __init__(self, rpc, book: dict):
        self.rpc, self.book = rpc, book

    def __call__(self, method, params):
        k = key(method, params)
        if k not in self.book:
            self.book[k] = self.rpc(method, params)
        return self.book[k]


def seeded_store(path: str) -> Store:
    st = Store(path)
    esm.import_research(st, ROOT / "insiders" / "esm_seed")
    WalletHistory(st, None, enabled=False).seed(json.load(open(ROOT / "insiders" / "result.json")))
    return st


def sets(st: Store) -> dict:
    radar = esm_view.radar(st, False, None, 5)
    creator_group = sorted({r["wallet"] for r in st.q("SELECT wallet FROM esm_flag WHERE flag='CREATOR_GROUP'")})
    ev = st.q("SELECT a, b, signal, sig FROM esm_link WHERE signal IN ('direct_transfer','paid_by') AND sig IS NOT NULL")
    return {"tier_a_radar5": [s["wallet"] for s in radar["smart"]], "creator_group": creator_group,
            "evidence": sorted({(r["sig"], r["a"], r["b"], r["signal"]) for r in ev}), "cases": CASE_ROOTS}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sigs-case", type=int, default=10)
    ap.add_argument("--sigs-tier", type=int, default=8)
    a = ap.parse_args()
    book: dict = json.load(gzip.open(OUT, "rt")) if OUT.exists() else {}
    rec = Recorder(Rpc(url=os.environ.get("INSIDER_TRACE_RPC")) if os.environ.get("INSIDER_TRACE_RPC") else Rpc(gap_s=0.35), book)
    st = seeded_store(os.path.join(tempfile.mkdtemp(), "t.db"))
    S = sets(st)
    meta = {"recorded_at": time.time(), "sets": S, "params": {"case": [2, "both", 0.05, a.sigs_case], "tier": [1, "both", 0.05, a.sigs_tier]}}
    for sig, *_ in S["evidence"]:
        rec("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}])
    print("evidence", len(S["evidence"]), "rpc answers", len(book), flush=True)
    tr = Tracer(st, rec, budget=Budget(10_000), day_cap=10**9)
    roots = [(n, w, 2, a.sigs_case) for n, w in CASE_ROOTS.items()] + \
            [("tier", w, 1, a.sigs_tier) for w in S["tier_a_radar5"] + S["creator_group"] if w not in CASE_ROOTS.values()]
    jobs = []
    for i, (name, w, hops, sigs) in enumerate(roots):
        job = tr.start(w, hops, "both", 0.05, sigs, call_cap=2_000)
        while tr.job_row(job["id"])["status"] == "running":
            tr.step()
        j = tr.job_row(job["id"])
        jobs.append({"name": name, "root": w, "hops": hops, "sigs": sigs, "status": j["status"], "calls": j["calls"]})
        print(i + 1, "/", len(roots), name, w[:6], j["status"], j["calls"], "answers", len(book), flush=True)
        if i % 5 == 0:
            _save(book, meta, jobs)
    _save(book, meta, jobs)
    return 0


def _save(book, meta, jobs):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(book, f, separators=(",", ":"))
    tmp.replace(OUT)
    (OUT.parent / "trace_sets.json").write_text(json.dumps({**meta, "jobs": jobs}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
