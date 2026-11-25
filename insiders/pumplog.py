"""pump.fun program events from transaction LOGS ("Program data: <base64>" lines): CreateEvent (a new mint) and
TradeEvent (a buy / sell with wallet, SOL, tokens and curve reserves). No getTransaction needed: one logsSubscribe on
the pump.fun program gives every new coin and every trade. Read only."""
from __future__ import annotations

import base64
import hashlib
import struct

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
COMPLETE = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
TOKEN_DEC = 6
SUPPLY = 1_000_000_000                      # pump.fun tokens (whole) per coin


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPH[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


class _R:
    def __init__(self, b: bytes):
        self.b, self.i = b, 8

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise ValueError("short")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def key(self) -> str:
        return b58(self.take(32))

    def s(self) -> str:
        n = struct.unpack("<I", self.take(4))[0]
        if n > 512:
            raise ValueError("string")
        return self.take(n).decode("utf-8", "replace")

    def opt(self, fn):
        """A trailing field of a newer program version: None when the event is from an older layout."""
        try:
            return fn()
        except ValueError:
            return None


def decode(data: bytes) -> dict | None:
    d = data[:8]
    try:
        r = _R(data)
        if d == CREATE:
            e = {"ev": "create", "name": r.s(), "symbol": r.s(), "uri": r.s(), "mint": r.key(), "bonding_curve": r.key(), "user": r.key()}
            e["creator"] = r.opt(r.key) or e["user"]
            e["ts"] = r.opt(r.i64)
            e["vtok"], e["vsol"] = r.opt(r.u64), r.opt(r.u64)
            return e
        if d == COMPLETE:
            return {"ev": "complete", "user": r.key(), "mint": r.key(), "bonding_curve": r.key(), "ts": r.opt(r.i64)}
        if d == TRADE:
            e = {"ev": "trade", "mint": r.key(), "sol": r.u64(), "tokens": r.u64(), "buy": r.take(1) == b"\x01", "user": r.key(),
                 "ts": r.i64(), "vsol": r.u64(), "vtok": r.u64()}
            e["rsol"] = r.opt(r.u64)
            return e
    except (ValueError, struct.error):
        return None
    return None


def events(logs: list[str]) -> list[dict]:
    """Only data emitted while pump.fun itself is executing: forks of pump.fun reuse the same event layout and show up
    in the same transactions, so the invoke stack is followed ("Program X invoke [n]" / "Program X success|failed")."""
    out, stack = [], []
    for line in logs or []:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split()[1])
            continue
        if line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
            if stack:
                stack.pop()
            continue
        if line.startswith("Program data: ") and stack and stack[-1] == PUMP:
            try:
                e = decode(base64.b64decode(line[14:]))
            except ValueError:
                continue
            if e:
                out.append(e)
    return out


def price_sol(vsol: int | None, vtok: int | None) -> float | None:
    """SOL per whole token from the curve's virtual reserves."""
    if not vsol or not vtok:
        return None
    return (vsol / 1e9) / (vtok / 10 ** TOKEN_DEC)
