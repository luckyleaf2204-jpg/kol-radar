"""Paper results: net return per copy after every cost, with a bootstrap CI that resamples whole KOLs (copies of
the same KOL are correlated). No conclusion is drawn under 100 trades."""
from __future__ import annotations

MIN_PRELIM, MIN_OK = 30, 100


def cluster_ci(rows: list[dict], key: str = "kol", iters: int = 2000, seed: int = 7):
    from kolbot.api import cluster_ci as fast        # same draws as before, O(groups) per draw
    g: dict[str, list[float]] = {}
    for r in rows:
        g.setdefault(r[key], []).append(r["net_pct"])
    return fast(list(g.values()), iters, seed)


def summarize(closed: list[dict]) -> dict:
    rows = [r for r in closed if not r.get("gap")]
    n = len(rows)
    out = {"n": n, "excluded_gap": len(closed) - n}
    if not n:
        out["status"] = "INSUFFICIENT"
        return out
    nets = sorted(r["net_pct"] for r in rows)
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["exit_kind"]] = kinds.get(r["exit_kind"], 0) + 1
    ci = cluster_ci(rows)
    status = "INSUFFICIENT" if n < MIN_PRELIM else ("PRELIMINARY" if n < MIN_OK else "OK")
    if status != "OK" or ci is None:
        verdict = "Chưa đủ dữ liệu để kết luận"
    elif ci[0] > 0:
        verdict = "Có lãi sau chi phí (CI theo KOL > 0)"
    elif ci[1] < 0:
        verdict = "Lỗ sau chi phí (CI theo KOL < 0)"
    else:
        verdict = "Không phân biệt được với 0"
    out.update({"status": status, "verdict": verdict, "pnl_sol": round(sum(r["pnl_sol"] for r in rows), 4),
                "mean_net_pct": round(sum(nets) / n, 2), "median_net_pct": round(nets[n // 2], 2),
                "win_rate_pct": round(100 * sum(1 for x in nets if x > 0) / n, 1), "ci95_by_kol": ci,
                "kols": len({r["kol"] for r in rows}), "tokens": len({r["mint"] for r in rows}),
                "exit_kinds": kinds, "best_pct": round(nets[-1], 1), "worst_pct": round(nets[0], 1)})
    return out


def by_kol(closed: list[dict], names: dict[str, str], top: int = 15) -> list[tuple]:
    g: dict[str, list[dict]] = {}
    for r in closed:
        if not r.get("gap"):
            g.setdefault(r["kol"], []).append(r)
    rows = [(names.get(k, k[:8]), len(v), round(sum(x["pnl_sol"] for x in v), 4),
             round(sum(x["net_pct"] for x in v) / len(v), 1)) for k, v in g.items()]
    return sorted(rows, key=lambda t: -t[1])[:top]
