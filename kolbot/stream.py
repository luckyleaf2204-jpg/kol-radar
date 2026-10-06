"""pump.fun bonding-curve events from the public Solana RPC (logsSubscribe on the pump.fun program). Read-only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import time

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WS_URL = "wss://api.mainnet-beta.solana.com"
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
COMPLETE_DISC = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def plausible_ts(ts: int) -> bool:
    return 1_704_067_200 <= ts <= time.time() + 86400


def decode(line: str) -> dict | None:
    """'Program data: <base64>' -> trade / complete event, or None.

    TradeEvent layout (checked live 2026-10-06): disc 8 | mint 32 | sol u64 | token u64 | is_buy u8 | user 32 |
    timestamp i64 | virtual_sol u64 | virtual_token u64 | real_sol u64 | real_token u64 | fee_recipient 32 |
    fee_bps u64 | fee u64 | creator 32 | creator_fee_bps u64 | creator_fee u64 ... Reserves are AFTER the trade."""
    if not line.startswith("Program data: "):
        return None
    try:
        d = base64.b64decode(line[14:])
    except ValueError:
        return None
    if d[:8] == TRADE_DISC and len(d) >= 129:
        sol, tok = struct.unpack_from("<QQ", d, 40)
        ts = struct.unpack_from("<q", d, 89)[0]
        vs, vt = struct.unpack_from("<QQ", d, 97)
        if not plausible_ts(ts) or tok <= 0 or vs <= 0 or vt <= 0:
            return None
        fee_bps = None
        if len(d) >= 225:
            fee_bps = struct.unpack_from("<Q", d, 161)[0] + struct.unpack_from("<Q", d, 209)[0]
        creator = b58(d[177:209]) if len(d) >= 209 else None
        return {"kind": "trade", "mint": b58(d[8:40]), "sol": sol, "token": tok, "is_buy": bool(d[56]),
                "user": b58(d[57:89]), "ts": ts, "vsol": vs, "vtok": vt, "fee_bps": fee_bps, "creator": creator}
    if d[:8] == CREATE_DISC:
        return _decode_create(d)
    if d[:8] == COMPLETE_DISC and len(d) >= 112:
        ts = struct.unpack_from("<q", d, 104)[0]
        return {"kind": "complete", "mint": b58(d[40:72]), "ts": ts} if plausible_ts(ts) else None
    return None


def _decode_create(d: bytes) -> dict | None:
    """CreateEvent (checked live 2026-10-06): disc | name str | symbol str | uri str | mint 32 | bonding_curve 32 |
    user 32 | creator 32 | timestamp i64 ... (str = u32 length + utf-8)."""
    try:
        o, out = 8, []
        for _ in range(3):
            n = struct.unpack_from("<I", d, o)[0]
            if n > 400:
                return None
            out.append(d[o + 4:o + 4 + n].decode("utf-8", "replace"))
            o += 4 + n
        ts = struct.unpack_from("<q", d, o + 128)[0]
    except struct.error:
        return None
    if not plausible_ts(ts):
        return None
    return {"kind": "create", "mint": b58(d[o:o + 32]), "name": out[0][:80], "symbol": out[1][:30],
            "user": b58(d[o + 64:o + 96]), "creator": b58(d[o + 96:o + 128]), "ts": ts}


async def listen(on_event, on_gap, stop: asyncio.Event, url: str = WS_URL, log=print, connect=None) -> None:
    """Call on_event(ev) for every decoded event until stop; reconnect with backoff, report each outage."""
    import websockets
    connect = connect or (lambda: websockets.connect(url, max_size=2 ** 24, ping_interval=20, ping_timeout=30))
    down_since, backoff = None, 1.0
    while not stop.is_set():
        try:
            async with connect() as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                          "params": [{"mentions": [PUMP]}, {"commitment": "confirmed"}]}))
                await ws.recv()
                if down_since is not None:
                    on_gap(down_since, time.time())
                down_since, backoff = None, 1.0
                log("[kol] stream connected")
                while not stop.is_set():
                    v = json.loads(await asyncio.wait_for(ws.recv(), 60)).get("params", {}).get("result", {})
                    val = v.get("value") or {}
                    if val.get("err") is None:
                        for line in val.get("logs") or []:
                            ev = decode(line)
                            if ev:
                                on_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if down_since is None:
                down_since = time.time()
            log(f"[kol] stream error {type(e).__name__}: {str(e)[:120]} -> reconnect in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)
