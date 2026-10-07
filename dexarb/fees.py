"""Network costs with provenance. Every value is a Cost(amount in native units, status, source):
  MEASURED   read from the chain now (gas price / base fee + priority from eth_feeHistory; Solana prioritization fees;
             rent-exempt minimum)
  SIMULATED  eth_estimateGas of the exact call (approve(router, max) from the paper address - approve needs no
             balance)
  ESTIMATED  derived, not exact: EVM swap gas = median gasUsed (+ L1 fee on OP-stack chains) of up to 8 recent
             transactions that swapped in the SAME pool (they may do extra work, so it is an estimate); a pool with no
             swap in the window uses the median of the other sampled pools of the same protocol on that chain; Solana compute
             units = 1,400,000 upper bound (Jupiter's simulation needs a funded wallet; none is used); Solana base fee =
             5,000 lamports per signature (protocol default)
  UNKNOWN    no source: the opportunity is not counted as executable.
A gas estimate never guarantees the future fee. Costs are converted to the chain's quote asset with a native price
QUOTED on a supported DEX (native -> quote asset, timestamped); a stale price (> PRICE_MAX_AGE_S) = UNKNOWN."""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from dexarb.keccak import selector, topic

SEL_FACTORY = selector("factory()")
SEL_GET_PAIR = selector("getPair(address,address)")
SEL_GET_POOL = selector("getPool(address,address,uint24)")
SEL_APPROVE = selector("approve(address,uint256)")
TOPIC_V2_SWAP = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
TOPIC_V3_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
TOPIC_PCS_V3_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")
PAPER_EVM_ADDRESS = "0x00000000000000000000000000000000000000a1"   # never holds keys; used only as `from` in reads
SOL_BASE_FEE_LAMPORTS = 5000
SOL_CU_UPPER = 1_400_000
GAS_SAMPLE_BLOCKS = 400
GAS_SAMPLE_TXS = 8
GAS_TTL_S = 3600
GAS_FAIL_TTL_S = 120                     # a failed / empty sample is retried after 2 min, not cached for 1 h
PRICE_MAX_AGE_S = 120


@dataclass
class Cost:
    native: float | None
    status: str
    source: str

    @property
    def known(self) -> bool:
        return self.native is not None and self.status != "UNKNOWN"


def _addr_word(a: str) -> str:
    return a[2:].rjust(64, "0")


class EvmFees:
    def __init__(self, chain, rpc, clock=time.time):
        self.chain, self.rpc, self.clock = chain, rpc, clock
        self.swap_gas: dict[tuple, tuple] = {}     # (proto, pool) -> (Cost, at)
        self.pools: dict[tuple, str | None] = {}
        self.approvals: dict[tuple, Cost] = {}

    def gas_price(self) -> Cost:
        """Wei per gas for the next block: base fee of the pending block + median priority of the last 5 blocks."""
        try:
            fh = self.rpc.call("eth_feeHistory", [5, "latest", [50]])
            base = int(fh["baseFeePerGas"][-1], 16)
            tips = [int(r[0], 16) for r in fh.get("reward") or [] if r]
            tip = int(statistics.median(tips)) if tips else 0
            return Cost((base + tip) / 1e18, "MEASURED", f"eth_feeHistory: base {base} wei + p50 tip {tip} wei")
        except Exception as e:
            try:
                gp = int(self.rpc.call("eth_gasPrice", []), 16)
                return Cost(gp / 1e18, "MEASURED", f"eth_gasPrice {gp} wei (feeHistory failed: {type(e).__name__})")
            except Exception as e2:
                return Cost(None, "UNKNOWN", f"gas price unavailable: {type(e2).__name__}")

    def pool_of(self, proto, a_in: str, a_out: str, fee: int | None) -> str | None:
        k = (proto.key, a_in, a_out, fee)
        if k in self.pools:
            return self.pools[k]
        pool = None
        try:
            fac = "0x" + self.rpc.call("eth_call", [{"to": proto.address, "data": SEL_FACTORY}, "latest"])[-40:]
            if proto.kind == "v2_router":
                data = SEL_GET_PAIR + _addr_word(a_in) + _addr_word(a_out)
            else:
                data = SEL_GET_POOL + _addr_word(a_in) + _addr_word(a_out) + f"{fee:064x}"
            r = self.rpc.call("eth_call", [{"to": fac, "data": data}, "latest"])
            pool = "0x" + r[-40:] if r and int(r, 16) else None
        except Exception:
            pool = None
        self.pools[k] = pool
        return pool

    def swap_gas_units(self, proto, pool: str | None) -> tuple[float | None, float, str]:
        """(median gasUsed, median L1 fee in native, source) from recent swaps in that pool."""
        if not pool:
            return None, 0.0, "pool unknown"
        k = (proto.key, pool)
        hit = self.swap_gas.get(k)
        if hit and self.clock() - hit[1] < (GAS_TTL_S if hit[0][0] is not None else GAS_FAIL_TTL_S):
            return hit[0] if hit[0][0] is not None else self._protocol_fallback(proto, hit[0])
        try:
            head = int(self.rpc.call("eth_blockNumber", []), 16)
            topics = [[TOPIC_V2_SWAP, TOPIC_V3_SWAP, TOPIC_PCS_V3_SWAP]]
            logs = self.rpc.call("eth_getLogs", [{"fromBlock": hex(head - GAS_SAMPLE_BLOCKS), "toBlock": hex(head),
                                                  "address": pool, "topics": topics}]) or []
            txs = list(dict.fromkeys(lg["transactionHash"] for lg in logs))[-GAS_SAMPLE_TXS:]
            if not txs:
                val = (None, 0.0, f"no swap in pool {pool} in the last {GAS_SAMPLE_BLOCKS} blocks")
            else:
                recs = []                         # one call each: batched receipts are refused by public RPCs
                for t in txs[-8:]:
                    try:
                        recs.append(self.rpc.call("eth_getTransactionReceipt", [t]))
                    except Exception:
                        pass
                if not any(isinstance(r, dict) for r in recs):      # fallback: receipts of the whole block
                    want = set(txs)
                    for bn in sorted({lg.get("blockNumber") for lg in logs if lg.get("blockNumber")})[-2:]:
                        try:
                            recs += [r for r in self.rpc.call("eth_getBlockReceipts", [bn]) or []
                                     if r.get("transactionHash") in want]
                        except Exception:
                            pass
                gas = [int(r["gasUsed"], 16) for r in recs if isinstance(r, dict)]
                l1 = [int(r.get("l1Fee") or "0x0", 16) / 1e18 for r in recs if isinstance(r, dict)]
                val = (statistics.median(gas) if gas else None, statistics.median(l1) if l1 else 0.0,
                       f"median gasUsed of {len(gas)} recent swap txs in pool {pool} (blocks {head - GAS_SAMPLE_BLOCKS}"
                       f"..{head}){'; median L1 fee included' if any(l1) else ''}")
        except Exception as e:
            val = (None, 0.0, f"gas sample failed: {type(e).__name__}")
        self.swap_gas[k] = (val, self.clock())
        return val if val[0] is not None else self._protocol_fallback(proto, val)

    def _protocol_fallback(self, proto, failed: tuple) -> tuple:
        """No sample for this pool: median of the other sampled pools of the SAME protocol on this chain."""
        vals = [v[0] for (pk, _), (v, _) in self.swap_gas.items() if pk == proto.key and v[0] is not None]
        if not vals:
            return failed
        l1 = [v[1] for (pk, _), (v, _) in self.swap_gas.items() if pk == proto.key and v[0] is not None]
        return (statistics.median(vals), statistics.median(l1), f"{failed[2]}; fallback: median gasUsed of "
                f"{len(vals)} sampled {proto.name} pool(s) on this chain")

    def swap_cost(self, proto, a_in: str, a_out: str, fee: int | None, price: Cost) -> Cost:
        units, l1, src = self.swap_gas_units(proto, self.pool_of(proto, a_in, a_out, fee))
        if units is None or not price.known:
            return Cost(None, "UNKNOWN", src if units is None else price.source)
        return Cost(units * price.native + l1, "ESTIMATED", f"{src}; x gas price ({price.source})")

    def approval_cost(self, token: str, spender: str, price: Cost) -> Cost:
        k = (token, spender)
        if k not in self.approvals:
            data = SEL_APPROVE + _addr_word(spender) + "f" * 64
            try:
                g = int(self.rpc.call("eth_estimateGas", [{"from": PAPER_EVM_ADDRESS, "to": token, "data": data}]), 16)
                self.approvals[k] = Cost(float(g), "SIMULATED", f"eth_estimateGas approve({spender}) = {g} gas")
            except Exception as e:
                self.approvals[k] = Cost(None, "UNKNOWN", f"approve estimate failed: {type(e).__name__}")
        units = self.approvals[k]
        if not units.known or not price.known:
            return Cost(None, "UNKNOWN", units.source)
        return Cost(units.native * price.native, units.status, f"{units.source}; x gas price ({price.source})")


class SolanaFees:
    def __init__(self, rpc, clock=time.time):
        self.rpc, self.clock = rpc, clock
        self._rent: Cost | None = None

    def priority_micro_lamports(self) -> Cost:
        try:
            res = self.rpc.call("getRecentPrioritizationFees", [[]]) or []
            vals = sorted(x["prioritizationFee"] for x in res)
            med = vals[len(vals) // 2] if vals else 0
            return Cost(float(med), "MEASURED", f"getRecentPrioritizationFees median of {len(vals)} slots = {med} "
                                                f"micro-lamports / CU")
        except Exception as e:
            return Cost(None, "UNKNOWN", f"priority fees unavailable: {type(e).__name__}")

    def swap_cost(self) -> Cost:
        pr = self.priority_micro_lamports()
        if not pr.known:
            return Cost(None, "UNKNOWN", pr.source)
        lam = SOL_BASE_FEE_LAMPORTS + pr.native * SOL_CU_UPPER / 1e6
        return Cost(lam / 1e9, "ESTIMATED", f"base fee {SOL_BASE_FEE_LAMPORTS} lamports (protocol default) + "
                                            f"{pr.source} x {SOL_CU_UPPER} CU (upper bound, not simulated)")

    def ata_rent(self) -> Cost:
        if self._rent is None:
            try:
                lam = int(self.rpc.call("getMinimumBalanceForRentExemption", [165]))
                self._rent = Cost(lam / 1e9, "MEASURED", f"rent-exempt minimum for a 165-byte token account = {lam} "
                                                        f"lamports (refundable on close; charged once per token)")
            except Exception as e:
                self._rent = Cost(None, "UNKNOWN", f"rent unavailable: {type(e).__name__}")
        return self._rent


def to_quote(cost: Cost, native_price: Cost) -> Cost:
    """native units -> quote-asset units with the DEX-quoted native price."""
    if not cost.known or not native_price.known:
        return Cost(None, "UNKNOWN", f"{cost.source} | {native_price.source}")
    return Cost(cost.native * native_price.native, cost.status, f"{cost.source} | price {native_price.source}")
