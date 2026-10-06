"""Read-only queries for the Smart Wallet tab (see smart.py for definitions). Ranking happens in SQLite on the
incremental per-wallet table; only one page of wallets ever reaches Python or the browser."""
from __future__ import annotations

import statistics
import time

from kolbot.api import MIN_N, _downsample, _has, _page, solscan, wilson_low

DEFAULT_MIN_N = 100
TOPS = {"10": 10, "50": 50}
# evidence-based "unreliable" flags (thresholds shown with each flag; nothing is called manipulation or rug)
FLAG_SQL = {
    "insufficient": "(w.unresolved >= 5 AND w.unresolved * 1.0 / (w.n + w.unresolved) > 0.5)",
    "self_trading": "(w.n >= 5 AND w.self_trades * 1.0 / w.n >= 0.3)",
    "abnormal": "(w.n >= 50 AND w.hold_sum / w.n < 10)",
}
STATUS_SQL = {
    "INCONCLUSIVE": f"(w.n < {MIN_N} OR w.ci_lo IS NULL)",
    "PROVISIONAL": f"(w.n >= {MIN_N} AND w.ci_lo > 0)",
    "REJECT": f"(w.n >= {MIN_N} AND w.ci_lo <= 0)",
    "PASS": "(0)",                                      # never: there is no control group
}
COLS = ("wallet", "first_seen", "last_seen", "n", "wins", "pnl_sol", "sol_in", "roi_sum", "best_pct", "worst_pct",
        "cur_streak", "long_win", "long_loss", "tokens", "unresolved", "gap_excluded", "self_trades", "hold_sum",
        "ci_lo", "ci_hi", "ci_n")


def flags_of(w: dict) -> list[dict]:
    n, out = w["n"], []
    if n < 30:
        out.append({"code": "low_sample", "label": "Chỉ có vài giao dịch", "evidence": f"{n} giao dịch (< 30)"})
    u = w["unresolved"]
    if u >= 5 and u / (n + u) > 0.5:
        out.append({"code": "insufficient", "label": "Dữ liệu không đủ",
                    "evidence": f"{u}/{n + u} vị thế chưa xác định được kết quả (token đã migrate)"})
    if n >= 5 and w["self_trades"] / n >= 0.3:
        out.append({"code": "self_trading", "label": "Tự trade token mình tạo",
                    "evidence": f"{w['self_trades']}/{n} giao dịch trên token do chính ví này tạo"})
    if n >= 50 and w["hold_sum"] / n < 10:
        out.append({"code": "abnormal", "label": "Hoạt động bất thường (bot tần suất cao)",
                    "evidence": f"thời gian giữ trung bình {w['hold_sum'] / n:.1f}s trên {n} giao dịch"})
    return out


def status_of(w: dict) -> str:
    if w["n"] < MIN_N or w["ci_lo"] is None:
        return "INCONCLUSIVE"
    return "PROVISIONAL" if w["ci_lo"] > 0 else "REJECT"


def _row(r: tuple) -> dict:
    w = dict(zip(COLS, r))
    n = w["n"] or 0
    w.update(losses=n - w["wins"], win_rate_pct=round(100 * w["wins"] / n, 1) if n else None,
             win_rate_low_pct=round(100 * wilson_low(w["wins"], n), 1),
             roi_pct=round(100 * w["pnl_sol"] / w["sol_in"], 2) if w["sol_in"] else None,
             avg_pnl_sol=w["pnl_sol"] / n if n else None, avg_roi_pct=w["roi_sum"] / n if n else None,
             avg_hold_s=w["hold_sum"] / n if n else None, status=status_of(w), flags=flags_of(w),
             wallet_url=solscan("wallet", w["wallet"]),
             ci95=[w["ci_lo"], w["ci_hi"]] if w["ci_lo"] is not None else None)
    return w


def _where(min_n: int, status: str, pnl: str, hide_flagged: bool) -> tuple[str, list]:
    where = ["w.n >= ?", "w.wallet NOT IN (SELECT wallet FROM kol_roster)"]
    args: list = [max(1, int(min_n))]
    if status in STATUS_SQL:
        where.append(STATUS_SQL[status])
    if pnl == "pos":
        where.append("w.pnl_sol > 0")
    elif pnl == "neg":
        where.append("w.pnl_sol < 0")
    if hide_flagged:
        where += [f"NOT {s}" for s in FLAG_SQL.values()]
    return " AND ".join(where), args


ORDER = "w.wins * 1.0 / w.n DESC, w.n DESC, w.wins DESC, w.wallet"


def smart_table(db, min_n: int = DEFAULT_MIN_N, top: str = "", status: str = "", pnl: str = "",
                hide_flagged: bool = True, page: int = 1, size: int = 25) -> dict:
    """Ranking: enough sample first (min_n filter, default 100 resolved trades), then win rate desc, resolved
    trades desc, wins desc. P&L / ROI / CI are shown, not used to rank."""
    if not _has(db, "sw_wallets"):
        return {"total": 0, "page": 1, "pages": 1, "size": size, "rows": [], "eligible": 0}
    where, args = _where(min_n, status, pnl, hide_flagged)
    eligible = db.execute(f"SELECT COUNT(*) FROM sw_wallets w WHERE {where}", args).fetchone()[0]
    total = min(eligible, TOPS[top]) if top in TOPS else eligible
    page, pages, size = _page(total, page, size)
    off = (page - 1) * size
    lim = max(0, min(size, total - off))
    rows = [_row(r) for r in db.execute(f"SELECT {','.join('w.' + c for c in COLS)} FROM sw_wallets w WHERE {where} "
                                        f"ORDER BY {ORDER} LIMIT ? OFFSET ?", (*args, lim, off))]
    for i, r in enumerate(rows, off + 1):
        r["rank"] = i
    _medians(db, rows)
    return {"total": total, "eligible": eligible, "page": page, "pages": pages, "size": size, "rows": rows,
            "top": top, "min_n": int(min_n)}


def _medians(db, rows: list[dict]) -> None:
    """Median P&L per trade for one page of wallets: one query."""
    if not rows:
        return
    ws = [r["wallet"] for r in rows]
    per: dict[str, list[float]] = {}
    for w, p in db.execute(f"SELECT wallet, pnl_sol FROM sw_trades WHERE gap = 0 AND kind <> 'migrated' AND wallet "
                           f"IN ({','.join('?' * len(ws))})", ws):
        per.setdefault(w, []).append(p)
    for r in rows:
        v = per.get(r["wallet"])
        r["median_pnl_sol"] = statistics.median(v) if v else None


def smart_summary(db, min_n: int = DEFAULT_MIN_N) -> dict:
    if not _has(db, "sw_wallets"):
        return {"wallets": 0}
    tracked, resolved = db.execute("SELECT COUNT(*), COALESCE(SUM(n), 0) FROM sw_wallets w WHERE w.wallet NOT IN "
                                   "(SELECT wallet FROM kol_roster)").fetchone()
    kinds = dict(db.execute("SELECT kind, COUNT(*) FROM sw_trades GROUP BY kind").fetchall())
    gaps = db.execute("SELECT COUNT(*), COALESCE(SUM(end - start), 0) FROM sw_gaps").fetchone()
    top = smart_table(db, min_n=min_n, top="10", size=10)
    return {"wallets": tracked, "resolved_trades": resolved, "by_kind": kinds,
            "open_positions": db.execute("SELECT COUNT(*) FROM sw_open").fetchone()[0],
            "gaps": gaps[0], "gap_seconds": round(gaps[1]), "eligible": top["eligible"], "min_n": int(min_n),
            "top3": top["rows"][:3],
            "first_ts": db.execute("SELECT MIN(open_ts) FROM sw_trades").fetchone()[0],
            "last_ts": db.execute("SELECT MAX(close_ts) FROM sw_trades").fetchone()[0], "now": time.time()}


def smart_detail(db, wallet: str, page: int = 1, size: int = 25) -> dict | None:
    if not _has(db, "sw_wallets"):
        return None
    r = db.execute(f"SELECT {','.join('w.' + c for c in COLS)} FROM sw_wallets w WHERE w.wallet = ?",
                   (wallet,)).fetchone()
    if not r:
        return None
    w = _row(r)
    w["is_kol"] = bool(db.execute("SELECT 1 FROM kol_roster WHERE wallet=?", (wallet,)).fetchone())
    w["rank_all"] = None
    eligible = not w["is_kol"] and w["n"] >= DEFAULT_MIN_N and not any(
        f["code"] in FLAG_SQL for f in w["flags"])
    if eligible:                                        # same rule as the default table: >= 100, flagged hidden
        where, args = _where(DEFAULT_MIN_N, "", "", True)
        better = db.execute(f"SELECT COUNT(*) FROM sw_wallets w WHERE {where} AND (w.wins * 1.0 / w.n > ? OR "
                            f"(w.wins * 1.0 / w.n = ? AND (w.n > ? OR (w.n = ? AND (w.wins > ? OR (w.wins = ? "
                            f"AND w.wallet < ?))))))",
                            (*args, w["wins"] / w["n"], w["wins"] / w["n"], w["n"], w["n"], w["wins"], w["wins"],
                             wallet)).fetchone()[0]
        w["rank_all"] = better + 1
    _medians(db, [w])
    base = "wallet = ? AND gap = 0 AND kind <> 'migrated'"
    total = db.execute("SELECT COUNT(*) FROM sw_trades WHERE wallet = ?", (wallet,)).fetchone()[0]
    page, pages, size = _page(total, page, size)
    tok = _has(db, "tokens")
    hist = [dict(zip(("mint", "symbol", "open_ts", "close_ts", "sol_in", "sol_out", "mark_sol", "pnl_sol", "roi_pct",
                      "kind", "hold_s", "buys", "sells", "self_token", "gap"), x)) for x in db.execute(
        f"SELECT t.mint, {'k.symbol' if tok else 'NULL'}, t.open_ts, t.close_ts, t.sol_in, t.sol_out, t.mark_sol, "
        f"t.pnl_sol, t.roi_pct, t.kind, t.hold_s, t.buys, t.sells, t.self_token, t.gap FROM sw_trades t "
        f"{'LEFT JOIN tokens k ON k.mint = t.mint' if tok else ''} WHERE t.wallet = ? "
        f"ORDER BY t.close_ts DESC, t.id DESC LIMIT ? OFFSET ?", (wallet, size, (page - 1) * size))]
    for h in hist:
        h["token_url"] = f"https://pump.fun/coin/{h['mint']}"
    daily, cw, cn, cp, cs = [], 0, 0, 0.0, 0.0
    for d, n, wins, pnl, sin in db.execute(
            f"SELECT date(close_ts, 'unixepoch'), COUNT(*), SUM(pnl_sol > 0), SUM(pnl_sol), SUM(sol_in) FROM sw_trades "
            f"WHERE {base} GROUP BY 1 ORDER BY 1", (wallet,)):
        cw, cn, cp, cs = cw + wins, cn + n, cp + pnl, cs + sin
        daily.append({"date": d, "trades": n, "wins": wins, "losses": n - wins, "pnl_sol": pnl,
                      "roi_pct": 100 * pnl / sin if sin else None, "win_rate_pct": round(100 * wins / n, 1),
                      "cum_trades": cn, "cum_pnl_sol": cp, "cum_roi_pct": 100 * cp / cs if cs else None,
                      "cum_win_rate_pct": round(100 * cw / cn, 1)})
    cum, pts = 0.0, []
    for ts, p in db.execute(f"SELECT close_ts, pnl_sol FROM sw_trades WHERE {base} ORDER BY close_ts, id", (wallet,)):
        cum += p
        pts.append((round(ts), round(cum, 6)))
    tokens = [dict(zip(("mint", "symbol", "trades", "wins", "pnl_sol", "sol_in"), x)) for x in db.execute(
        f"SELECT t.mint, {'k.symbol' if tok else 'NULL'}, COUNT(*), SUM(t.pnl_sol > 0), SUM(t.pnl_sol), "
        f"SUM(t.sol_in) FROM sw_trades t {'LEFT JOIN tokens k ON k.mint = t.mint' if tok else ''} "
        f"WHERE t.wallet = ? AND t.gap = 0 AND t.kind <> 'migrated' GROUP BY t.mint ORDER BY SUM(t.pnl_sol) DESC "
        f"LIMIT 100", (wallet,))]
    if tokens and tok:
        from kolbot.devs import COLS as TCOLS, classify
        mints = [t["mint"] for t in tokens]
        recs = {x[0]: dict(zip(TCOLS, x)) for x in db.execute(
            f"SELECT {','.join(TCOLS)} FROM tokens WHERE mint IN ({','.join('?' * len(mints))})", mints)}
        now = time.time()
        for t in tokens:
            c = classify(recs[t["mint"]], now) if t["mint"] in recs else None
            t.update(outcome=c["outcome"] if c else None, rug=c["rug"] if c else None,
                     roi_pct=100 * t["pnl_sol"] / t["sol_in"] if t["sol_in"] else None,
                     token_url=f"https://pump.fun/coin/{t['mint']}")
    return dict(w, history={"total": total, "page": page, "pages": pages, "size": size, "rows": hist},
                daily=daily[::-1][:400], cumulative=_downsample(pts), tokens_traded=tokens)
