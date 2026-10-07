"""Read-only live check of every DEX connector -> docs/dexarb_connector_verification.json.

A protocol is "ok" (SUPPORTED) only if, on its chain, every registered token was quoted at every registered size in
BOTH directions (quote asset -> token at the size, then that exact token amount -> quote asset) with status OK, a
known route on that venue, a context (block / slot) and an impact value. Anything else -> not ok, with the reason.
Nothing is signed or sent.

usage: python tools/dexarb_verify.py [--chains base,solana]"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dexarb import config as C  # noqa: E402
from dexarb.engine import human, raw  # noqa: E402
from dexarb.protocols import evm_quotes, jupiter_quote  # noqa: E402
from dexarb.registry import CHAINS, VERIFY_FILE  # noqa: E402
from dexarb.rpc import Rpc  # noqa: E402


def check(chain: str) -> dict:
    ch = CHAINS[chain]
    out = {}
    sizes = C.SCAN_SIZES_SOLANA if chain == "solana" else C.SIZES
    rpc = Rpc(ch.rpc_url()) if ch.kind == "evm" else None
    for p in ch.protocols:
        problems, samples = [], []
        for tok in ch.tokens:
            for size in sizes:
                if ch.kind == "evm":
                    b = evm_quotes(rpc, chain, [p], [(p.key, ch.quote_asset, tok, raw(size, ch.quote_asset))])[0]
                else:
                    b = jupiter_quote(p, ch.quote_asset, tok, raw(size, ch.quote_asset))
                    time.sleep(1.1)
                if not b.ok:
                    problems.append(f"buy {tok.symbol} {size}: {b.status} {b.error[:80]}")
                    continue
                if ch.kind == "evm":
                    s = evm_quotes(rpc, chain, [p], [(p.key, tok, ch.quote_asset, b.amount_out)])[0]
                else:
                    s = jupiter_quote(p, tok, ch.quote_asset, b.amount_out)
                    time.sleep(1.1)
                if not s.ok:
                    problems.append(f"sell {tok.symbol} {size}: {s.status} {s.error[:80]}")
                    continue
                if b.context is None or b.impact is None or not b.route:
                    problems.append(f"{tok.symbol} {size}: missing context / impact / route")
                samples.append({"token": tok.symbol, "size": size, "round_trip_out": human(s.amount_out, ch.quote_asset),
                                "impact_buy": b.impact, "impact_sell": s.impact, "context": b.context,
                                "route": b.route, "latency_ms": round(b.latency_ms)})
        out[f"{chain}/{p.key}"] = {"ok": not problems, "checked_at": time.time(), "samples": samples,
                                   "note": "; ".join(problems)[:400] if problems else
                                   f"{len(samples)} round trips quoted on {p.name}"}
        print(f"{chain:9s} {p.key:16s} ok={not problems} " + ("; ".join(problems)[:150] if problems else
              " ".join(f"{x['token']}@{x['size']:g}->{x['round_trip_out']:.4f}" for x in samples)), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default=",".join(CHAINS))
    a = ap.parse_args()
    res = {}
    for c in [x for x in a.chains.split(",") if x]:
        try:
            res.update(check(c))
        except Exception as e:
            for p in CHAINS[c].protocols:
                res[f"{c}/{p.key}"] = {"ok": False, "checked_at": time.time(), "note": f"error {type(e).__name__}: {e}"}
            print(c, "ERROR", e)
    old = {}
    try:
        old = json.loads(VERIFY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    old.update(res)
    VERIFY_FILE.write_text(json.dumps(old, indent=1, sort_keys=True), encoding="utf-8")
    print("written", VERIFY_FILE)


if __name__ == "__main__":
    main()
