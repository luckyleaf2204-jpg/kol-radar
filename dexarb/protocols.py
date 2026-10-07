"""Per-DEX quote adapters (read-only). Each quote is for the EXACT token in / out and amount (never a small-size price
scaled linearly), and records its route, fee provenance, context (block / slot), fetch time and latency.

  v2_router           router.getAmountsOut(amount, [in, out]) via eth_call: one hop through that router's pair.
                      Pool fee is inside the output (INCLUDED_IN_QUOTE). Impact = 1 - effective / small-size price,
                      both quoted on chain in the same batch.
  v3_quoter           QuoterV2.quoteExactInputSingle for every fee tier in one batch; the best tier is the route (the
                      tier is recorded). Fee INCLUDED_IN_QUOTE. Impact as above at the chosen tier.
  jupiter_single_dex  Jupiter /swap/v1/quote with dexes=<label> and onlyDirectRoutes=true; accepted only if every
                      routePlan hop carries that label (otherwise NO_ROUTE "route_not_on_dex"). Impact from the
                      provider (priceImpactPct). contextSlot recorded. Platform fee must be absent.
EVM quotes use block tag "latest"; the batch reply is matched to the head read in the same batch (context)."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from dexarb.keccak import selector
from dexarb.registry import Asset, Protocol

SEL_GAO = selector("getAmountsOut(uint256,address[])")
SEL_Q1 = selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
V3_TIERS = {"uniswap_v3": (100, 500, 3000, 10000), "pancakeswap_v3": (100, 500, 2500, 10000)}
JUP_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUP_HOSTS = ("lite-api.jup.ag",)


@dataclass
class Quote:
    chain: str
    protocol: str
    token_in: str
    token_out: str
    amount_in: int                       # raw units
    amount_out: int | None               # raw units; None = failed
    status: str = "OK"                   # OK | NO_ROUTE | NO_QUOTE | ERROR
    error: str = ""
    route: list = field(default_factory=list)
    fee_note: str = "INCLUDED_IN_QUOTE"
    impact: float | None = None
    context: int | None = None           # EVM block / Solana slot
    fetched_at: float = 0.0
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.amount_out is not None and self.amount_out > 0


def _w(x: int) -> str:
    return f"{x:064x}"


def _a(addr: str) -> str:
    return addr[2:].rjust(64, "0")


def v2_call(router: str, a_in: Asset, a_out: Asset, amount: int):
    data = SEL_GAO + _w(amount) + _w(64) + _w(2) + _a(a_in.address) + _a(a_out.address)
    return ("eth_call", [{"to": router, "data": data}, "latest"])


def v3_call(quoter: str, a_in: Asset, a_out: Asset, amount: int, fee: int):
    data = SEL_Q1 + _a(a_in.address) + _a(a_out.address) + _w(amount) + _w(fee) + _w(0)
    return ("eth_call", [{"to": quoter, "data": data}, "latest"])


def _int_last(res) -> int | None:
    if not isinstance(res, str) or len(res) < 66:
        return None
    return int(res[-64:], 16)


def _int_first(res) -> int | None:
    if not isinstance(res, str) or len(res) < 66:
        return None
    return int(res[2:66], 16)


def evm_quotes(rpc, chain: str, protos: list[Protocol], reqs: list[tuple], clock=time.time) -> list[Quote]:
    """reqs: (proto_key, asset_in, asset_out, amount). One JSON-RPC batch for everything (+ head for context)."""
    by_key = {p.key: p for p in protos}
    calls, plan = [("eth_blockNumber", [])], []
    for pk, a_in, a_out, amt in reqs:
        p = by_key[pk]
        small = max(1, amt // 1000)
        if p.kind == "v2_router":
            plan.append((pk, a_in, a_out, amt, [(None, len(calls), len(calls) + 1)]))
            calls += [v2_call(p.address, a_in, a_out, amt), v2_call(p.address, a_in, a_out, small)]
        else:
            tiers = []
            for fee in V3_TIERS.get(pk, (100, 500, 3000, 10000)):
                tiers.append((fee, len(calls), len(calls) + 1))
                calls += [v3_call(p.address, a_in, a_out, amt, fee), v3_call(p.address, a_in, a_out, small, fee)]
            plan.append((pk, a_in, a_out, amt, tiers))
    t0 = clock()
    try:
        res = rpc.batch(calls)
    except Exception as e:
        t1 = clock()
        return [Quote(chain, pk, a_in.symbol, a_out.symbol, amt, None, "ERROR", f"{type(e).__name__}: {str(e)[:120]}",
                      fetched_at=t1, latency_ms=1000 * (t1 - t0)) for pk, a_in, a_out, amt, _ in plan]
    t1 = clock()
    head = int(res[0], 16) if isinstance(res[0], str) else None
    out = []
    for pk, a_in, a_out, amt, opts in plan:
        p = by_key[pk]
        best = None
        for fee, i_big, i_small in opts:
            big = _int_last(res[i_big]) if p.kind == "v2_router" else _int_first(res[i_big])
            sm = _int_last(res[i_small]) if p.kind == "v2_router" else _int_first(res[i_small])
            if big and (best is None or big > best[0]):
                best = (big, sm, fee)
        if best is None:
            out.append(Quote(chain, pk, a_in.symbol, a_out.symbol, amt, None, "NO_ROUTE", "no pool / reverted",
                             context=head, fetched_at=t1, latency_ms=1000 * (t1 - t0)))
            continue
        big, sm, fee = best
        small = max(1, amt // 1000)
        impact = (1 - (big / amt) / (sm / small)) if sm else None
        route = [{"venue": p.name, "via": p.address, "path": [a_in.address, a_out.address],
                  **({"fee_tier": fee} if fee else {})}]
        out.append(Quote(chain, pk, a_in.symbol, a_out.symbol, amt, big, route=route,
                         fee_note=f"INCLUDED_IN_QUOTE ({p.pool_types}{f', tier {fee / 1e4:.2f} %' if fee else ''})",
                         impact=max(0.0, impact) if impact is not None else None, context=head, fetched_at=t1,
                         latency_ms=1000 * (t1 - t0)))
    return out


def http_get_json(url: str, timeout: float = 15.0):
    host = urllib.parse.urlsplit(url).hostname
    if host not in JUP_HOSTS or "/quote" not in url:
        raise PermissionError(f"read-only quote client: {host} {urllib.parse.urlsplit(url).path} not allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "kol-radar-dexarb/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def jupiter_quote(p: Protocol, a_in: Asset, a_out: Asset, amount: int, get=http_get_json, clock=time.time,
                  slippage_bps: int = 50) -> Quote:
    url = (f"{JUP_URL}?inputMint={a_in.address}&outputMint={a_out.address}&amount={amount}"
           f"&slippageBps={slippage_bps}&onlyDirectRoutes=true&dexes={urllib.parse.quote(p.address)}")
    t0 = clock()
    try:
        d = get(url)
    except Exception as e:
        t1 = clock()
        st = "NO_ROUTE" if "400" in str(e) or "No routes" in str(e) else "ERROR"
        return Quote("solana", p.key, a_in.symbol, a_out.symbol, amount, None, st, f"{type(e).__name__}: {str(e)[:120]}",
                     fetched_at=t1, latency_ms=1000 * (t1 - t0))
    t1 = clock()
    plan = d.get("routePlan") or []
    labels = [(x.get("swapInfo") or {}).get("label") for x in plan]
    if not plan or any(lb != p.address for lb in labels):
        return Quote("solana", p.key, a_in.symbol, a_out.symbol, amount, None, "NO_ROUTE",
                     f"route_not_on_dex {labels}", fetched_at=t1, latency_ms=1000 * (t1 - t0),
                     context=d.get("contextSlot"))
    if d.get("platformFee"):
        return Quote("solana", p.key, a_in.symbol, a_out.symbol, amount, None, "NO_QUOTE", "platform_fee_present",
                     fetched_at=t1, latency_ms=1000 * (t1 - t0))
    route = [{"venue": lb, "amm": (x.get("swapInfo") or {}).get("ammKey"), "percent": x.get("percent")}
             for lb, x in zip(labels, plan)]
    try:
        impact = float(d.get("priceImpactPct") or 0)
    except ValueError:
        impact = None
    return Quote("solana", p.key, a_in.symbol, a_out.symbol, amount, int(d["outAmount"]), route=route,
                 fee_note="INCLUDED_IN_QUOTE (provider outAmount is net of pool fees)", impact=impact,
                 context=d.get("contextSlot"), fetched_at=t1, latency_ms=1000 * (t1 - t0))
