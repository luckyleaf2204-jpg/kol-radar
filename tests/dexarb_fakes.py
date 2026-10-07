"""Deterministic fakes: an EVM chain with constant-product venues behind a JSON-RPC transport (batch aware) and a
Jupiter quote function. No network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dexarb.fees import SEL_APPROVE, SEL_FACTORY, SEL_GET_PAIR, SEL_GET_POOL  # noqa: E402
from dexarb.protocols import SEL_GAO, SEL_Q1  # noqa: E402
from dexarb.registry import CHAINS  # noqa: E402
from dexarb.simulate import (EXEC_ROUTERS, SEL_ALW, SEL_BAL, SEL_V2_SWAP, SEL_V3_SWAP, allowance_key,  # noqa: E402
                             balance_key)
from dexarb.fees import PAPER_EVM_ADDRESS  # noqa: E402

BASE = CHAINS["base"]
USDC, WETH, CBBTC = BASE.quote_asset, BASE.tokens[0], BASE.tokens[1]
VENUES = {p.key: p.address for p in BASE.protocols}           # uniswap_v2, sushiswap_v2, uniswap_v3, pancakeswap_v3
FACTORY = {a: "0x" + f"{i + 1:040x}" for i, a in enumerate(VENUES.values())}
POOL = {a: "0x" + f"{i + 0xa0:040x}" for i, a in enumerate(VENUES.values())}


class FakeEvm:
    """reserves[venue_address][token_address] = (quote reserve raw, token reserve raw); fee 0.3 %."""

    def __init__(self):
        self.head = 1000
        self.fail = 0
        self.calls: list[str] = []
        self.reserves = {}
        for a in VENUES.values():
            self.reserves[a] = {WETH.address: (3_000_000 * 10 ** 6, 1000 * 10 ** 18),
                                CBBTC.address: (6_000_000 * 10 ** 6, 100 * 10 ** 8)}
        self.base_fee = 10 ** 9
        self.tip = 10 ** 8
        self.gas_used = 150_000
        self.sim_gas = 121_000
        self.layout_known = True
        self.exec_routers = {r: p for (c, p), r in EXEC_ROUTERS.items() if c == "base"}

    def swap_call(self, p):
        """Unsigned swap with state override: needs the override; reverts below min-out."""
        tx, ov = p[0], (p[2] if len(p) > 2 else {})
        data, to = tx["data"], tx["to"]
        w = [int(data[10 + 64 * i:74 + 64 * i], 16) for i in range((len(data) - 10) // 64)]
        if data.startswith(SEL_V2_SWAP):
            amt, mino, a_in, a_out, venue = w[0], w[1], f"0x{w[6]:040x}", f"0x{w[7]:040x}", to
        else:
            a_in, a_out, amt, mino = f"0x{w[0]:040x}", f"0x{w[1]:040x}", w[4], w[5]
            pk = self.exec_routers.get(to)
            venue = VENUES.get(pk) if pk else None
            if venue is None:
                return None, "execution reverted"
        if not ov.get(a_in, {}).get("stateDiff"):
            return None, "execution reverted: insufficient balance"
        o = self.out(venue, a_in, a_out, amt)
        if o is None or o < mino:
            return None, "execution reverted: INSUFFICIENT_OUTPUT_AMOUNT"
        return o, None

    def price(self, venue, token, factor):
        q, t = self.reserves[venue][token]
        self.reserves[venue][token] = (int(q * factor), t)

    @staticmethod
    def cp(x, rx, ry):
        x = x * 997 // 1000
        return ry * x // (rx + x)

    def out(self, venue, a_in, a_out, amt):
        tok = a_out if a_in == USDC.address else a_in
        if tok not in self.reserves[venue]:
            return None
        q, t = self.reserves[venue][tok]
        return self.cp(amt, q, t) if a_in == USDC.address else self.cp(amt, t, q)

    def one(self, req):
        m, p = req["method"], req["params"]
        self.calls.append(m)
        res, err = None, None
        if m == "eth_blockNumber":
            res = hex(self.head)
        elif m == "eth_feeHistory":
            res = {"baseFeePerGas": [hex(self.base_fee)] * 6, "reward": [[hex(self.tip)]] * 5}
        elif m == "eth_gasPrice":
            res = hex(self.base_fee + self.tip)
        elif m == "eth_estimateGas":
            if p[0]["data"].startswith(SEL_APPROVE):
                res = hex(46_000)
            else:
                o, e = self.swap_call(p)
                res, err = (hex(self.sim_gas), None) if o is not None else (None, e)
        elif m == "eth_getLogs":
            res = [{"transactionHash": f"0x{i:064x}"} for i in range(5)]
        elif m == "eth_getTransactionReceipt":
            res = {"gasUsed": hex(self.gas_used), "l1Fee": hex(0)}
        elif m == "eth_call" and (p[0]["data"].startswith(SEL_V2_SWAP) or p[0]["data"].startswith(SEL_V3_SWAP)):
            o, err = self.swap_call(p)
            if o is not None:
                res = ("0x" + f"{32:064x}{2:064x}{0:064x}{o:064x}") if p[0]["data"].startswith(SEL_V2_SWAP) \
                    else "0x" + f"{o:064x}" + "0" * 192
        elif m == "eth_call" and (p[0]["data"].startswith(SEL_BAL) or p[0]["data"].startswith(SEL_ALW)):
            ov = (p[2] if len(p) > 2 else {}).get(p[0]["to"], {}).get("stateDiff", {})
            spender = "0x" + p[0]["data"][-40:] if p[0]["data"].startswith(SEL_ALW) else None
            key = balance_key(PAPER_EVM_ADDRESS, 9) if spender is None else allowance_key(PAPER_EVM_ADDRESS, spender, 10)
            res = ov.get(key, "0x" + "0" * 64) if self.layout_known else "0x" + "0" * 64
        elif m == "eth_call":
            to, data = p[0]["to"], p[0]["data"]
            w = [int(data[10 + 64 * i:74 + 64 * i], 16) for i in range((len(data) - 10) // 64)]
            if data == SEL_FACTORY:
                res = "0x" + FACTORY[to][2:].rjust(64, "0")
            elif data.startswith(SEL_GET_PAIR) or data.startswith(SEL_GET_POOL):
                venue = next(v for v, f in FACTORY.items() if f == to)
                res = "0x" + POOL[venue][2:].rjust(64, "0")
            elif data.startswith(SEL_GAO) and to in self.reserves:
                amt, a_in, a_out = w[0], f"0x{w[3]:040x}", f"0x{w[4]:040x}"
                o = self.out(to, a_in, a_out, amt)
                res = None if o is None else "0x" + f"{2:064x}" * 0 + f"{64:064x}{2:064x}{amt:064x}{o:064x}"
                err = None if o is not None else "execution reverted"
            elif data.startswith(SEL_Q1) and to in self.reserves:
                a_in, a_out, amt, fee = f"0x{w[0]:040x}", f"0x{w[1]:040x}", w[2], w[3]
                o = self.out(to, a_in, a_out, amt) if fee == 500 else None
                res = None if o is None else "0x" + f"{o:064x}" + "0" * 192
                err = None if o is not None else "execution reverted"
            else:
                err = "execution reverted"
        out = {"jsonrpc": "2.0", "id": req["id"]}
        out.update({"error": {"code": 3, "message": err}} if err or res is None else {"result": res})
        return out

    def transport(self, payload: bytes) -> bytes:
        req = json.loads(payload)
        if self.fail:
            self.fail -= 1
            raise OSError("connection refused")
        if isinstance(req, list):
            return json.dumps([self.one(r) for r in req]).encode()
        return json.dumps(self.one(req)).encode()


SOL = CHAINS["solana"]


class FakeJupiter:
    """out = amount x price[(dex, in_symbol, out_symbol)] (raw units already scaled); label = dex."""

    def __init__(self):
        self.calls: list[str] = []
        self.slot = 5000
        self.price = {}
        self.fail = set()
        self.wrong_label = set()
        for dex in ("Raydium", "Raydium CLMM", "Whirlpool", "Meteora DLMM"):
            self.price[(dex, "USDC", "SOL")] = 1 / 150 * 1e9 / 1e6       # raw SOL per raw USDC
            self.price[(dex, "SOL", "USDC")] = 150 * 1e6 / 1e9 * 0.998
            self.price[(dex, "USDC", "JUP")] = 2.0
            self.price[(dex, "JUP", "USDC")] = 0.499

    def __call__(self, url):
        from urllib.parse import parse_qs, urlsplit
        self.calls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        sym = {a.address: a.symbol for a in (SOL.quote_asset, *SOL.tokens)}
        dex = q["dexes"]
        key = (dex, sym[q["inputMint"]], sym[q["outputMint"]])
        if dex in self.fail or key not in self.price:
            raise OSError("HTTP Error 400: Bad Request")
        self.slot += 1
        label = "Some Aggregator" if dex in self.wrong_label else dex
        return {"outAmount": str(int(int(q["amount"]) * self.price[key])), "priceImpactPct": "0.0004",
                "contextSlot": self.slot, "platformFee": None,
                "routePlan": [{"percent": 100, "swapInfo": {"label": label, "ammKey": "AMM" + dex[:3]}}]}
