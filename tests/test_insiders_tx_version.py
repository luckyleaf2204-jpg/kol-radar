"""Regression: every getTransaction of the stream, the coin tracker and the wallet worker asks for transaction
version 1. With 0 the node refuses version-1 transactions (-32015, seen on mainnet 2026-11-25) and each one became a
gap. Fake RPCs only."""
import re
import socket
import urllib.request
from pathlib import Path

import pytest

from insiders import track as T
from insiders import wallets as WH

ROOT = Path(T.__file__).parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_no_module_asks_for_version_0():
    for f in ("track.py", "wallets.py", "vip.py", "watch.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r'"maxSupportedTransactionVersion":\s*0\b', src), f


class V1Node:
    """Serves only callers that accept version 1, like mainnet for a v1 transaction."""

    def __init__(self):
        self.asked = []

    def __call__(self, method, params):
        if method == "getTransaction":
            v = (params[1] or {}).get("maxSupportedTransactionVersion")
            self.asked.append(v)
            if v is None or v < 1:
                raise RuntimeError("{'code': -32015, 'message': 'Transaction version (1) is not supported'}")
            return {"blockTime": 1, "slot": 1, "version": 1, "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [],
                                                                       "preBalances": [0], "postBalances": [0], "fee": 0},
                    "transaction": {"signatures": [params[0]], "message": {"accountKeys": [], "instructions": []}}}
        return []


def test_tracker_reads_a_version_1_transaction(tmp_path):
    node = V1Node()
    t = T.Tracker(T.Store(tmp_path / "t.db"), node, budget=T.Budget(1000))
    assert t._tx("sig1")["version"] == 1 and node.asked == [1]


def test_wallet_worker_reads_a_version_1_transaction_without_a_gap(tmp_path):
    node = V1Node()
    st = T.Store(tmp_path / "t.db")
    h = WH.WalletHistory(st, node, enabled=True, budget=T.Budget(1000))
    w = "7GSMwnWB5YWmCwQax4bzfeDMvSZEogp6TAehxrJddDyr"
    st.x("INSERT INTO wallets (wallet, cohort, priority) VALUES (?, 'DEPTH', 0)", (w,))
    st.x("INSERT INTO wallet_queue (wallet, priority, state, coverage) VALUES (?, 0, 'parsing', 'full')", (w,))
    st.x("INSERT INTO wallet_sigs (wallet, sig, block_time, err) VALUES (?, 's1', 1, 0)", (w,))
    h.step_parse()
    assert node.asked == [1]
    assert st.q("SELECT parsed FROM wallet_sigs")[0]["parsed"] == 1 and st.gaps(scope="wallet-tx") == []
