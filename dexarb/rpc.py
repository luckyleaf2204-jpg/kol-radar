"""Read-only JSON-RPC client. Only methods on the allow-list can be called; anything that could sign or broadcast
(eth_sendRawTransaction, eth_sendTransaction, eth_sign*, personal_*, sendTransaction, simulateTransaction, requestAirdrop, ...) raises
before any network I/O. No key, seed or credential is ever read, stored or logged; the URL comes from an env var
(DEXARB_RPC_<CHAIN>) or a public endpoint and is never written to the DB."""
from __future__ import annotations

import json
import time
import urllib.request

READ_ONLY = frozenset({
    "eth_estimateGas", "eth_feeHistory", "eth_gasPrice", "eth_maxPriorityFeePerGas",
    "getRecentPrioritizationFees", "getMinimumBalanceForRentExemption", "getLatestBlockhash", "getBalance",
    "getTokenAccountsByOwner", "eth_getBalance",
    "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getBlockByHash", "eth_getLogs",
    "eth_getBlockReceipts", "eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_call", "eth_getCode",
    "getSlot", "getAccountInfo", "getMultipleAccounts", "getBlockTime", "getTransaction", "getSignaturesForAddress",
})


class RpcError(Exception):
    pass


class ForbiddenMethod(RpcError):
    pass


class Rpc:
    def __init__(self, url: str, timeout: float = 20.0, transport=None):
        self.url, self.timeout = url, timeout
        self.transport = transport or self._http
        self.calls = 0
        self.timing: dict[str, list] = {}              # method -> [calls, seconds] (diagnostics)

    def _http(self, payload: bytes) -> bytes:
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json",
                                                                      "User-Agent": "kol-radar-dexarb/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()

    def call(self, method: str, params: list):
        if method not in READ_ONLY:
            raise ForbiddenMethod(f"{method} is not a read-only method; this client never signs or sends")
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}).encode()
        t0 = time.time()
        try:
            out = json.loads(self.transport(body))
        finally:
            tm = self.timing.setdefault(method, [0, 0.0])
            tm[0] += 1
            tm[1] += time.time() - t0
        if out.get("error"):
            raise RpcError(str(out["error"])[:300])
        return out.get("result")

    def batch(self, calls: list[tuple[str, list]], chunk: int = 50) -> list:
        """Several read calls in one HTTP round trip per `chunk` (JSON-RPC batch). Every method is checked against
        the allow-list BEFORE anything is sent. Returns one item per call: the result, or an RpcError instance."""
        for m, _ in calls:
            if m not in READ_ONLY:
                raise ForbiddenMethod(f"{m} is not a read-only method; this client never signs or sends")
        out: list = []
        for i in range(0, len(calls), chunk):
            part = calls[i:i + chunk]
            ids = list(range(self.calls + 1, self.calls + 1 + len(part)))
            self.calls += len(part)
            body = json.dumps([{"jsonrpc": "2.0", "id": k, "method": m, "params": p}
                               for k, (m, p) in zip(ids, part)]).encode()
            t0 = time.time()
            try:
                res = json.loads(self.transport(body))
            finally:
                tm = self.timing.setdefault("batch", [0, 0.0])
                tm[0] += 1
                tm[1] += time.time() - t0
            if isinstance(res, dict):                  # whole batch refused (rate limit, batch not supported)
                raise RpcError(str(res.get("error") or res)[:300])
            by_id = {r.get("id"): r for r in res}
            for k in ids:
                r = by_id.get(k)
                if r is None:
                    out.append(RpcError("missing in batch reply"))
                elif r.get("error"):
                    out.append(RpcError(str(r["error"])[:300]))
                else:
                    out.append(r.get("result"))
        return out


class Breaker:
    """Per-chain circuit breaker: after `fails` consecutive errors the chain is OPEN for `cool` seconds (doubling up to
    `max_cool`); while open nothing is fetched and the skipped range is recorded as a coverage gap."""

    def __init__(self, fails: int = 5, cool: float = 60.0, max_cool: float = 900.0, clock=time.time):
        self.limit, self.base, self.max_cool, self.clock = fails, cool, max_cool, clock
        self.errors, self.cool, self.open_until, self.trips, self.last_error = 0, cool, 0.0, 0, ""

    @property
    def state(self) -> str:
        return "OPEN" if self.clock() < self.open_until else ("HALF_OPEN" if self.errors >= self.limit else "CLOSED")

    def allow(self) -> bool:
        return self.clock() >= self.open_until

    def ok(self) -> None:
        self.errors, self.cool = 0, self.base

    def fail(self, err: str) -> None:
        self.errors += 1
        self.last_error = err[:200]
        if self.errors >= self.limit:
            self.open_until = self.clock() + self.cool
            self.cool = min(self.max_cool, self.cool * 2)
            self.trips += 1
