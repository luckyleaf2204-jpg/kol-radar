"""dexarb.db — a NEW, separate SQLite database (WAL). Never opens / writes the old paper.db or any KOL / Smart
Wallet / S1-S3 file: open() refuses those names. Schema is versioned (schema_version table); the app creates the
schema on start; migrations only ever ADD (no destructive migration runs automatically).

Volume control (pre-registered): candidates and evaluations with gross spread > 0 are stored raw; every other
rejection is counted in opportunity_rollups (per day, chain, token, size, pair, reason; count / best / sum gross) and
1 % of them (deterministic hash sample) are stored raw for audit. Paper cycles / legs are always stored. Raw quote snapshots are kept RETAIN_QUOTES_DAYS; rollups,
ledger, cycles and audit events are kept. Quota DEXARB_DB_MAX_MB: above it raw quote / opportunity writes stop (a
data_gap is recorded) while the ledger keeps working."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from dexarb import SCHEMA_VERSION

FORBIDDEN_NAMES = ("paper.db", "signal_paper", "s3_signals", "mc.db", "demo.db")
RETAIN_QUOTES_DAYS = 7
RETAIN_REJECTED_DAYS = 30

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at REAL);
CREATE TABLE IF NOT EXISTS chains (key TEXT PRIMARY KEY, chain_id TEXT, kind TEXT, native TEXT, quote_asset TEXT,
    wallets TEXT);
CREATE TABLE IF NOT EXISTS dex_protocols (chain TEXT, key TEXT, name TEXT, kind TEXT, version TEXT, address TEXT,
    pool_types TEXT, quote_source TEXT, status TEXT, atomic TEXT, PRIMARY KEY (chain, key));
CREATE TABLE IF NOT EXISTS assets (chain TEXT, symbol TEXT, address TEXT, decimals INTEGER, role TEXT,
    risk TEXT, PRIMARY KEY (chain, address));
CREATE TABLE IF NOT EXISTS pools (chain TEXT, protocol TEXT, token_in TEXT, token_out TEXT, fee INTEGER, pool TEXT,
    seen_at REAL, PRIMARY KEY (chain, protocol, token_in, token_out, fee));
CREATE TABLE IF NOT EXISTS quote_snapshots (id INTEGER PRIMARY KEY, chain TEXT, protocol TEXT, token_in TEXT,
    token_out TEXT, amount_in TEXT, amount_out TEXT, status TEXT, error TEXT, route TEXT, fee_note TEXT, impact REAL,
    context INTEGER, fetched_at REAL, latency_ms REAL, purpose TEXT);
CREATE INDEX IF NOT EXISTS ix_qs_time ON quote_snapshots (fetched_at);
CREATE TABLE IF NOT EXISTS opportunities (id INTEGER PRIMARY KEY, uid TEXT UNIQUE, chain TEXT, chain_id TEXT,
    token TEXT, size REAL, buy_protocol TEXT, sell_protocol TEXT, buy_quote_id INTEGER, sell_quote_id INTEGER,
    detected_at REAL, buy_context INTEGER, sell_context INTEGER, quote_age_s REAL, amount_in REAL, tokens_mid REAL,
    amount_out REAL, gross_spread REAL, swap_fees TEXT, gas_cost REAL, setup_cost REAL, other_cost REAL,
    estimated_costs REAL, net_profit_estimate REAL, uncertainty_buffer REAL, net_profit_after_buffer REAL,
    cost_status TEXT, cost_detail TEXT, impact_buy REAL, impact_sell REAL, status TEXT, reason TEXT, sampled INTEGER,
    baseline INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_opp_time ON opportunities (detected_at);
CREATE TABLE IF NOT EXISTS opportunity_rollups (day TEXT, chain TEXT, token TEXT, size REAL, buy_protocol TEXT,
    sell_protocol TEXT, reason TEXT, n INTEGER, best_gross REAL, sum_gross REAL,
    PRIMARY KEY (day, chain, token, size, buy_protocol, sell_protocol, reason));
CREATE TABLE IF NOT EXISTS simulation_results (id INTEGER PRIMARY KEY, chain TEXT, kind TEXT, target TEXT,
    status TEXT, value REAL, detail TEXT, at REAL);
CREATE TABLE IF NOT EXISTS paper_accounts (id TEXT PRIMARY KEY, model TEXT, arm TEXT, created_at REAL, config TEXT);
CREATE TABLE IF NOT EXISTS paper_balances (account TEXT, chain TEXT, token TEXT, amount REAL, updated_at REAL,
    PRIMARY KEY (account, chain, token));
CREATE TABLE IF NOT EXISTS paper_positions (id INTEGER PRIMARY KEY, account TEXT, chain TEXT, token TEXT,
    amount REAL, cycle_id INTEGER, status TEXT, opened_at REAL, closed_at REAL, mark_value REAL, mark_at REAL);
CREATE TABLE IF NOT EXISTS paper_cycles (id INTEGER PRIMARY KEY, uid TEXT UNIQUE, account TEXT, opportunity_id
    INTEGER, chain TEXT, token TEXT, size REAL, buy_protocol TEXT, sell_protocol TEXT, status TEXT, reason TEXT,
    started_at REAL, closed_at REAL, spent REAL, received REAL, gas_cost REAL, setup_cost REAL, net REAL,
    detect_net_after_buffer REAL, quote_decay REAL, unknown_costs INTEGER DEFAULT 0, baseline INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS paper_legs (id INTEGER PRIMARY KEY, cycle_id INTEGER, leg INTEGER, attempt INTEGER,
    protocol TEXT, token_in TEXT, token_out TEXT, amount_in REAL, decision_at REAL, decision_quote_out REAL,
    min_out REAL, exec_at REAL, exec_quote_id INTEGER, exec_quote_out REAL, filled_out REAL, status TEXT,
    gas_native REAL, gas_quote REAL, cost_status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fee_snapshots (id INTEGER PRIMARY KEY, chain TEXT, kind TEXT, value REAL, status TEXT,
    source TEXT, at REAL);
CREATE TABLE IF NOT EXISTS oracle_prices (id INTEGER PRIMARY KEY, chain TEXT, base TEXT, quote TEXT, price REAL,
    source TEXT, context INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS feed_health (chain TEXT, protocol TEXT, status TEXT, last_ok REAL, last_error TEXT,
    ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, latency_ms REAL, updated_at REAL, PRIMARY KEY (chain, protocol));
CREATE TABLE IF NOT EXISTS data_gaps (id INTEGER PRIMARY KEY, chain TEXT, protocol TEXT, start REAL, end REAL,
    reason TEXT);
CREATE TABLE IF NOT EXISTS experiment_config (version TEXT PRIMARY KEY, registered_at REAL, config TEXT);
CREATE TABLE IF NOT EXISTS experiment_runs (id INTEGER PRIMARY KEY, version TEXT, started_at REAL, ended_at REAL,
    host TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS daily_rollups (day TEXT, account TEXT, chain TEXT, metric TEXT, value REAL,
    PRIMARY KEY (day, account, chain, metric));
CREATE TABLE IF NOT EXISTS db_storage_metrics (at REAL, table_name TEXT, rows INTEGER, est_bytes INTEGER,
    file_bytes INTEGER, PRIMARY KEY (at, table_name));
CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY, at REAL, kind TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path: str | Path, max_mb: float | None = None, clock=time.time):
        p = Path(path)
        if any(f in p.name for f in FORBIDDEN_NAMES):
            raise ValueError(f"refusing to open {p.name}: the arbitrage lab only writes its own dexarb.db")
        self.path, self.clock = p, clock
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p), check_same_thread=False, timeout=30)
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM schema_version WHERE version=?", (SCHEMA_VERSION,)).fetchone():
            self.db.execute("INSERT INTO schema_version VALUES (?, ?)", (SCHEMA_VERSION, clock()))
            self.audit("schema", {"version": SCHEMA_VERSION})
        self.db.commit()
        self.max_mb = float(max_mb if max_mb is not None else os.environ.get("DEXARB_DB_MAX_MB") or 1000)

    # --- basics ---------------------------------------------------------------------------------------------------
    def audit(self, kind: str, detail) -> None:
        self.db.execute("INSERT INTO audit_events (at, kind, detail) VALUES (?,?,?)",
                        (self.clock(), kind, json.dumps(detail, default=str)))

    def version(self) -> int:
        return self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]

    def size_mb(self) -> float:
        return sum(f.stat().st_size for f in self.path.parent.glob(self.path.name + "*") if f.is_file()) / 1e6

    def over_quota(self) -> bool:
        return self.size_mb() > self.max_mb

    def commit(self) -> None:
        self.db.commit()

    # --- writers --------------------------------------------------------------------------------------------------
    def add_quote(self, q, purpose: str) -> int | None:
        if self.over_quota():
            return None
        cur = self.db.execute(
            "INSERT INTO quote_snapshots (chain, protocol, token_in, token_out, amount_in, amount_out, status, error, "
            "route, fee_note, impact, context, fetched_at, latency_ms, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (q.chain, q.protocol, q.token_in, q.token_out, str(q.amount_in),
             None if q.amount_out is None else str(q.amount_out), q.status, q.error, json.dumps(q.route), q.fee_note,
             q.impact, q.context, q.fetched_at, q.latency_ms, purpose))
        return cur.lastrowid

    def rollup(self, day: str, chain: str, token: str, size: float, buy: str, sell: str, reason: str,
               gross: float | None) -> None:
        self.db.execute(
            "INSERT INTO opportunity_rollups VALUES (?,?,?,?,?,?,?,1,?,?) ON CONFLICT (day, chain, token, size, "
            "buy_protocol, sell_protocol, reason) DO UPDATE SET n = n + 1, best_gross = CASE WHEN "
            "excluded.best_gross IS NULL THEN best_gross WHEN best_gross IS NULL THEN excluded.best_gross ELSE "
            "MAX(best_gross, excluded.best_gross) END, sum_gross = sum_gross + excluded.sum_gross",
            (day, chain, token, size, buy, sell, reason, gross, gross or 0.0))      # no quote -> no gross (NULL)

    def health(self, chain: str, protocol: str, ok: bool, err: str = "", latency_ms: float | None = None) -> None:
        now = self.clock()
        self.db.execute(
            "INSERT INTO feed_health (chain, protocol, status, last_ok, last_error, ok, fail, latency_ms, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (chain, protocol) DO UPDATE SET status=excluded.status, "
            "last_ok=COALESCE(excluded.last_ok, last_ok), last_error=CASE WHEN excluded.last_error='' THEN last_error "
            "ELSE excluded.last_error END, ok=ok+excluded.ok, fail=fail+excluded.fail, "
            "latency_ms=COALESCE(excluded.latency_ms, latency_ms), updated_at=excluded.updated_at",
            (chain, protocol, "OK" if ok else "UNAVAILABLE", now if ok else None, err, int(ok), int(not ok),
             latency_ms, now))

    def gap(self, chain: str, protocol: str, start: float, end: float, reason: str) -> None:
        last = self.db.execute("SELECT id, end FROM data_gaps WHERE chain=? AND protocol=? AND reason=? ORDER BY id DESC "
                               "LIMIT 1", (chain, protocol, reason)).fetchone()
        if last and start - last[1] <= 120:
            self.db.execute("UPDATE data_gaps SET end=MAX(end, ?) WHERE id=?", (end, last[0]))
        else:
            self.db.execute("INSERT INTO data_gaps (chain, protocol, start, end, reason) VALUES (?,?,?,?,?)",
                            (chain, protocol, start, end, reason))

    # --- storage --------------------------------------------------------------------------------------------------
    def table_bytes(self, sample: int = 2000) -> dict:
        out = {}
        for (t,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                                    ).fetchall():
            cols = [r[1] for r in self.db.execute(f'PRAGMA table_info("{t}")')]
            n = self.db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            expr = " + ".join(f'IFNULL(LENGTH(CAST("{c}" AS BLOB)), 1)' for c in cols)
            avg = self.db.execute(f'SELECT AVG({expr}) FROM (SELECT * FROM "{t}" ORDER BY rowid DESC LIMIT {sample})'
                                  ).fetchone()[0] if n else 0
            out[t] = {"rows": n, "est_bytes": int(n * ((avg or 0) + len(cols)) * 1.3)}
        return out

    def record_metrics(self) -> dict:
        now, fb = self.clock(), int(self.size_mb() * 1e6)
        tb = self.table_bytes()
        self.db.executemany("INSERT OR REPLACE INTO db_storage_metrics VALUES (?,?,?,?,?)",
                            [(now, t, v["rows"], v["est_bytes"], fb) for t, v in tb.items()])
        self.db.commit()
        return tb

    def bytes_per_day(self) -> dict:
        """Per table: (latest est_bytes - est_bytes ~24 h earlier (or first sample)) scaled to a day."""
        rows = self.db.execute("SELECT at, table_name, est_bytes, file_bytes FROM db_storage_metrics ORDER BY at"
                               ).fetchall()
        if not rows:
            return {}
        last_at = rows[-1][0]
        first = {}
        for at, t, b, fb in rows:
            if at >= last_at - 86400 and t not in first:
                first[t] = (at, b)
        last = {t: (at, b) for at, t, b, fb in rows if at == last_at}
        out = {}
        for t, (at1, b1) in last.items():
            at0, b0 = first.get(t, (at1, b1))
            out[t] = round((b1 - b0) / max(at1 - at0, 1) * 86400) if at1 > at0 else None
        return out

    def retention(self, now: float | None = None, dry_run: bool = True, max_rows: int = 50_000) -> dict:
        now = self.clock() if now is None else now
        q_cut, r_cut = now - RETAIN_QUOTES_DAYS * 86400, now - RETAIN_REJECTED_DAYS * 86400
        keep_q = ("SELECT buy_quote_id FROM opportunities WHERE buy_quote_id IS NOT NULL UNION SELECT sell_quote_id "
                  "FROM opportunities WHERE sell_quote_id IS NOT NULL UNION SELECT exec_quote_id FROM paper_legs "
                  "WHERE exec_quote_id IS NOT NULL")      # a NULL here would make NOT IN match nothing
        nq = self.db.execute(f"SELECT COUNT(*) FROM quote_snapshots WHERE fetched_at < ? AND id NOT IN ({keep_q})",
                             (q_cut,)).fetchone()[0]
        nr = self.db.execute("SELECT COUNT(*) FROM opportunities WHERE detected_at < ? AND status='REJECTED' AND id NOT "
                             "IN (SELECT opportunity_id FROM paper_cycles WHERE opportunity_id IS NOT NULL)",
                             (r_cut,)).fetchone()[0]
        res = {"dry_run": dry_run, "quotes_eligible": nq, "rejected_eligible": nr, "deleted": 0}
        if not dry_run:
            d = self.db.execute(f"DELETE FROM quote_snapshots WHERE id IN (SELECT id FROM quote_snapshots WHERE "
                                f"fetched_at < ? AND id NOT IN ({keep_q}) LIMIT ?)", (q_cut, max_rows)).rowcount
            d += self.db.execute("DELETE FROM opportunities WHERE id IN (SELECT id FROM opportunities WHERE detected_at < "
                                 "? AND status='REJECTED' AND id NOT IN (SELECT opportunity_id FROM paper_cycles WHERE "
                                 "opportunity_id IS NOT NULL) LIMIT ?)", (r_cut, max_rows)).rowcount
            res["deleted"] = d
        self.audit("retention", res)
        self.db.commit()
        return res

    def backup(self, dest: str | Path) -> dict:
        """Online consistent copy (sqlite backup API) + integrity check of the copy."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = sqlite3.connect(str(dest))
        try:
            self.db.backup(tmp)
            ok = tmp.execute("PRAGMA integrity_check").fetchone()[0]
            n = tmp.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        finally:
            tmp.close()
        res = {"dest": str(dest), "bytes": dest.stat().st_size, "integrity": ok, "objects": n}
        self.audit("backup", res)
        self.db.commit()
        return res


def restore(src: str | Path, dest: str | Path) -> dict:
    """Restore a backup file to `dest` (refuses to overwrite an existing file)."""
    src, dest = Path(src), Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} exists: restore never overwrites")
    shutil.copyfile(src, dest)
    c = sqlite3.connect(str(dest))
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        v = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        c.close()
    return {"integrity": ok, "schema_version": v}
