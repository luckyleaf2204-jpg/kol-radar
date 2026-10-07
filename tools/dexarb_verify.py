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
from dexarb.simulate import Simulator  # noqa: E402

SIM_TOL = 0.001                           # simulated output must match the quote within 0.1 %


def check_sim(chain: str) -> dict:
    """Unsigned-swap simulation (state override) vs quote, both directions, size 1000, every token."""
    ch = CHAINS[chain]
    rpc = Rpc(ch.rpc_url())
    sim = Simulator(chain, rpc)
    out = {}
    for p in ch.protocols:
        rows, bad = [], []
        for tok in ch.tokens:
            amt = raw(1000.0, ch.quote_asset)
            for a_in, a_out in ((ch.quote_asset, tok), (tok, ch.quote_asset)):
                q = evm_quotes(rpc, chain, [p], [(p.key, a_in, a_out, amt)])[0]
                if not q.ok:
                    bad.append(f"{a_in.symbol}->{a_out.symbol}: quote {q.status}")
                    break
                fee = q.route[0].get("fee_tier") if q.route else None
                r = sim.swap(p, a_in, a_out, amt, 0, fee)
                dev = abs(r.amount_out - q.amount_out) / q.amount_out if r.amount_out else None
                rows.append({"dir": f"{a_in.symbol}->{a_out.symbol}", "status": r.status, "deviation": dev,
                             "gas": r.gas_units, "router": r.target, "error": r.error[:100]})
                if r.status != "SIMULATED_OK" or dev is None or dev > SIM_TOL or r.gas_units is None:
                    bad.append(f"{a_in.symbol}->{a_out.symbol}: {r.status} dev={dev} {r.error[:60]}")
                amt = q.amount_out                # the sell direction uses the bought amount
        out[p.key] = {"sim_ok": not bad, "sim_checked_at": time.time(), "sim_samples": rows,
                      "sim_note": "; ".join(bad)[:300]}
        print(f"{chain:9s} {p.key:16s} sim_ok={not bad} " + ("; ".join(bad)[:140] if bad else
              " ".join(f"{x['dir']} gas={x['gas']} dev={x['deviation']:.1e}" for x in rows)), flush=True)
    return out


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
    ap.add_argument("--sim", action="store_true", help="only the EVM unsigned-swap simulation check")
    a = ap.parse_args()
    res = {}
    if a.sim:
        old = json.loads(VERIFY_FILE.read_text(encoding="utf-8")) if VERIFY_FILE.exists() else {}
        for c in [x for x in a.chains.split(",") if x and CHAINS[x].kind == "evm"]:
            try:
                for pk, v in check_sim(c).items():
                    old.setdefault(f"{c}/{pk}", {}).update(v)
            except Exception as e:
                print(c, "SIM ERROR", e)
        VERIFY_FILE.write_text(json.dumps(old, indent=1, sort_keys=True), encoding="utf-8")
        print("written", VERIFY_FILE)
        return
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
    for k, v in res.items():                 # merge: keeps the simulation fields of an earlier --sim run
        old.setdefault(k, {}).update(v)
    VERIFY_FILE.write_text(json.dumps(old, indent=1, sort_keys=True), encoding="utf-8")
    print("written", VERIFY_FILE)


if __name__ == "__main__":
    main()
