"""Read-only report data for the dashboard / API. Nothing here labels a gross spread as profit: every money figure is
a NET ESTIMATE in the chain's quote asset, with its cost breakdown and cost status.

Verdict per (arm, chain), pre-registered (config.EVAL_*):
  NO_EVIDENCE_YET           < EVAL_MIN_CYCLES closed signal cycles or < EVAL_MIN_DAYS days of data
  NOT_EXECUTABLE            fewer than 20 % of candidates reached a filled leg 1 (aborted / reverted / skipped)
  BENCHMARK_UNAVAILABLE     no closed baseline cycle on that chain / arm
  PAPER_LOSS                95 % CI (bootstrap by day) of the mean net per closed cycle entirely < 0
  PAPER_PROFIT_ESTIMATE     CI lower bound > 0, no cycle with an UNKNOWN cost, mean above the baseline mean
  otherwise NO_EVIDENCE_YET (inconclusive). A paper net estimate is never a real profit."""
from __future__ import annotations

import json
import statistics
import time

import numpy as np

from dexarb import config as C
from dexarb.registry import CHAINS, coverage


def _rows(db, q, a=()):
    cur = db.execute(q, a)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def day_ci(values: list[float], days: list[str], seed: int = 1):
    if len(values) < 2:
        return None
    keys = sorted(set(days))
    idx = {k: i for i, k in enumerate(keys)}
    s, n = np.zeros(len(keys)), np.zeros(len(keys))
    for v, d in zip(values, days):
        s[idx[d]] += v
        n[idx[d]] += 1
    pick = np.random.default_rng(seed).integers(0, len(keys), size=(C.BOOT, len(keys)))
    m = s[pick].sum(1) / np.maximum(n[pick].sum(1), 1)
    return float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def max_drawdown(xs: list[float]) -> float:
    eq = peak = dd = 0.0
    for x in xs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


WARMUP_S = 86400


def verdicts(db) -> list[dict]:
    """Cycles started in the first 24 h after the first run (warm-up) are excluded from conclusions."""
    first = db.execute("SELECT MIN(started_at) FROM experiment_runs").fetchone()[0] or 0
    warm_end = first + WARMUP_S
    out = []
    for acc, chain in db.execute("SELECT DISTINCT account, chain FROM paper_cycles").fetchall():
        cyc = _rows(db, "SELECT * FROM paper_cycles WHERE account=? AND chain=? AND started_at >= ?",
                    (acc, chain, warm_end))
        sig = [c for c in cyc if not c["baseline"]]
        base = [c for c in cyc if c["baseline"] and c["status"] == "CLOSED"]
        closed = sorted((c for c in sig if c["status"] == "CLOSED"), key=lambda c: c["closed_at"])
        nets = [c["net"] for c in closed]
        days = [time.strftime("%Y-%m-%d", time.gmtime(c["closed_at"])) for c in closed]
        filled = sum(1 for c in sig if c["status"] in ("LEG1", "OPEN_EXPOSURE", "CLOSED"))
        ci = day_ci(nets, days)
        srt = sorted(nets, reverse=True)
        r = {"account": acc, "chain": chain, "candidates": len(sig), "leg1_filled": filled, "closed": len(closed),
             "open_exposure": sum(1 for c in sig if c["status"] in ("LEG1", "OPEN_EXPOSURE")),
             "aborted": sum(1 for c in sig if c["status"] == "ABORTED"),
             "failed": sum(1 for c in sig if c["status"] == "FAILED"),
             "skipped": sum(1 for c in sig if c["status"] == "SKIPPED"),
             "mean_net": float(np.mean(nets)) if nets else None, "median_net": statistics.median(nets) if nets else None,
             "sum_net": float(np.sum(nets)) if nets else 0.0, "ci95_by_day": ci,
             "max_drawdown": max_drawdown(nets), "worst": min(nets) if nets else None,
             "top3_share": (sum(x for x in srt[:3] if x > 0) / sum(x for x in nets if x > 0))
             if any(x > 0 for x in nets) else None,
             "baseline_closed": len(base), "baseline_mean": float(np.mean([c["net"] for c in base])) if base else None,
             "unknown_cost_cycles": sum(1 for c in closed if c["unknown_costs"]), "days": len(set(days))}
        if len(closed) < C.EVAL_MIN_CYCLES or len(set(days)) < C.EVAL_MIN_DAYS:
            v = "NO_EVIDENCE_YET"
        elif sig and filled / len(sig) < 0.2:
            v = "NOT_EXECUTABLE"
        elif not base:
            v = "BENCHMARK_UNAVAILABLE"
        elif ci and ci[1] < 0:
            v = "PAPER_LOSS"
        elif ci and ci[0] > 0 and not r["unknown_cost_cycles"] and r["mean_net"] > r["baseline_mean"]:
            v = "PAPER_PROFIT_ESTIMATE"
        else:
            v = "NO_EVIDENCE_YET"
        r["verdict"] = v
        out.append(r)
    return out


def overview(lab, store) -> dict:
    db, now = store.db, store.clock()
    counts = dict(db.execute("SELECT status, COUNT(*) FROM opportunities GROUP BY status").fetchall())
    rej = dict(db.execute("SELECT reason, SUM(n) FROM opportunity_rollups GROUP BY reason").fetchall())
    cyc = dict(db.execute("SELECT status, COUNT(*) FROM paper_cycles WHERE baseline=0 GROUP BY status").fetchall())
    fresh = {c: (now - t if t else None) for c, t in lab.last_ok.items()}
    return {"paper_only": True, "now": now, "schema_version": store.version(), "experiment": C.VERSION,
            "chains": {c: {"scans": s["scans"], "evaluated": s["evaluated"], "candidates": s["candidates"],
                           "last_scan_age_s": (now - s["last_scan"]) if s["last_scan"] else None,
                           "data_age_s": fresh.get(c), "last_error": s["last_error"],
                           "native_price": lab.price[c].native if c in lab.price else None}
                       for c, s in lab.stats.items()},
            "opportunities_stored": counts, "evaluations_by_reason": rej, "cycles": cyc,
            "verdicts": verdicts(db), "paper_enabled": lab.paper_enabled}


def opportunities(store, limit: int = 100, status: str | None = None) -> list[dict]:
    q = ("SELECT o.*, b.route AS buy_route, s.route AS sell_route, b.latency_ms AS buy_latency_ms, "
         "s.latency_ms AS sell_latency_ms FROM opportunities o LEFT JOIN quote_snapshots b ON b.id=o.buy_quote_id "
         "LEFT JOIN quote_snapshots s ON s.id=o.sell_quote_id")
    a = []
    if status:
        q += " WHERE o.status=?"
        a.append(status)
    q += " ORDER BY o.id DESC LIMIT ?"
    a.append(limit)
    return _rows(store.db, q, a)


def ledger(store, limit: int = 100) -> dict:
    db = store.db
    return {"cycles": _rows(db, "SELECT * FROM paper_cycles ORDER BY id DESC LIMIT ?", (limit,)),
            "legs": _rows(db, "SELECT * FROM paper_legs ORDER BY id DESC LIMIT ?", (limit * 2,)),
            "balances": _rows(db, "SELECT * FROM paper_balances ORDER BY account, chain, token"),
            "positions": _rows(db, "SELECT * FROM paper_positions WHERE status='OPEN'"),
            "quote_decay": _rows(db, "SELECT account, chain, COUNT(*) n, AVG(quote_decay) mean_decay FROM paper_cycles "
                                     "WHERE quote_decay IS NOT NULL GROUP BY account, chain")}


def health(store) -> dict:
    db, now = store.db, store.clock()
    rows = _rows(db, "SELECT * FROM feed_health ORDER BY chain, protocol")
    for r in rows:
        age = now - r["last_ok"] if r["last_ok"] else None
        r["state"] = "UNAVAILABLE" if age is None else ("STALE" if age > 120 else r["status"])
        r["last_ok_age_s"] = age
    return {"feeds": rows, "coverage": coverage(), "gaps": _rows(db, "SELECT * FROM data_gaps ORDER BY id DESC LIMIT 50"),
            "fees": _rows(db, "SELECT chain, kind, value, status, source, at FROM fee_snapshots ORDER BY id DESC LIMIT 20")}


def research(store) -> dict:
    db = store.db
    by = _rows(db, "SELECT account, chain, token, size, buy_protocol, sell_protocol, COUNT(*) n, AVG(net) mean_net, "
                   "SUM(net) sum_net FROM paper_cycles WHERE status='CLOSED' AND baseline=0 GROUP BY 1,2,3,4,5,6")
    rej = _rows(db, "SELECT chain, reason, SUM(n) n, MAX(best_gross) best_gross FROM opportunity_rollups GROUP BY 1,2")
    return {"config": _rows(db, "SELECT * FROM experiment_config"), "runs": _rows(db, "SELECT * FROM experiment_runs "
                                                                                     "ORDER BY id DESC LIMIT 20"),
            "by_route": by, "evaluations_by_reason": rej, "verdicts": verdicts(db),
            "tests_run": len(by) + len(rej), "atomic_model": "NOT_SUPPORTED for every connector (model B)"}


def storage(store, durable: tuple) -> dict:
    tb = store.table_bytes()
    return {"db_path": str(store.path), "durable": durable[0], "durability_note": durable[1],
            "db_mb": round(store.size_mb(), 2), "quota_mb": store.max_mb, "over_quota": store.over_quota(),
            "tables": tb, "bytes_per_day": store.bytes_per_day(),
            "retention": {"quote_snapshots_days": 7, "rejected_opportunities_days": 30,
                          "kept": "rollups, ledger, cycles, legs, audit"},
            "last_backup": _rows(store.db, "SELECT at, detail FROM audit_events WHERE kind='backup' ORDER BY id DESC "
                                           "LIMIT 1"),
            "schema_version": store.version()}


def chain_list() -> list[dict]:
    return [{"key": c.key, "chain_id": c.chain_id, "quote_asset": c.quote_asset.symbol, "native": c.native,
             "tokens": [t.symbol for t in c.tokens], "wallets": c.wallets} for c in CHAINS.values()]


def to_json(o) -> bytes:
    return json.dumps(o, default=str).encode()
