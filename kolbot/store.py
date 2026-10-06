"""SQLite journal of the paper bot: closed trades, stream gaps, and the state needed to resume after a restart
(cash, open positions). Pending entries are not kept: a restart drops them."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, mint TEXT, kol TEXT, kol_sol REAL, trigger_ts REAL,
    entry_ts REAL, entry_how TEXT, exit_ts REAL, exit_kind TEXT, spend_sol REAL, proceeds_sol REAL, pnl_sol REAL,
    net_pct REAL, gap INTEGER);
CREATE TABLE IF NOT EXISTS gaps (start REAL, end REAL);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_kol_exit ON trades(kol, exit_ts);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS ix_trades_mint ON trades(mint);
"""


class Store:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotency key per trade: one position = (kol, mint, trigger time). Re-closing the same position after a
        restart can never write a second row."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        if "uid" not in cols:
            self.db.execute("ALTER TABLE trades ADD COLUMN uid TEXT")
        self.db.execute("UPDATE trades SET uid = kol || ':' || mint || ':' || CAST(trigger_ts AS TEXT) WHERE uid IS NULL")
        try:
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_uid ON trades(uid)")
        except sqlite3.IntegrityError:          # pre-existing duplicates: keep them, report, do not delete data
            print("[store] WARNING: duplicate trade uids in the ledger; unique index not created")
        self.db.commit()

    @staticmethod
    def uid(row: dict) -> str:
        return f"{row['kol']}:{row['mint']}:{float(row['trigger_ts'])}"

    def closed(self, row: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO trades (id, mint, kol, kol_sol, trigger_ts, entry_ts, entry_how, exit_ts, "
                        "exit_kind, spend_sol, proceeds_sol, pnl_sol, net_pct, gap, uid) VALUES (:id,:mint,:kol,"
                        ":kol_sol,:trigger_ts,:entry_ts,:entry_how,:exit_ts,:exit_kind,:spend_sol,:proceeds_sol,"
                        ":pnl_sol,:net_pct,:gap,:uid)", dict(row, uid=self.uid(row)))

    def gap(self, start: float, end: float) -> None:
        self.db.execute("INSERT INTO gaps VALUES (?, ?)", (start, end))
        self.db.commit()

    def save(self, eng) -> None:
        st = {"cash": eng.cash, "next_id": eng.next_id, "day": eng.day, "day_loss": eng.day_loss,
              "positions": [asdict(p) for p in eng.positions.values()], "counts": eng.counts}
        self.db.execute("INSERT OR REPLACE INTO state VALUES ('engine', ?)", (json.dumps(st),))
        self.db.commit()

    def restore(self, eng) -> None:
        from kolbot.engine import Position
        r = self.db.execute("SELECT v FROM state WHERE k='engine'").fetchone()
        eng.gaps = [tuple(g) for g in self.db.execute("SELECT start, end FROM gaps")]
        eng.closed = [dict(zip([c[0] for c in cur.description], row)) for cur in
                      [self.db.execute("SELECT * FROM trades ORDER BY id")] for row in cur.fetchall()]
        if not r:
            return
        st = json.loads(r[0])
        eng.cash, eng.next_id, eng.day, eng.day_loss = st["cash"], st["next_id"], st["day"], st["day_loss"]
        eng.counts = st.get("counts", eng.counts)
        eng.positions = {p["mint"]: Position(**p) for p in st["positions"]}
