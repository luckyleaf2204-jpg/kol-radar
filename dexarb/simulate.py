"""EVM simulation of the exact UNSIGNED swap of a paper leg — read-only (eth_call / eth_estimateGas with a state
override). Nothing is signed, nothing is broadcast; the override exists only inside the RPC node's call.

The paper address (fees.PAPER_EVM_ADDRESS, holds no key) is given, inside the call only, the input-token balance and
an allowance for the router by overriding the token's storage slots. The slots are found by probing: slot s is the
balance mapping if overriding keccak(addr . s) makes balanceOf(addr) return the written value (same for the
allowance mapping with allowance(owner, spender)). Tokens whose layout is not found -> UNAVAILABLE.

Calls simulated (amountOutMinimum = the leg's min-out, so a slippage failure reverts exactly as on chain):
  v2_router  swapExactTokensForTokens(amountIn, minOut, [in, out], paper, deadline) on the registered router
  v3_quoter  exactInputSingle((in, out, fee, paper, amountIn, minOut, 0)) on the protocol's swap router (SwapRouter02
             / PancakeSwap SmartRouter; addresses registered in EXEC_ROUTERS and only used where the live check
             matched the quote)
Result: SIMULATED_OK (output + gasUsed from eth_estimateGas), SIM_REVERT (reason), UNAVAILABLE (no slot / no router /
RPC refused overrides). A fill based on SIMULATED_OK is labelled SIMULATED; otherwise the leg stays QUOTE_ONLY.
OP-stack L1 data fees are not part of eth_estimateGas: they remain ESTIMATED from the recent same-pool sample."""
from __future__ import annotations

import time
from dataclasses import dataclass

from dexarb.fees import PAPER_EVM_ADDRESS as PAPER
from dexarb.keccak import keccak256, selector

SEL_BAL = selector("balanceOf(address)")
SEL_ALW = selector("allowance(address,address)")
SEL_V2_SWAP = selector("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)")
SEL_V3_SWAP = selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
SLOTS = list(range(0, 21)) + [51, 52]
PROBE = 12345678901234567890
EXEC_ROUTERS = {        # protocol swap routers (from public docs; used only after a live match with the quote)
    ("base", "uniswap_v3"): "0x2626664c2603336e57b271c5c0b26f421741e481",
    ("base", "pancakeswap_v3"): "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",
    ("ethereum", "uniswap_v3"): "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
    ("polygon", "uniswap_v3"): "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
    ("bnb", "uniswap_v3"): "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2",
    ("bnb", "pancakeswap_v3"): "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",
}


@dataclass
class SimResult:
    status: str                 # SIMULATED_OK | SIM_REVERT | UNAVAILABLE
    amount_out: int | None = None
    gas_units: int | None = None
    error: str = ""
    target: str = ""
    at: float = 0.0


def _w(x: int) -> str:
    return f"{x:064x}"


def _a(x: str) -> str:
    return x[2:].rjust(64, "0")


def balance_key(owner: str, slot: int) -> str:
    return "0x" + keccak256(bytes.fromhex(_a(owner) + _w(slot))).hex()


def allowance_key(owner: str, spender: str, slot: int) -> str:
    inner = keccak256(bytes.fromhex(_a(owner) + _w(slot))).hex()
    return "0x" + keccak256(bytes.fromhex(_a(spender) + inner)).hex()


class Simulator:
    def __init__(self, chain: str, rpc, clock=time.time):
        self.chain, self.rpc, self.clock = chain, rpc, clock
        self.slots: dict[str, tuple] = {}            # token -> (balance slot, allowance slot) or (None, None)

    def _probe(self, token: str, data: str, key: str) -> bool:
        ov = {token: {"stateDiff": {key: "0x" + _w(PROBE)}}}
        out = self.rpc.call("eth_call", [{"to": token, "data": data}, "latest", ov])
        return isinstance(out, str) and len(out) >= 66 and int(out[2:66], 16) == PROBE

    def find_slots(self, token: str) -> tuple:
        if token in self.slots:
            return self.slots[token]
        bal = alw = None
        spender = "0x" + "11" * 20
        try:
            bal = next((s for s in SLOTS if self._probe(token, SEL_BAL + _a(PAPER), balance_key(PAPER, s))), None)
            alw = next((s for s in SLOTS if self._probe(token, SEL_ALW + _a(PAPER) + _a(spender),
                                                        allowance_key(PAPER, spender, s))), None)
        except Exception:
            bal = alw = None
        self.slots[token] = (bal, alw)
        return bal, alw

    def swap(self, proto, a_in, a_out, amount: int, min_out: int, fee_tier: int | None) -> SimResult:
        now = self.clock()
        if proto.kind == "v2_router":
            router = proto.address
            data = (SEL_V2_SWAP + _w(amount) + _w(min_out) + _w(160) + _a(PAPER) + _w(2 ** 40) + _w(2)
                    + _a(a_in.address) + _a(a_out.address))
        else:
            router = EXEC_ROUTERS.get((self.chain, proto.key))
            if not router or not fee_tier:
                return SimResult("UNAVAILABLE", error="no registered swap router / fee tier", at=now)
            data = (SEL_V3_SWAP + _a(a_in.address) + _a(a_out.address) + _w(fee_tier) + _a(PAPER) + _w(amount)
                    + _w(min_out) + _w(0))
        bal, alw = self.find_slots(a_in.address)
        if bal is None or alw is None:
            return SimResult("UNAVAILABLE", error=f"storage layout of {a_in.symbol} not found", target=router, at=now)
        ov = {a_in.address: {"stateDiff": {balance_key(PAPER, bal): "0x" + _w(amount),
                                           allowance_key(PAPER, router, alw): "0x" + "f" * 64}}}
        tx = {"from": PAPER, "to": router, "data": data}
        try:
            out = self.rpc.call("eth_call", [tx, "latest", ov])
        except Exception as e:
            msg = str(e)
            st = "SIM_REVERT" if "revert" in msg.lower() or "execution" in msg.lower() else "UNAVAILABLE"
            return SimResult(st, error=msg[:200], target=router, at=now)
        if not isinstance(out, str) or len(out) < 66:
            return SimResult("UNAVAILABLE", error="empty result", target=router, at=now)
        amount_out = int(out[-64:], 16) if proto.kind == "v2_router" else int(out[2:66], 16)
        try:
            gas = int(self.rpc.call("eth_estimateGas", [tx, "latest", ov]), 16)
        except Exception as e:
            return SimResult("SIMULATED_OK", amount_out, None, f"estimateGas failed: {str(e)[:120]}", router, now)
        return SimResult("SIMULATED_OK", amount_out, gas, "", router, now)
