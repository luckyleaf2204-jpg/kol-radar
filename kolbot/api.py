"""Read-only queries behind the dashboard. Everything is aggregated in SQLite (GROUP BY, LIMIT/OFFSET) so the
browser never receives the raw trade list; per-KOL bootstrap CIs are cached. Nothing here writes, and nothing here
changes how trades are copied, priced or judged.

Status of a KOL or of the whole sample (direction C, docs/kol_plan.md of sol_memecoin_hunter, applied to paper
trades; trades overlapping a stream gap are excluded as in the report):
  INCONCLUSIVE  n < 100
  REJECT        n >= 100 and the lower bound of the 95 % CI <= 0
  PROVISIONAL   n >= 100 and the lower bound > 0, but there is no random-wallet control in the paper bot, so the
                pre-registered PASS (which also needs random p < 0.05) cannot be established here
  PASS          never assigned by this dashboard (needs the control group)"""
from __future__ import annotations

import csv
import io
import random
import time

MIN_N = 100
RANGES = {"today": "today", "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
SORTS = {"pnl": "pnl_sol", "roi": "roi_pct", "win": "win_rate_pct", "trades": "n", "avg": "avg_pnl_sol",
         "pnl7": "pnl_7d_sol", "pnl30": "pnl_30d_sol", "first": "first_seen_at", "wins": "wins",
         "losses": "losses", "median": "median_pnl_sol", "best": "best_pct", "worst": "worst_pct",
         "streak": "streak", "last": "last_seen_at", "consistent": "win_rate_low_pct"}
HIGH_WIN_RATE_PCT = 60.0               # "high win rate only" filter
STATUSES = ("PASS", "PROVISIONAL", "INCONCLUSIVE", "REJECT")
CI_ITERS = 1000
_cache: dict = {}
_ci: dict = {}                         # per-KOL bootstrap CIs, keyed by (kol, range, n, sum)


def solscan(kind: str, ident: str) -> str:
    """Explorer link. The bot only reads Solana (pump.fun), so Solscan is the right explorer."""
    return f"https://solscan.io/{'account' if kind == 'wallet' else 'token' if kind == 'token' else 'tx'}/{ident}"


def since_of(rng: str, now: float | None = None) -> float:
    now = now or time.time()
    span = RANGES.get(rng or "all", None)
    if span == "today":                                   # since 00:00 UTC
        return now - now % 86400
    return 0.0 if span is None else (now - span) // 60 * 60    # minute steps: cache-friendly, stable


def boot_ci(values: list[float], iters: int = CI_ITERS, seed: int = 7):
    """95 % percentile-bootstrap CI of the mean (trades resampled), vectorised and chunked for memory."""
    import numpy as np
    n = len(values)
    if n < 2:
        return None
    arr = np.asarray(values, dtype=float)
    gen = np.random.default_rng(seed)
    step = max(1, 2_000_000 // n)
    means = np.concatenate([arr[gen.integers(0, n, size=(min(step, iters - i), n))].mean(axis=1)
                            for i in range(0, iters, step)])
    lo, hi = np.quantile(means, [0.025, 0.975])
    return round(float(lo), 2), round(float(hi), 2)


def cluster_ci(groups: list[list[float]], iters: int = 2000, seed: int = 7):
    """95 % CI resampling whole groups (KOLs); O(groups) per draw."""
    import numpy as np
    if len(groups) < 2:
        return None
    sums = np.array([sum(g) for g in groups], dtype=float)
    lens = np.array([len(g) for g in groups], dtype=float)
    k = len(groups)
    gen = np.random.default_rng(seed)
    step = max(1, 2_000_000 // k)
    means = np.concatenate([(lambda idx: sums[idx].sum(axis=1) / lens[idx].sum(axis=1))(
        gen.integers(0, k, size=(min(step, iters - i), k))) for i in range(0, iters, step)])
    lo, hi = np.quantile(means, [0.025, 0.975])
    return round(float(lo), 2), round(float(hi), 2)


def wilson_low(wins: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95 % Wilson interval of a win rate: shown next to the win rate so a 5/5 KOL reads as
    'somewhere above 57 %', not '100 %'. Information only; it does not change any status."""
    if n <= 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - margin) / den)


def status_of(n: int, ci) -> str:
    if n < MIN_N or ci is None:
        return "INCONCLUSIVE"
    return "PROVISIONAL" if ci[0] > 0 else "REJECT"


def _ranking(db, since: float, roster: dict) -> list[dict]:
    """Every KOL with >= 1 counted trade in the range, ranked by P&L (2 queries, whatever the KOL count)."""
    key = ("rank", since, db.execute("SELECT COUNT(*), MAX(id) FROM trades").fetchone())
    if key in _cache:
        return _cache[key]
    rows = db.execute(
        "SELECT kol, COUNT(*), SUM(pnl_sol), SUM(spend_sol), SUM(net_pct > 0), AVG(net_pct), MIN(entry_ts), "
        "MAX(exit_ts), COUNT(DISTINCT mint), MAX(net_pct), MIN(net_pct), SUM(net_pct) FROM trades "
        "WHERE gap = 0 AND exit_ts >= ? GROUP BY kol", (since,)).fetchall()
    now = time.time()
    recent = {k: (a, b) for k, a, b in db.execute(
        "SELECT kol, SUM(CASE WHEN exit_ts >= ? THEN pnl_sol ELSE 0 END), SUM(pnl_sol) FROM trades "
        "WHERE gap = 0 AND exit_ts >= ? GROUP BY kol", (now - 7 * 86400, now - 30 * 86400))}
    seen = {w: (f, src) for w, f, src in db.execute("SELECT wallet, first_seen_at, first_seen_source FROM kols")} \
        if _has(db, "kols") else {}
    seen_last = {w: l for w, l in db.execute("SELECT wallet, last_seen_at FROM kols")} if _has(db, "kols") else {}
    # per-KOL stats that need the trades themselves (median, streak, CI): recomputed only for KOLs whose trades
    # changed (key = n + sums), one IN-query for all of them
    ks_key = {r[0]: ("ks", r[0], r[1], round(r[2], 6), round(r[11], 4)) for r in rows}
    todo = [k for k, ck in ks_key.items() if ck not in _ci]
    if todo:
        from kolbot.kolhist import streaks
        per: dict[str, list[tuple]] = {}
        for i in range(0, len(todo), 900):
            part = todo[i:i + 900]
            for kol, p, x in db.execute(f"SELECT kol, pnl_sol, net_pct FROM trades WHERE gap = 0 AND exit_ts >= ? "
                                        f"AND kol IN ({','.join('?' * len(part))}) ORDER BY kol, exit_ts, id",
                                        (since, *part)):
                per.setdefault(kol, []).append((p, x))
        if len(_ci) > 50_000:
            _ci.clear()
        for k in todo:
            vals = per.get(k, [])
            pn = sorted(v[0] for v in vals)
            m = len(pn)
            _ci[ks_key[k]] = {
                "ci": boot_ci([v[1] for v in vals]) if m >= MIN_N else None,
                "median": (pn[m // 2] if m % 2 else (pn[m // 2 - 1] + pn[m // 2]) / 2) if m else None,
                "streak": streaks([v[1] > 0 for v in vals])}
    out = []
    for kol, n, pnl, spend, wins, avg_pct, first, last, tokens, best, worst, _s in rows:
        st = _ci.get(ks_key[kol]) or {}
        ci = st.get("ci") if n >= MIN_N else None
        sk = st.get("streak") or {}
        info = roster.get(kol, {})
        out.append({"kol": kol, "name": info.get("name") or kol[:6], "twitter": info.get("twitter") or "",
                    "n": n, "pnl_sol": round(pnl, 6), "spend_sol": round(spend, 6),
                    "roi_pct": round(100 * pnl / spend, 2) if spend else 0.0,
                    "win_rate_pct": round(100 * wins / n, 1), "avg_pnl_sol": round(pnl / n, 6),
                    "avg_pct": round(avg_pct, 2), "first_ts": first, "last_ts": last, "tokens": tokens,
                    "best_pct": round(best, 2), "worst_pct": round(worst, 2), "ci95": ci,
                    "status": status_of(n, ci), "wallet_url": solscan("wallet", kol),
                    "first_seen_at": seen.get(kol, (None, None))[0],
                    "first_seen_source": seen.get(kol, (None, None))[1],
                    "pnl_7d_sol": round(recent.get(kol, (0, 0))[0] or 0, 6),
                    "pnl_30d_sol": round(recent.get(kol, (0, 0))[1] or 0, 6),
                    "wins": wins, "losses": n - wins,
                    "win_rate_low_pct": round(100 * wilson_low(wins, n), 1),
                    "median_pnl_sol": round(st["median"], 6) if st.get("median") is not None else None,
                    "streak": sk.get("current", 0), "longest_win": sk.get("longest_win", 0),
                    "longest_loss": sk.get("longest_loss", 0),
                    "last_seen_at": max(x for x in (seen_last.get(kol), last) if x)})
    out.sort(key=lambda r: (-r["pnl_sol"], -r["n"], r["kol"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = out
    return out


CONTROL_NOTE = ("Bot paper chưa có nhóm Control (ví ngẫu nhiên). Thêm Control là thay đổi methodology, cần quyết "
                "định riêng; nhóm Control của hướng C nằm trong bài test đã đăng ký (sau 27/10).")


def _filter(rows: list[dict], min_n=1, pnl="", status="", min_wr=None, hi_wr=False, seen="") -> list[dict]:
    out = [r for r in rows if r["n"] >= max(1, int(min_n))]
    if pnl == "pos":
        out = [r for r in out if r["pnl_sol"] > 0]
    elif pnl == "neg":
        out = [r for r in out if r["pnl_sol"] < 0]
    if status in STATUSES:
        out = [r for r in out if r["status"] == status]
    if min_wr not in (None, ""):
        out = [r for r in out if r["win_rate_pct"] >= float(min_wr)]
    if hi_wr:
        out = [r for r in out if r["win_rate_pct"] >= HIGH_WIN_RATE_PCT]
    if seen and seen != "all":                          # KOLs first seen inside this window
        cut = since_of(seen)
        out = [r for r in out if r.get("first_seen_at") and r["first_seen_at"] >= cut]
    return out


def kol_table(db, roster: dict, rng: str = "all", min_n: int = 1, pnl: str = "", status: str = "",
              sort: str = "pnl", direction: str = "desc", page: int = 1, size: int = 25, group: str = "kol",
              min_wr=None, hi_wr: bool = False, seen: str = "") -> dict:
    if group == "control":
        return {"group": "control", "total": 0, "page": 1, "pages": 0, "rows": [], "note": CONTROL_NOTE}
    ranked = _filter(_ranking(db, since_of(rng), roster), min_n, pnl, status, min_wr, hi_wr, seen)
    k = SORTS.get(sort, "pnl_sol")
    rows = sorted(ranked, key=lambda r: (r[k] if r[k] is not None else float("-inf"), -r["rank"]),
                  reverse=direction != "asc")
    size = max(5, min(100, int(size)))
    pages = max(1, -(-len(rows) // size))
    page = max(1, min(int(page), pages))
    return {"group": "kol", "total": len(rows), "page": page, "pages": pages, "size": size,
            "rows": rows[(page - 1) * size: page * size]}


WINRATE_DEFAULT_MIN_N = 100


def winrate_table(db, roster: dict, rng: str = "all", min_n: int = WINRATE_DEFAULT_MIN_N, pnl: str = "",
                  status: str = "", min_wr=None, hi_wr: bool = False, seen: str = "", page: int = 1,
                  size: int = 25, group: str = "kol") -> dict:
    """Top KOL by win rate (research view, never a buy signal). Win rate = wins / resolved trades, where a
    resolved trade is a closed paper trade not excluded for a stream gap and a win is net P&L > 0 after costs.
    Order: win rate desc, then resolved trades desc, then wins desc. P&L is shown, never used to rank. The
    sample filter (default >= 100 resolved trades) keeps 5/5 KOLs from sitting above 200/300 ones."""
    if group == "control":
        return {"group": "control", "total": 0, "page": 1, "pages": 0, "rows": [], "note": CONTROL_NOTE}
    rows = _filter(_ranking(db, since_of(rng), roster), min_n, pnl, status, min_wr, hi_wr, seen)
    rows = sorted(rows, key=lambda r: (-(r["wins"] / r["n"]), -r["n"], -r["wins"], r["kol"]))
    out = [dict(r, wr_rank=i) for i, r in enumerate(rows, 1)]
    page, pages, size = _page(len(out), page, size)
    return {"group": "kol", "total": len(out), "page": page, "pages": pages, "size": size, "min_n": int(min_n),
            "rows": out[(page - 1) * size: page * size]}


def _downsample(points: list, max_points: int = 300) -> list:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    out = [points[int(i * step)] for i in range(max_points)]
    return out + [points[-1]]


def _series(db, where: str, args: tuple) -> list:
    cum, pts = 0.0, []
    for ts, p in db.execute(f"SELECT exit_ts, pnl_sol FROM trades WHERE {where} ORDER BY exit_ts, id", args):
        cum += p
        pts.append((round(ts), round(cum, 6)))
    return _downsample(pts)


def summary(db, roster: dict, rng: str = "all", min_n: int = 1) -> dict:
    since = since_of(rng)
    skey = ("summary", rng, since, int(min_n), db.execute("SELECT COUNT(*), MAX(id) FROM trades").fetchone())
    if skey in _cache:
        return _cache[skey]
    out = _summary(db, roster, rng, since, min_n)
    if len(_cache) > 256:
        _cache.clear()
    _cache[skey] = out
    return out


def _summary(db, roster: dict, rng: str, since: float, min_n: int) -> dict:
    tot = db.execute("SELECT COUNT(*), COALESCE(SUM(pnl_sol),0), COALESCE(SUM(spend_sol),0), "
                     "COALESCE(SUM(net_pct > 0),0), MIN(entry_ts), MAX(exit_ts), COUNT(DISTINCT kol) "
                     "FROM trades WHERE gap = 0 AND exit_ts >= ?", (since,)).fetchone()
    n, pnl, spend, wins, first, last, kols = tot
    excluded = db.execute("SELECT COUNT(*) FROM trades WHERE gap = 1 AND exit_ts >= ?", (since,)).fetchone()[0]
    ckey = ("sum", since, n, round(pnl, 6))
    if ckey not in _cache:
        groups: dict[str, list[float]] = {}
        for kol, x in db.execute("SELECT kol, net_pct FROM trades WHERE gap = 0 AND exit_ts >= ?", (since,)):
            groups.setdefault(kol, []).append(x)
        _cache[ckey] = cluster_ci(list(groups.values()))
    ci = _cache[ckey]
    span = (last - first) if n else 0
    bucket = 3600 if span <= 3 * 86400 else 86400
    over_time = [{"t": b * bucket, "n": c, "wins": w} for b, c, w in db.execute(
        "SELECT CAST(exit_ts / ? AS INT) b, COUNT(*), SUM(net_pct > 0) FROM trades WHERE gap = 0 AND exit_ts >= ? "
        "GROUP BY b ORDER BY b", (bucket, since))]
    hist = [{"lo": -100 + 10 * b, "hi": -90 + 10 * b, "n": c} for b, c in db.execute(
        "SELECT CAST((MIN(MAX(net_pct, -100), 299.99) + 100) / 10 AS INT) b, COUNT(*) FROM trades "
        "WHERE gap = 0 AND exit_ts >= ? GROUP BY b ORDER BY b", (since,))]
    ranked = [r for r in _ranking(db, since, roster) if r["n"] >= max(1, int(min_n))]
    all_ranked = [r for r in _ranking(db, 0.0, roster) if r["n"] >= max(1, int(min_n))]   # since first seen
    return {"range": rng, "n": n, "excluded_gap": excluded, "kols_active": kols, "roster": len(roster),
            "pnl_sol": round(pnl, 6), "spend_sol": round(spend, 6), "roi_pct": round(100 * pnl / spend, 2) if spend
            else None, "win_rate_pct": round(100 * wins / n, 1) if n else None, "wins": wins, "losses": n - wins,
            "first_ts": first, "last_ts": last, "ci95_by_kol": ci, "status": status_of(n, ci), "min_n_required": MIN_N,
            "cumulative": _series(db, "gap = 0 AND exit_ts >= ?", (since,)), "histogram": hist,
            "over_time": over_time, "bucket_s": bucket,
            "top": ranked[:3], "losers": sorted([r for r in ranked if r["pnl_sol"] < 0],
                                                key=lambda r: r["pnl_sol"])[:3],
            "top_all": all_ranked[:5],
            "losers_all": sorted([r for r in all_ranked if r["pnl_sol"] < 0], key=lambda r: r["pnl_sol"])[:5]}


def kol_detail(db, roster: dict, kol: str, rng: str = "all", page: int = 1, size: int = 25,
               symbols: dict | None = None) -> dict | None:
    since = since_of(rng)
    ranked = _ranking(db, since, roster)
    me = next((r for r in ranked if r["kol"] == kol), None)
    info = roster.get(kol)
    if me is None and info is None:
        return None
    total = db.execute("SELECT COUNT(*) FROM trades WHERE kol = ? AND exit_ts >= ?", (kol, since)).fetchone()[0]
    size = max(5, min(100, int(size)))
    pages = max(1, -(-total // size))
    page = max(1, min(int(page), pages))
    symbols = symbols or {}
    hist = []
    for r in db.execute("SELECT id, mint, kol_sol, trigger_ts, entry_ts, entry_how, exit_ts, exit_kind, spend_sol, "
                        "proceeds_sol, pnl_sol, net_pct, gap FROM trades WHERE kol = ? AND exit_ts >= ? "
                        "ORDER BY exit_ts DESC, id DESC LIMIT ? OFFSET ?", (kol, since, size, (page - 1) * size)):
        (tid, mint, kol_sol, trig, entry, how, exit_, kind, spend, proceeds, pnl, pct, gap) = r
        hist.append({"id": tid, "mint": mint, "symbol": symbols.get(mint), "token_url": f"https://pump.fun/coin/{mint}",
                     "solscan_url": solscan("token", mint), "kol_sol": kol_sol, "trigger_ts": trig, "entry_ts": entry,
                     "exit_ts": exit_, "buy_sol": spend, "sell_sol": proceeds, "pnl_sol": pnl, "roi_pct": pct,
                     "delay_s": round(entry - trig, 1) if trig else None, "entry_how": how, "exit_kind": kind,
                     "gap": bool(gap)})
    base = me or {"kol": kol, "name": info.get("name") or kol[:6], "twitter": info.get("twitter") or "", "n": 0,
                  "rank": None, "status": "INCONCLUSIVE", "wallet_url": solscan("wallet", kol)}
    if hist:
        devmap = _token_devs(db, list({h["mint"] for h in hist}))
        for h in hist:
            h["dev"] = devmap.get(h["mint"])
            h["symbol"] = h["symbol"] or (h["dev"] or {}).get("symbol")
    by_token = [{"mint": m, "symbol": symbols.get(m) or sym, "trades": c, "pnl_sol": p,
                 "roi_pct": 100 * p / sp if sp else None, "wins": w, "token_url": f"https://pump.fun/coin/{m}"}
                for m, sym, c, p, sp, w in db.execute(
        f"SELECT t.mint, {'k.symbol' if _has(db, 'tokens') else 'NULL'}, COUNT(*), SUM(t.pnl_sol), SUM(t.spend_sol), "
        f"SUM(t.net_pct > 0) FROM trades t {'LEFT JOIN tokens k ON k.mint = t.mint' if _has(db, 'tokens') else ''} "
        "WHERE t.kol = ? AND t.gap = 0 AND t.exit_ts >= ? GROUP BY t.mint ORDER BY SUM(t.pnl_sol) DESC LIMIT 100",
        (kol, since))]
    if by_token and _has(db, "tokens"):                   # token outcome + dev, 2 queries for the whole page
        from kolbot.devs import COLS, classify
        mints = [t["mint"] for t in by_token]
        now = time.time()
        recs = {r[0]: dict(zip(COLS, r)) for r in db.execute(
            f"SELECT {','.join(COLS)} FROM tokens WHERE mint IN ({','.join('?' * len(mints))})", mints)}
        devmap = _token_devs(db, mints)
        for t in by_token:
            c = classify(recs[t["mint"]], now) if t["mint"] in recs else None
            t["outcome"] = c["outcome"] if c else None
            t["rug"] = c["rug"] if c else None
            t["dev"] = devmap.get(t["mint"])
    from kolbot.kolhist import kol_history
    since_first = kol_history(db, kol) if _has(db, "kols") else None
    return dict(base, ranked_of=len(ranked), in_roster=info is not None, by_token=by_token, since_first=since_first,
                cumulative=_series(db, "kol = ? AND gap = 0 AND exit_ts >= ?", (kol, since)),
                history={"total": total, "page": page, "pages": pages, "size": size, "rows": hist})


def export_csv(db) -> bytes:
    """All closed paper trades (read-only), to keep a copy before a redeploy wipes the free-plan disk."""
    cur = db.execute("SELECT * FROM trades ORDER BY id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in cur.description])
    w.writerows(cur)
    return buf.getvalue().encode()


def _has(db, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def _token_devs(db, mints: list[str]) -> dict:
    """mint -> creator + KNOWN/NEW DEV + risk + history counts, in one query."""
    if not mints or not _has(db, "tokens") or not _has(db, "devs"):
        return {}
    out = {}
    for mint, sym, creator, created, risk, reason, known, mig, failed, rug, chk, med, last_mint in db.execute(
            f"SELECT k.mint, k.symbol, k.creator, d.created, d.risk, d.risk_reason, d.known, d.migrated, d.failed, "
            f"d.rug, d.rug_checkable, d.median_outcome_pct, d.latest_mint FROM tokens k LEFT JOIN devs d ON "
            f"d.wallet = k.creator WHERE k.mint IN ({','.join('?' * len(mints))})", mints):
        prev = max((created or 1) - 1, 0)
        out[mint] = {"symbol": sym, "creator": creator, "creator_url": solscan("wallet", creator) if creator else None,
                     "known_dev": prev > 0, "label": "KNOWN DEV" if prev > 0 else "NEW DEV", "previous_tokens": prev,
                     "risk": risk or "UNKNOWN", "risk_reason": reason, "known": known, "migrated": mig,
                     "failed": failed, "rug": rug, "rug_checkable": chk, "median_outcome_pct": med,
                     "latest_mint": last_mint}
    return out


def _page(total: int, page, size) -> tuple[int, int, int]:
    size = max(5, min(100, int(size)))
    pages = max(1, -(-total // size))
    return max(1, min(int(page), pages)), pages, size


DEV_SORTS = {"rug": "rug_rate", "fail": "fail_rate", "median": "median_outcome_pct", "tokens": "created",
             "success": "success_rate", "pnl": "paper_pnl_sol", "recent": "last_token_ts"}
DEV_LIST_COLS = ("wallet", "created", "observed", "known", "migrated", "failed", "dead", "pending", "rug",
                 "rug_checkable", "success_rate", "fail_rate", "rug_rate", "median_outcome_pct", "outcome_n",
                 "paper_pnl_sol", "paper_trades", "risk", "risk_reason", "last_token_ts", "latest_mint",
                 "seen_only", "unknown_age", "distinct_names", "max_same_name", "top_name")


def dev_table(db, min_tokens: int = 2, risk: str = "", sort: str = "tokens", direction: str = "desc",
              page: int = 1, size: int = 25, q: str = "") -> dict:
    if not _has(db, "devs"):
        return {"total": 0, "page": 1, "pages": 1, "size": size, "rows": []}
    where, args = ["created >= ?"], [max(1, int(min_tokens))]
    if risk:
        where.append("risk = ?")
        args.append(risk)
    if q:
        where.append("wallet LIKE ?")
        args.append(q.strip() + "%")
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM devs WHERE {w}", args).fetchone()[0]
    page, pages, size = _page(total, page, size)
    col = DEV_SORTS.get(sort, "created")
    order = "ASC" if direction == "asc" else "DESC"
    rows = []
    for i, r in enumerate(db.execute(
            f"SELECT {','.join(DEV_LIST_COLS)} FROM devs WHERE {w} ORDER BY {col} IS NULL, {col} {order}, "
            f"created DESC, wallet LIMIT ? OFFSET ?", (*args, size, (page - 1) * size)), (page - 1) * size + 1):
        d = dict(zip(DEV_LIST_COLS, r))
        d.update(rank=i, wallet_url=solscan("wallet", d["wallet"]))
        rows.append(d)
    return {"total": total, "page": page, "pages": pages, "size": size, "rows": rows}


def dev_detail(db, wallet: str, page: int = 1, size: int = 25) -> dict | None:
    from kolbot import devs as D
    if not _has(db, "devs"):
        return None
    prof = D.get_dev_profile(db, wallet)
    toks = D.get_dev_tokens(db, wallet, page, size)
    if prof is None and not toks["total"]:
        return None
    for t in toks["rows"]:
        t["token_url"] = f"https://pump.fun/coin/{t['mint']}"
        t["solscan_url"] = solscan("token", t["mint"])
    return {"wallet": wallet, "wallet_url": solscan("wallet", wallet), "profile": prof,
            "risk": D.get_dev_risk(db, wallet), "tokens": toks,
            "links": [dict(zip(("related", "relation", "source", "evidence", "ts"), r)) for r in db.execute(
                "SELECT related, relation, source, evidence, ts FROM wallet_links WHERE wallet=? LIMIT 50", (wallet,))]}


def token_table(db, scope: str = "kol", page: int = 1, size: int = 25, q: str = "") -> dict:
    from kolbot.devs import COLS, classify
    if not _has(db, "tokens"):
        return {"total": 0, "page": 1, "pages": 1, "size": size, "rows": []}
    where, args = [], []
    if scope == "kol":
        where.append("(k.kol_buys > 0 OR k.mint IN (SELECT mint FROM trades))")
    if q:
        where.append("(k.mint LIKE ? OR k.creator LIKE ? OR k.symbol LIKE ?)")
        args += [q.strip() + "%"] * 3
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM tokens k {w}", args).fetchone()[0]
    page, pages, size = _page(total, page, size)
    now = time.time()
    rows = []
    for r in db.execute(f"SELECT {','.join('k.' + c for c in COLS)} FROM tokens k {w} ORDER BY "
                        f"COALESCE(k.created_ts, k.first_seen_ts) DESC LIMIT ? OFFSET ?",
                        (*args, size, (page - 1) * size)):
        t = dict(zip(COLS, r))
        t.update(classify(t, now), token_url=f"https://pump.fun/coin/{t['mint']}",
                 solscan_url=solscan("token", t["mint"]))
        rows.append(t)
    mints = [t["mint"] for t in rows]
    devs = _token_devs(db, mints)
    pnl = {m: (n, p) for m, n, p in db.execute(
        f"SELECT mint, COUNT(*), SUM(pnl_sol) FROM trades WHERE gap=0 AND mint IN ({','.join('?' * len(mints))}) "
        "GROUP BY mint", mints)} if mints else {}
    for t in rows:
        t["dev"] = devs.get(t["mint"])
        t["paper_trades"], t["paper_pnl_sol"] = pnl.get(t["mint"], (0, None))
    return {"total": total, "page": page, "pages": pages, "size": size, "rows": rows, "scope": scope}


def token_detail(db, mint: str, roster: dict) -> dict | None:
    from kolbot.devs import COLS, classify
    r = db.execute(f"SELECT {','.join(COLS)} FROM tokens WHERE mint=?", (mint,)).fetchone() \
        if _has(db, "tokens") else None
    tcols = ("id", "kol", "entry_ts", "exit_ts", "exit_kind", "spend_sol", "proceeds_sol", "pnl_sol", "net_pct",
             "trigger_ts", "gap")
    trades = [dict(zip(tcols, x)) for x in db.execute(
        f"SELECT {','.join(tcols)} FROM trades WHERE mint=? ORDER BY exit_ts DESC LIMIT 200", (mint,))]
    if r is None and not trades:
        return None
    t = dict(zip(COLS, r)) if r else {"mint": mint}
    if r:
        t.update(classify(t, time.time()))
    for x in trades:
        x["kol_name"] = (roster.get(x["kol"]) or {}).get("name") or x["kol"][:6]
        x["delay_s"] = round(x["entry_ts"] - x["trigger_ts"], 1) if x["trigger_ts"] else None
    return dict(t, token_url=f"https://pump.fun/coin/{mint}", solscan_url=solscan("token", mint),
                dev=_token_devs(db, [mint]).get(mint), paper_trades=trades)


def trade_table(db, roster: dict, rng: str = "all", kol: str = "", exit_kind: str = "", pnl: str = "",
                page: int = 1, size: int = 25) -> dict:
    where, args = ["t.exit_ts >= ?"], [since_of(rng)]
    if kol:
        where.append("t.kol = ?")
        args.append(kol)
    if exit_kind:
        where.append("t.exit_kind = ?")
        args.append(exit_kind)
    if pnl == "pos":
        where.append("t.pnl_sol > 0")
    elif pnl == "neg":
        where.append("t.pnl_sol < 0")
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM trades t WHERE {w}", args).fetchone()[0]
    page, pages, size = _page(total, page, size)
    tok = _has(db, "tokens")
    cols = ("id", "mint", "kol", "trigger_ts", "entry_ts", "exit_ts", "exit_kind", "spend_sol", "proceeds_sol",
            "pnl_sol", "net_pct", "gap", "uid")
    rows = []
    for r in db.execute(f"SELECT {','.join('t.' + c for c in cols)}, {'k.symbol' if tok else 'NULL'} FROM trades t "
                        f"{'LEFT JOIN tokens k ON k.mint = t.mint' if tok else ''} WHERE {w} "
                        f"ORDER BY t.exit_ts DESC, t.id DESC LIMIT ? OFFSET ?", (*args, size, (page - 1) * size)):
        x = dict(zip(cols + ("symbol",), r))
        x.update(kol_name=(roster.get(x["kol"]) or {}).get("name") or x["kol"][:6],
                 delay_s=round(x["entry_ts"] - x["trigger_ts"], 1) if x["trigger_ts"] else None,
                 token_url=f"https://pump.fun/coin/{x['mint']}", gap=bool(x["gap"]))
        rows.append(x)
    devs = _token_devs(db, list({x["mint"] for x in rows}))
    for x in rows:
        x["dev"] = devs.get(x["mint"])
    return {"total": total, "page": page, "pages": pages, "size": size, "rows": rows}
