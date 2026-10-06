"""Smart Wallet Radar: discovery and ranking of NON-KOL wallets from their OWN pump.fun bonding-curve round trips.
Independent research module: it never trades, never feeds the paper bot, Direction B or C, and does not touch the
KOL roster / copy rules / costs. KOL wallets are recorded like any wallet but excluded at query time, so a wallet
added to the KOL roster later disappears from this list automatically.

A position = one wallet in one token, opened by a buy >= 0.05 SOL (smaller first buys are dust and ignored), then
grown by further buys. It is RESOLVED when:
  closed    the wallet sold >= 99 % of the tokens it bought: P&L = SOL received - SOL paid (pump.fun fees included)
  expired   the token had no trade for 24 h while the wallet still held: the rest is valued at the token's last
            curve price (usually ~0), so a wallet that never sells a dead token takes the loss (no survivorship)
and stays UNRESOLVED (excluded from every statistic, counted separately) when the token completed its curve
(migrated: what happens on PumpSwap is not recorded). Positions that overlap a stream gap > 5 min (including the
time the process was down) are flagged and excluded, like the KOL ledger.
Win = P&L > 0. Status uses the KOL rule unchanged: n < 100 INCONCLUSIVE; n >= 100 and the 95 % bootstrap CI of
the mean ROI per trade > 0 PROVISIONAL, else REJECT; never PASS (no control)."""
from __future__ import annotations

import time

LAMPORTS = 1_000_000_000
MIN_OPEN_SOL = 50_000_000                 # 0.05 SOL
CLOSE_FRAC = 0.99
EXPIRE_S = 86400
KEEP_IN_MEMORY_S = 2 * 3600
FLUSH_S = 30
SWEEP_S = 600
CI_EVERY_S = 120
CI_BATCH = 200
GAP_FLAG_S = 300
RAW_SUPPLY = 1e15                         # pump.fun: 1e9 tokens x 1e6

SCHEMA = """
CREATE TABLE IF NOT EXISTS sw_open (wallet TEXT, mint TEXT, open_ts REAL, last_ts REAL, sol_in REAL, sol_out REAL,
    tok_in REAL, tok_out REAL, buys INTEGER, sells INTEGER, self_token INTEGER, PRIMARY KEY (wallet, mint));
CREATE INDEX IF NOT EXISTS ix_sw_open_last ON sw_open(last_ts);
CREATE TABLE IF NOT EXISTS sw_trades (id INTEGER PRIMARY KEY, uid TEXT UNIQUE, wallet TEXT, mint TEXT, open_ts REAL,
    close_ts REAL, sol_in REAL, sol_out REAL, mark_sol REAL, pnl_sol REAL, roi_pct REAL, kind TEXT, hold_s REAL,
    buys INTEGER, sells INTEGER, self_token INTEGER, gap INTEGER);
CREATE INDEX IF NOT EXISTS ix_sw_trades_wallet ON sw_trades(wallet, close_ts);
CREATE INDEX IF NOT EXISTS ix_sw_trades_close ON sw_trades(close_ts);
CREATE TABLE IF NOT EXISTS sw_wallets (wallet TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, n INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0, pnl_sol REAL DEFAULT 0, sol_in REAL DEFAULT 0, roi_sum REAL DEFAULT 0, best_pct REAL,
    worst_pct REAL, cur_streak INTEGER DEFAULT 0, long_win INTEGER DEFAULT 0, long_loss INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0, unresolved INTEGER DEFAULT 0, gap_excluded INTEGER DEFAULT 0,
    self_trades INTEGER DEFAULT 0, hold_sum REAL DEFAULT 0, ci_lo REAL, ci_hi REAL, ci_n INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sw_wallets_n ON sw_wallets(n);
CREATE TABLE IF NOT EXISTS sw_gaps (start REAL, end REAL);
CREATE TABLE IF NOT EXISTS sw_meta (k TEXT PRIMARY KEY, v REAL);
CREATE TABLE IF NOT EXISTS kol_roster (wallet TEXT PRIMARY KEY);
"""
OPEN_COLS = ("wallet", "mint", "open_ts", "last_ts", "sol_in", "sol_out", "tok_in", "tok_out", "buys", "sells",
             "self_token")


class SmartTracker:
    def __init__(self, db, roster: dict, clock=time.time, log=print):
        self.db, self.clock, self.log = db, clock, log
        db.executescript(SCHEMA)
        db.execute("DELETE FROM kol_roster")                 # the roster in force now decides who is a KOL
        db.executemany("INSERT OR IGNORE INTO kol_roster VALUES (?)", [(w,) for w in roster])
        now = clock()
        hb = db.execute("SELECT v FROM sw_meta WHERE k='heartbeat'").fetchone()
        if hb and now - hb[0] > 60:                          # process was down: trades in between were missed
            db.execute("INSERT INTO sw_gaps VALUES (?, ?)", (hb[0], now))
        db.execute("INSERT OR REPLACE INTO sw_meta VALUES ('heartbeat', ?)", (now,))
        db.commit()
        self.gaps = [g for g in db.execute("SELECT start, end FROM sw_gaps") if g[1] - g[0] > GAP_FLAG_S]
        self.mem: dict[tuple, dict] = {}
        self.dirty: set[tuple] = set()
        self.removed: set[tuple] = set()
        self.resolved: list[dict] = []
        self.creators: dict[str, str] = {}
        self.last_flush = self.last_sweep = self.last_ci = now

    # --- stream ---------------------------------------------------------------------------------------------------
    def _load(self, key):
        p = self.mem.get(key)
        if p is None and key not in self.removed:
            r = self.db.execute(f"SELECT {','.join(OPEN_COLS)} FROM sw_open WHERE wallet=? AND mint=?", key).fetchone()
            if r:
                p = self.mem[key] = dict(zip(OPEN_COLS, r))
        return p

    def on_event(self, ev: dict) -> None:
        kind = ev["kind"]
        if kind == "create":
            self.creators[ev["mint"]] = ev.get("creator")
            return
        if kind == "complete":
            self._complete(ev["mint"], ev["ts"])
            return
        if kind != "trade":
            return
        key = (ev["user"], ev["mint"])
        fee = ev.get("fee_lamports") or 0
        if ev.get("creator"):
            self.creators.setdefault(ev["mint"], ev["creator"])
        p = self._load(key)
        if ev["is_buy"]:
            if p is None:
                if ev["sol"] < MIN_OPEN_SOL:
                    return
                p = self.mem[key] = {"wallet": key[0], "mint": key[1], "open_ts": ev["ts"], "last_ts": ev["ts"],
                                     "sol_in": 0.0, "sol_out": 0.0, "tok_in": 0.0, "tok_out": 0.0, "buys": 0,
                                     "sells": 0, "self_token": int(self.creators.get(key[1]) == key[0])}
                self.removed.discard(key)
            p["sol_in"] += ev["sol"] + fee
            p["tok_in"] += ev["token"]
            p["buys"] += 1
        else:
            if p is None:
                return
            p["sol_out"] += max(0, ev["sol"] - fee)
            p["tok_out"] += ev["token"]
            p["sells"] += 1
        p["last_ts"] = ev["ts"]
        if p["tok_in"] > 0 and p["tok_out"] >= CLOSE_FRAC * p["tok_in"]:
            self._resolve(p, ev["ts"], "closed", 0.0)
        else:
            self.dirty.add(key)

    def on_gap(self, start: float, end: float) -> None:
        self.db.execute("INSERT INTO sw_gaps VALUES (?, ?)", (start, end))
        if end - start > GAP_FLAG_S:
            self.gaps.append((start, end))

    def _complete(self, mint: str, ts: float) -> None:
        keys = [k for k in self.mem if k[1] == mint]
        keys += [k for k in (tuple(r) for r in self.db.execute("SELECT wallet, mint FROM sw_open WHERE mint=?", (mint,)))
                 if k not in self.mem and k not in self.removed]
        for k in keys:
            p = self._load(k)
            if p:
                self._resolve(p, ts, "migrated", None)

    def _resolve(self, p: dict, ts: float, kind: str, mark_sol) -> None:
        key = (p["wallet"], p["mint"])
        self.mem.pop(key, None)
        self.dirty.discard(key)
        self.removed.add(key)
        gap = any(a < ts and b > p["open_ts"] for a, b in self.gaps)
        sol_in = p["sol_in"] / LAMPORTS
        if kind == "migrated":
            pnl = roi = None
        else:
            pnl = p["sol_out"] / LAMPORTS + (mark_sol or 0.0) - sol_in
            roi = 100 * pnl / sol_in if sol_in > 0 else None
        self.resolved.append({"uid": f"{p['wallet']}:{p['mint']}:{float(p['open_ts'])}", "wallet": p["wallet"],
                              "mint": p["mint"], "open_ts": p["open_ts"], "close_ts": ts, "sol_in": sol_in,
                              "sol_out": p["sol_out"] / LAMPORTS, "mark_sol": mark_sol, "pnl_sol": pnl,
                              "roi_pct": roi, "kind": kind, "hold_s": ts - p["open_ts"], "buys": p["buys"],
                              "sells": p["sells"], "self_token": p["self_token"], "gap": int(gap)})

    # --- persistence ----------------------------------------------------------------------------------------------
    def tick(self, force: bool = False) -> None:
        now = self.clock()
        if force or now - self.last_sweep >= SWEEP_S:
            self.last_sweep = now
            self.sweep(now)
        if force or now - self.last_flush >= FLUSH_S:
            self.last_flush = now
            self.flush(now)
        if force or now - self.last_ci >= CI_EVERY_S:
            self.last_ci = now
            self.refresh_ci()

    def sweep(self, now: float) -> int:
        """Positions whose token had no trade for 24 h are resolved at the token's last curve price."""
        self.flush(now)
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='tokens'").fetchone():
            return 0
        rows = self.db.execute(
            f"SELECT {','.join('o.' + c for c in OPEN_COLS)}, k.last_mc_sol, k.migrated, k.migrated_ts, "
            "COALESCE(k.last_trade_ts, o.last_ts) FROM sw_open o LEFT JOIN tokens k ON k.mint = o.mint "
            "WHERE COALESCE(k.last_trade_ts, o.last_ts) < ? LIMIT 20000", (now - EXPIRE_S,)).fetchall()
        for r in rows:
            p = dict(zip(OPEN_COLS, r[:len(OPEN_COLS)]))
            mc, migrated, mts, last = r[len(OPEN_COLS):]
            if migrated:
                self._resolve(p, mts or last, "migrated", None)
            else:
                held = max(0.0, p["tok_in"] - p["tok_out"])
                self._resolve(p, last + EXPIRE_S, "expired", held * (mc or 0.0) / RAW_SUPPLY)
        self.flush(now)
        return len(rows)

    def flush(self, now: float | None = None) -> None:
        now = now or self.clock()
        if self.dirty:
            self.db.executemany(f"INSERT OR REPLACE INTO sw_open ({','.join(OPEN_COLS)}) VALUES "
                                f"({','.join('?' * len(OPEN_COLS))})",
                                [tuple(self.mem[k][c] for c in OPEN_COLS) for k in self.dirty if k in self.mem])
            self.dirty.clear()
        if self.removed:
            self.db.executemany("DELETE FROM sw_open WHERE wallet=? AND mint=?", list(self.removed))
        if self.resolved:
            self._write_resolved(self.resolved)
            self.resolved = []
        self.removed = {k for k in self.removed if k in self.mem}
        cut = now - KEEP_IN_MEMORY_S                       # quiet positions live in sw_open only
        for k in [k for k, p in self.mem.items() if p["last_ts"] < cut]:
            del self.mem[k]
        if len(self.creators) > 200_000:
            self.creators.clear()
        self.db.execute("INSERT OR REPLACE INTO sw_meta VALUES ('heartbeat', ?)", (now,))
        self.db.commit()

    def _write_resolved(self, rows: list[dict]) -> None:
        cols = ("uid", "wallet", "mint", "open_ts", "close_ts", "sol_in", "sol_out", "mark_sol", "pnl_sol", "roi_pct",
                "kind", "hold_s", "buys", "sells", "self_token", "gap")
        for r in sorted(rows, key=lambda x: x["close_ts"]):
            cur = self.db.execute(f"INSERT OR IGNORE INTO sw_trades ({','.join(cols)}) VALUES "
                                  f"({','.join('?' * len(cols))})", tuple(r[c] for c in cols))
            if cur.rowcount == 0:                           # replayed position: already counted once
                continue
            self._aggregate(r)

    def _aggregate(self, r: dict) -> None:
        """Incremental wallet record (first / last seen never move the wrong way; identity = wallet)."""
        w = r["wallet"]
        self.db.execute("INSERT OR IGNORE INTO sw_wallets (wallet, first_seen, last_seen) VALUES (?,?,?)",
                        (w, r["open_ts"], r["close_ts"]))
        self.db.execute("UPDATE sw_wallets SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?) "
                        "WHERE wallet = ?", (r["open_ts"], r["close_ts"], w))
        if r["kind"] == "migrated":
            self.db.execute("UPDATE sw_wallets SET unresolved = unresolved + 1 WHERE wallet = ?", (w,))
            return
        if r["gap"]:
            self.db.execute("UPDATE sw_wallets SET gap_excluded = gap_excluded + 1 WHERE wallet = ?", (w,))
            return
        win = r["pnl_sol"] > 0
        new_token = not self.db.execute("SELECT 1 FROM sw_trades WHERE wallet=? AND mint=? AND uid<>? AND gap=0 "
                                        "AND kind<>'migrated' LIMIT 1", (w, r["mint"], r["uid"])).fetchone()
        cur, lw, ll = self.db.execute("SELECT cur_streak, long_win, long_loss FROM sw_wallets WHERE wallet=?",
                                      (w,)).fetchone()
        cur = (cur + 1 if cur > 0 else 1) if win else (cur - 1 if cur < 0 else -1)
        roi = r["roi_pct"] or 0.0
        self.db.execute(
            "UPDATE sw_wallets SET n = n + 1, wins = wins + ?, pnl_sol = pnl_sol + ?, sol_in = sol_in + ?, "
            "roi_sum = roi_sum + ?, best_pct = MAX(COALESCE(best_pct, ?), ?), worst_pct = MIN(COALESCE(worst_pct, ?), ?), "
            "cur_streak = ?, long_win = ?, long_loss = ?, tokens = tokens + ?, self_trades = self_trades + ?, "
            "hold_sum = hold_sum + ? WHERE wallet = ?",
            (int(win), r["pnl_sol"], r["sol_in"], roi, roi, roi, roi, roi, cur, max(lw, cur), max(ll, -cur),
             int(new_token), r["self_token"], r["hold_s"], w))

    def refresh_ci(self) -> int:
        """Bootstrap CIs for wallets with n >= 100 whose sample grew >= 10 % since the last CI (bounded batch)."""
        from kolbot.api import boot_ci
        todo = [w for (w,) in self.db.execute(
            "SELECT wallet FROM sw_wallets WHERE n >= 100 AND (ci_n = 0 OR n >= ci_n * 1.1) LIMIT ?", (CI_BATCH,))]
        for w in todo:
            vals = [x for (x,) in self.db.execute("SELECT roi_pct FROM sw_trades WHERE wallet=? AND gap=0 AND "
                                                  "kind<>'migrated' AND roi_pct IS NOT NULL", (w,))]
            ci = boot_ci(vals)
            self.db.execute("UPDATE sw_wallets SET ci_lo=?, ci_hi=?, ci_n=? WHERE wallet=?",
                            (ci[0] if ci else None, ci[1] if ci else None, len(vals), w))
        if todo:
            self.db.commit()
        return len(todo)
