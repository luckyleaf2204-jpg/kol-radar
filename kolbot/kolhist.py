"""Persistent KOL identity and performance history. The wallet is the key; the display name is stored beside it
and may change. first_seen_at is the first time the stream saw the KOL trade (or, for KOLs already in the ledger
before this table existed, its first copied trade), and only ever moves earlier: a KOL that disappears and comes
back keeps it. Daily snapshots are derived from the trade ledger only (never from UI numbers) and can be rebuilt
at any time with the same result."""
from __future__ import annotations

import statistics
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kols (wallet TEXT PRIMARY KEY, display_name TEXT, twitter TEXT, first_seen_at REAL,
    last_seen_at REAL, first_seen_source TEXT, name_updated_at REAL);
CREATE TABLE IF NOT EXISTS kol_daily (date TEXT, wallet TEXT, trades INTEGER, wins INTEGER, losses INTEGER,
    pnl_sol REAL, spend_sol REAL, roi_pct REAL, win_rate_pct REAL, cum_trades INTEGER, cum_wins INTEGER,
    cum_pnl_sol REAL, cum_spend_sol REAL, cum_roi_pct REAL, PRIMARY KEY (date, wallet));
CREATE INDEX IF NOT EXISTS ix_kol_daily_wallet ON kol_daily(wallet, date);
"""
DAILY_EVERY_S = 300


class KolHistory:
    def __init__(self, db, roster: dict, clock=time.time):
        self.db, self.clock = db, clock
        db.executescript(SCHEMA)
        self.seen: dict[str, list[float]] = {}
        self.last_daily = 0.0
        now = clock()
        for w, info in roster.items():                     # names can change; identity cannot
            db.execute("INSERT INTO kols (wallet, display_name, twitter, name_updated_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(wallet) DO UPDATE SET display_name=excluded.display_name, "
                       "twitter=excluded.twitter, name_updated_at=CASE WHEN kols.display_name IS NOT "
                       "excluded.display_name THEN excluded.name_updated_at ELSE kols.name_updated_at END",
                       (w, info.get("name"), info.get("twitter"), now))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone():
            # KOLs already in the ledger before this table existed: first copied trigger, marked as such
            db.execute("UPDATE kols SET first_seen_at = (SELECT MIN(trigger_ts) FROM trades WHERE trades.kol = "
                       "kols.wallet), first_seen_source = 'ledger' WHERE first_seen_at IS NULL AND EXISTS "
                       "(SELECT 1 FROM trades WHERE trades.kol = kols.wallet)")
        db.commit()

    def on_kol_event(self, wallet: str, ts: float) -> None:
        s = self.seen.get(wallet)
        if s is None:
            self.seen[wallet] = [ts, ts]
        else:
            s[0], s[1] = min(s[0], ts), max(s[1], ts)

    def tick(self, force: bool = False) -> None:
        if self.seen:
            self.db.executemany(
                "UPDATE kols SET first_seen_at = CASE WHEN first_seen_at IS NULL OR ? < first_seen_at THEN ? "
                "ELSE first_seen_at END, first_seen_source = COALESCE(first_seen_source, 'stream'), "
                "last_seen_at = MAX(COALESCE(last_seen_at, 0), ?) WHERE wallet = ?",
                [(a, a, b, w) for w, (a, b) in self.seen.items()])
            self.seen.clear()
            self.db.commit()
        now = self.clock()
        if force or now - self.last_daily >= DAILY_EVERY_S:
            self.last_daily = now
            rebuild_daily(self.db)


def rebuild_daily(db) -> int:
    """kol_daily from the ledger (UTC days, gap-flagged trades excluded): one statement, idempotent."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone():
        return 0
    db.execute("DELETE FROM kol_daily")
    db.execute("""
        INSERT INTO kol_daily
        SELECT date, wallet, trades, wins, trades - wins, pnl, spend,
               CASE WHEN spend > 0 THEN 100.0 * pnl / spend END, 100.0 * wins / trades,
               SUM(trades) OVER w, SUM(wins) OVER w, SUM(pnl) OVER w, SUM(spend) OVER w,
               CASE WHEN SUM(spend) OVER w > 0 THEN 100.0 * SUM(pnl) OVER w / SUM(spend) OVER w END
        FROM (SELECT date(exit_ts, 'unixepoch') AS date, kol AS wallet, COUNT(*) AS trades,
                     SUM(net_pct > 0) AS wins, SUM(pnl_sol) AS pnl, SUM(spend_sol) AS spend
              FROM trades WHERE gap = 0 GROUP BY 1, 2)
        WINDOW w AS (PARTITION BY wallet ORDER BY date ROWS UNBOUNDED PRECEDING)""")
    db.commit()
    return db.execute("SELECT COUNT(*) FROM kol_daily").fetchone()[0]


def streaks(results: list[bool]) -> dict:
    """Current streak (+n wins / -n losses) and longest win / loss streaks, in trade order."""
    cur = best_w = best_l = run = 0
    last = None
    for r in results:
        run = run + 1 if r == last else 1
        last = r
        if r:
            best_w = max(best_w, run)
        else:
            best_l = max(best_l, run)
    if last is not None:
        cur = run if last else -run
    return {"current": cur, "longest_win": best_w, "longest_loss": best_l}


def kol_history(db, wallet: str) -> dict:
    """Performance since first seen, from the ledger: totals, median, best / worst, streaks, daily snapshots."""
    k = db.execute("SELECT display_name, twitter, first_seen_at, last_seen_at, first_seen_source FROM kols "
                   "WHERE wallet=?", (wallet,)).fetchone()
    rows = db.execute("SELECT pnl_sol, net_pct, spend_sol FROM trades WHERE kol=? AND gap=0 ORDER BY exit_ts, id",
                      (wallet,)).fetchall()
    n = len(rows)
    pnl = sum(r[0] for r in rows)
    spend = sum(r[2] for r in rows)
    wins = sum(1 for r in rows if r[1] > 0)
    daily = [dict(zip(("date", "trades", "wins", "losses", "pnl_sol", "roi_pct", "win_rate_pct", "cum_trades",
                       "cum_wins", "cum_pnl_sol", "cum_roi_pct"), r)) for r in db.execute(
        "SELECT date, trades, wins, losses, pnl_sol, roi_pct, win_rate_pct, cum_trades, cum_wins, cum_pnl_sol, "
        "cum_roi_pct FROM kol_daily WHERE wallet=? ORDER BY date DESC LIMIT 400", (wallet,))]
    for d in daily:                                        # win rate since first seen, as of each day
        d["cum_win_rate_pct"] = round(100 * d["cum_wins"] / d["cum_trades"], 1) if d["cum_trades"] else None
    return {"wallet": wallet, "display_name": k[0] if k else None, "twitter": k[1] if k else None,
            "first_seen_at": k[2] if k else None, "last_seen_at": k[3] if k else None,
            "first_seen_source": k[4] if k else None,
            "history_status": "OK" if k and k[2] else "UNKNOWN",
            "total_trades": n, "winning_trades": wins, "losing_trades": n - wins,
            "win_rate_pct": round(100 * wins / n, 1) if n else None, "total_pnl_sol": round(pnl, 6),
            "roi_pct": round(100 * pnl / spend, 2) if spend else None,
            "avg_pnl_sol": round(pnl / n, 6) if n else None,
            "median_pnl_sol": round(statistics.median(r[0] for r in rows), 6) if n else None,
            "median_pct": round(statistics.median(r[1] for r in rows), 2) if n else None,
            "best_trade_pct": round(max(r[1] for r in rows), 2) if n else None,
            "worst_trade_pct": round(min(r[1] for r in rows), 2) if n else None,
            "streaks": streaks([r[1] > 0 for r in rows]), "daily": daily}
