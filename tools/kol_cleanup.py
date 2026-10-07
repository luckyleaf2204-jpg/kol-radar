"""Remove KOL data from the OLD database (paper.db) and the KOL book files. DRY-RUN by default.

Scope (identified, nothing else is touched):
  tables used ONLY by the KOL copy bot   kols, kol_roster, kol_daily, trades (KOL copy ledger: column `kol`),
                                         gaps (KOL engine stream gaps), state row k='engine' (KOL engine state)
  rows in a shared table                 signals WHERE source = 'kol'
  files                                  signal_paper_kol5.db, signal_paper_kol10.db (+ -wal / -shm)
Kept (not KOL): tokens, devs, dev_risk_log, devs_meta, wallet_links, sw_* (Smart Wallet), signals of other sources,
every other signal_paper_*.db, s3_signals.db, mc.db, dexarb.db.
Reported but NOT deleted (cannot be separated safely): Smart Wallet rows (sw_trades / sw_open / sw_wallets) of
wallets that are in kol_roster — they are Smart Wallet research rows about those wallets, not KOL-bot data.

Apply only with --apply AND --backup-confirmed <text> AND --expect-fingerprint <the dry-run fingerprint> (proves
the same database and the same row counts were reviewed). Writes a JSON report (rows / bytes before and after).
Run it only after the old KOL app has stopped writing (i.e. after the lab replaced it), never automatically.

usage: python tools/kol_cleanup.py --db /var/data/paper.db [--apply --backup-confirmed "snapshot 2026-10-08"
       --expect-fingerprint abcd1234] [--vacuum] [--report kol_cleanup_report.json]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

KOL_TABLES = ("kols", "kol_roster", "kol_daily", "trades", "gaps")
KOL_ROWS = (("state", "k = 'engine'"), ("signals", "source = 'kol'"))
KOL_FILES = ("signal_paper_kol5.db", "signal_paper_kol10.db")
AMBIGUOUS = (("sw_trades", "wallet"), ("sw_open", "wallet"), ("sw_wallets", "wallet"))


def table_bytes(db, t: str) -> int:
    cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
    if not cols:
        return 0
    expr = " + ".join(f'IFNULL(LENGTH(CAST("{c}" AS BLOB)), 1)' for c in cols)
    return int(db.execute(f'SELECT IFNULL(SUM({expr}), 0) FROM "{t}"').fetchone()[0])


def exists(db, t: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())


def plan(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if not exists(db, "kols") or not exists(db, "trades"):
            raise SystemExit(f"{path}: no kols / trades table - not the KOL Radar database, nothing done")
        cols = [r[1] for r in db.execute("PRAGMA table_info(trades)")]
        if "kol" not in cols:
            raise SystemExit(f"{path}: trades has no `kol` column - not the KOL ledger, nothing done")
        p = {"db": str(path), "db_bytes": sum(f.stat().st_size for f in path.parent.glob(path.name + "*")),
             "tables": {}, "rows": {}, "files": {}, "ambiguous": {}}
        for t in KOL_TABLES:
            if exists(db, t):
                p["tables"][t] = {"rows": db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0],
                                  "data_bytes": table_bytes(db, t)}
        for t, where in KOL_ROWS:
            if exists(db, t):
                n = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {where}').fetchone()[0]
                p["rows"][f"{t} WHERE {where}"] = n
        for f in KOL_FILES:
            fs = [x for x in path.parent.glob(f + "*") if x.is_file()]
            if fs:
                p["files"][f] = sum(x.stat().st_size for x in fs)
        if exists(db, "kol_roster"):
            for t, c in AMBIGUOUS:
                if exists(db, t):
                    p["ambiguous"][t] = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IN (SELECT wallet FROM '
                                                   f'kol_roster)').fetchone()[0]
    finally:
        db.close()
    fp_src = json.dumps({k: p[k] for k in ("tables", "rows", "files")}, sort_keys=True)
    p["fingerprint"] = hashlib.sha256((str(path.resolve()) + fp_src).encode()).hexdigest()[:16]
    return p


def apply(path: Path, p: dict, vacuum: bool) -> dict:
    db = sqlite3.connect(str(path))
    done = {"tables_dropped": [], "rows_deleted": {}, "files_deleted": []}
    try:
        db.execute("BEGIN")
        for t in p["tables"]:
            db.execute(f'DROP TABLE "{t}"')
            done["tables_dropped"].append(t)
        for t, where in KOL_ROWS:
            if exists(db, t):
                done["rows_deleted"][f"{t} WHERE {where}"] = db.execute(f'DELETE FROM "{t}" WHERE {where}').rowcount
        db.execute("COMMIT")
        if vacuum:
            db.execute("VACUUM")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    for f in p["files"]:
        for x in path.parent.glob(f + "*"):
            if x.is_file():
                x.unlink()
                done["files_deleted"].append(x.name)
    done["db_bytes_after"] = sum(f.stat().st_size for f in path.parent.glob(path.name + "*"))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("KOL_DB") or "data/paper.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-confirmed", default="")
    ap.add_argument("--expect-fingerprint", default="")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--report", default="")
    a = ap.parse_args(argv)
    path = Path(a.db)
    p = plan(path)
    out = {"at": time.time(), "mode": "dry-run", "plan": p}
    if a.apply:
        if not a.backup_confirmed:
            raise SystemExit("refused: --backup-confirmed is required (confirm a backup / disk snapshot exists)")
        if a.expect_fingerprint != p["fingerprint"]:
            raise SystemExit(f"refused: fingerprint {p['fingerprint']} != --expect-fingerprint "
                             f"{a.expect_fingerprint!r} (database or row counts changed since the dry run)")
        out.update(mode="apply", backup_confirmed=a.backup_confirmed, result=apply(path, p, a.vacuum))
    text = json.dumps(out, indent=1)
    if a.report:
        Path(a.report).write_text(text, encoding="utf-8")
    print(text)
    return out


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
