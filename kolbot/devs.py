"""Token creators (devs): persistent token records and dev profiles, built from the same pump.fun stream
(CreateEvent / TradeEvent carry the creator) and, for a dev's history before we started listening, from pump.fun's
public coin list. Information / risk signal only: nothing here changes what the paper bot copies.

Outcome of a token (never guessed):
  PENDING    younger than 24 h and not migrated: no outcome yet, counted nowhere as win or loss
  UNKNOWN_AGE creation time unknown (an old token first seen through a later trade, before pump.fun's coin data
             filled its date in): no outcome, counted nowhere as win or loss
  MIGRATED   the bonding curve completed (success)
  FAILED     >= 24 h old and not migrated; DEAD = failed and no trade for >= 24 h (subset of failed)
Rug evidence is only claimed for a token we watched from its creation (first seen <= 120 s after create) whose
dev sold >= 90 % of the tokens the dev bought, that failed, and whose market cap fell >= 80 % from its peak.
Anything else is "not checkable", never a rug. Outcome % (last / first market cap) is likewise only computed for
tokens watched from creation, so late-seen survivors do not bias it."""
from __future__ import annotations

import json
import statistics
import time

DAY = 86400
FROM_CREATION_S = 120
RUG_SOLD_FRAC = 0.9
RUG_DRAWDOWN = 0.8
FLUSH_S = 30
PROFILE_VERSION = 2
BACKFILL_BATCH = 300
NEW_DEV_COLS = (("seen_only", "INTEGER DEFAULT 0"), ("unknown_age", "INTEGER DEFAULT 0"),
                ("distinct_names", "INTEGER"), ("max_same_name", "INTEGER"), ("top_name", "TEXT"))
SOFT_S = 600
KEEP_IN_MEMORY_S = 2 * 3600
RISKS = ("UNKNOWN", "LOW RISK", "MEDIUM RISK", "HIGH RISK", "REPEAT FAILURE", "SUSPICIOUS / RUG HISTORY")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (mint TEXT PRIMARY KEY, creator TEXT, name TEXT, symbol TEXT, created_ts REAL,
    first_seen_ts REAL, first_mc_sol REAL, peak_mc_sol REAL, last_mc_sol REAL, last_trade_ts REAL,
    trades INTEGER DEFAULT 0, migrated INTEGER DEFAULT 0, migrated_ts REAL, dev_buy_tok REAL DEFAULT 0,
    dev_sell_tok REAL DEFAULT 0, dev_first_sell_ts REAL, kol_buys INTEGER DEFAULT 0, source TEXT,
    api_ath_usd REAL, api_mc_usd REAL, api_last_trade_ts REAL, updated_ts REAL);
CREATE INDEX IF NOT EXISTS ix_tokens_creator ON tokens(creator);
CREATE INDEX IF NOT EXISTS ix_tokens_kol ON tokens(kol_buys);
CREATE INDEX IF NOT EXISTS ix_tokens_seen ON tokens(first_seen_ts);
CREATE TABLE IF NOT EXISTS devs (wallet TEXT PRIMARY KEY, first_seen_ts REAL, last_token_ts REAL,
    created INTEGER, observed INTEGER, known INTEGER, migrated INTEGER, failed INTEGER, dead INTEGER,
    pending INTEGER, rug INTEGER, rug_checkable INTEGER, success_rate REAL, fail_rate REAL, rug_rate REAL,
    median_outcome_pct REAL, avg_outcome_pct REAL, outcome_n INTEGER, paper_pnl_sol REAL, paper_trades INTEGER,
    latest_mint TEXT, prev_mint TEXT, risk TEXT, risk_reason TEXT, risk_evidence TEXT, risk_ts REAL,
    updated_ts REAL);
CREATE INDEX IF NOT EXISTS ix_devs_created ON devs(created);
CREATE TABLE IF NOT EXISTS devs_meta (k TEXT PRIMARY KEY, v INTEGER);
CREATE TABLE IF NOT EXISTS dev_risk_log (wallet TEXT, risk TEXT, reason TEXT, evidence TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_dev_risk_log ON dev_risk_log(wallet, ts);
-- relations between wallets, only with an explicit source (e.g. 'pump.fun:creator'); nothing is inferred
CREATE TABLE IF NOT EXISTS wallet_links (wallet TEXT, related TEXT, relation TEXT, source TEXT, evidence TEXT,
    ts REAL, PRIMARY KEY (wallet, related, relation));
"""

COLS = ("mint", "creator", "name", "symbol", "created_ts", "first_seen_ts", "first_mc_sol", "peak_mc_sol",
        "last_mc_sol", "last_trade_ts", "trades", "migrated", "migrated_ts", "dev_buy_tok", "dev_sell_tok",
        "dev_first_sell_ts", "kol_buys", "source", "api_ath_usd", "api_mc_usd", "api_last_trade_ts", "updated_ts")


def classify(t: dict, now: float) -> dict:
    """Outcome and rug evidence of one token record (see module doc)."""
    born = t.get("created_ts")                    # never the first-seen time: an old token can be first seen today
    seen = [x for x in (t.get("last_trade_ts"), t.get("api_last_trade_ts"), born) if x]
    last_trade = max(seen) if seen else None
    if t.get("migrated"):
        outcome = "MIGRATED"
    elif born is None:
        outcome = "UNKNOWN_AGE"
    elif now - born < DAY:
        outcome = "PENDING"
    else:
        outcome = "DEAD" if last_trade and now - last_trade >= DAY else "FAILED"
    from_creation = bool(t.get("first_seen_ts") and t.get("created_ts")
                         and t["first_seen_ts"] - t["created_ts"] <= FROM_CREATION_S)
    known = outcome not in ("PENDING", "UNKNOWN_AGE")
    rug, evidence = None, None
    if from_creation and known:
        bought, sold = t.get("dev_buy_tok") or 0, t.get("dev_sell_tok") or 0
        peak, last = t.get("peak_mc_sol") or 0, t.get("last_mc_sol") or 0
        rug = bool(outcome != "MIGRATED" and bought > 0 and sold >= RUG_SOLD_FRAC * bought and peak > 0
                   and last <= (1 - RUG_DRAWDOWN) * peak)
        if rug:
            evidence = (f"dev bán {sold / bought:.0%} số token dev mua; MC từ đỉnh {peak:.1f} còn {last:.1f} SOL "
                        f"(-{1 - last / peak:.0%}); không migrate")
    outcome_pct = None
    if from_creation and known and t.get("first_mc_sol"):
        outcome_pct = 100 * ((t.get("last_mc_sol") or 0) / t["first_mc_sol"] - 1)
    return {"outcome": outcome, "known": known, "success": outcome == "MIGRATED",
            "failed": outcome in ("FAILED", "DEAD"), "dead": outcome == "DEAD", "rug": rug,
            "rug_checkable": rug is not None, "rug_evidence": evidence, "outcome_pct": outcome_pct,
            "from_creation": from_creation, "observed": bool(t.get("first_seen_ts"))}


def risk_of(p: dict) -> tuple[str, str]:
    """Dev risk label + reason, from the profile counts. Thresholds are fixed here and shown with the reason."""
    known, rug, chk = p["known"], p["rug"], p["rug_checkable"]
    if known < 3:
        return "UNKNOWN", f"chỉ {known} token đã có kết quả (cần >= 3)"
    if rug >= 2 and chk and rug / chk >= 0.3:
        return "SUSPICIOUS / RUG HISTORY", f"{rug}/{chk} token kiểm chứng được có bằng chứng dev xả (>= 2 và >= 30%)"
    if known >= 5 and p["migrated"] == 0:
        return "REPEAT FAILURE", f"{known} token đã có kết quả, 0 token migrate"
    if p["fail_rate"] >= 0.9 or rug >= 1:
        why = f"tỉ lệ thất bại {p['fail_rate']:.0%}" + (f", {rug} token có bằng chứng dev xả" if rug else "")
        return "HIGH RISK", why
    if p["fail_rate"] >= 0.6:
        return "MEDIUM RISK", f"tỉ lệ thất bại {p['fail_rate']:.0%} (60-90%)"
    return "LOW RISK", f"{p['migrated']}/{known} token migrate, tỉ lệ thất bại {p['fail_rate']:.0%}"


class DevTracker:
    def __init__(self, db, clock=time.time, log=print):
        self.db, self.clock, self.log = db, clock, log
        db.executescript(SCHEMA)
        have = {r[1] for r in db.execute("PRAGMA table_info(devs)")}
        for col, typ in NEW_DEV_COLS:
            if col not in have:
                db.execute(f"ALTER TABLE devs ADD COLUMN {col} {typ}")
        v = (db.execute("SELECT v FROM devs_meta WHERE k='profile_version'").fetchone() or [0])[0]
        # profiles written by an older version counted old tokens as new: recompute all, in the background
        self.backfill = [w for (w,) in db.execute("SELECT wallet FROM devs")] if v < PROFILE_VERSION else []
        db.execute("INSERT OR REPLACE INTO devs_meta VALUES ('profile_version', ?)", (PROFILE_VERSION,))
        db.commit()
        self.mem: dict[str, dict] = {}
        self.dirty: set[str] = set()
        self.dirty_devs: set[str] = set()          # refreshed at the next flush (create, migrate, dev / KOL trade)
        self.soft_devs: set[str] = set()           # only price moved: refreshed every SOFT_S
        self.last_soft = clock()
        self.last_flush = clock()
        self.last_age_scan = 0.0

    # --- stream ------------------------------------------------------------------------------------------------
    def _rec(self, mint: str) -> dict:
        t = self.mem.get(mint)
        if t is None:
            row = self.db.execute(f"SELECT {','.join(COLS)} FROM tokens WHERE mint=?", (mint,)).fetchone()
            t = dict(zip(COLS, row)) if row else {"mint": mint, "trades": 0, "migrated": 0, "dev_buy_tok": 0,
                                                    "dev_sell_tok": 0, "kol_buys": 0, "source": "stream"}
            self.mem[mint] = t
        return t

    def on_event(self, ev: dict, is_kol: bool = False) -> None:
        kind = ev["kind"]
        if kind not in ("create", "trade", "complete"):
            return
        t = self._rec(ev["mint"])
        important = kind != "trade" or is_kol or not t.get("first_seen_ts")
        now = ev.get("ts") or self.clock()
        if kind == "create":
            t.update(name=ev.get("name"), symbol=ev.get("symbol"), creator=ev.get("creator") or t.get("creator"),
                     created_ts=ev["ts"])
            t.setdefault("first_seen_ts", None)
            t["first_seen_ts"] = t["first_seen_ts"] or ev["ts"]
        elif kind == "trade":
            mc = ev["vsol"] / ev["vtok"] * 1e6
            if ev.get("creator"):
                t["creator"] = t.get("creator") or ev["creator"]
            if not t.get("first_seen_ts"):
                t["first_seen_ts"] = ev["ts"]
            if not t.get("first_mc_sol"):
                t["first_mc_sol"] = mc
            t["peak_mc_sol"] = max(t.get("peak_mc_sol") or 0, mc)
            t["last_mc_sol"], t["last_trade_ts"] = mc, ev["ts"]
            t["trades"] = (t.get("trades") or 0) + 1
            if t.get("creator") and ev["user"] == t["creator"]:
                important = True
                if ev["is_buy"]:
                    t["dev_buy_tok"] = (t.get("dev_buy_tok") or 0) + ev["token"]
                else:
                    t["dev_sell_tok"] = (t.get("dev_sell_tok") or 0) + ev["token"]
                    t["dev_first_sell_ts"] = t.get("dev_first_sell_ts") or ev["ts"]
            if is_kol and ev["is_buy"]:
                t["kol_buys"] = (t.get("kol_buys") or 0) + 1
        elif kind == "complete":
            t["migrated"], t["migrated_ts"] = 1, ev["ts"]
        t["updated_ts"] = now
        self.dirty.add(ev["mint"])
        if t.get("creator"):
            (self.dirty_devs if important else self.soft_devs).add(t["creator"])

    def ingest_coin(self, mint: str, d: dict) -> None:
        """pump.fun's own record of one coin: fills name / symbol / creation time that the stream did not see."""
        if not d or not d.get("creator"):
            return
        t = self._rec(mint)
        t["creator"] = t.get("creator") or d["creator"]
        t["name"] = t.get("name") or d.get("name")
        t["symbol"] = t.get("symbol") or d.get("symbol")
        if d.get("created_timestamp") and not t.get("created_ts"):
            t["created_ts"] = d["created_timestamp"] / 1000
        if d.get("complete"):
            t["migrated"] = 1
        t["updated_ts"] = self.clock()
        self.dirty.add(mint)
        self.dirty_devs.add(t["creator"])

    def ingest_api(self, creator: str, coins: list[dict]) -> None:
        """A dev's coin list from pump.fun (history before we listened). Stream fields are never overwritten."""
        for c in coins:
            mint = c.get("mint")
            if not mint:
                continue
            t = self._rec(mint)
            t["creator"] = t.get("creator") or c.get("creator") or creator
            t["name"] = t.get("name") or c.get("name")
            t["symbol"] = t.get("symbol") or c.get("symbol")
            if c.get("created_timestamp"):
                t["created_ts"] = t.get("created_ts") or c["created_timestamp"] / 1000
            if c.get("complete"):
                t["migrated"] = 1
            t["api_ath_usd"] = c.get("ath_market_cap")
            t["api_mc_usd"] = c.get("usd_market_cap")
            if c.get("last_trade_timestamp"):
                t["api_last_trade_ts"] = c["last_trade_timestamp"] / 1000
            if not t.get("first_seen_ts"):
                t["source"] = "api"
            t["updated_ts"] = self.clock()
            self.dirty.add(mint)
        self.dirty_devs.add(creator)
        self.db.execute("INSERT OR IGNORE INTO wallet_links VALUES (?,?,?,?,?,?)",
                        (creator, creator, "creator", "pump.fun:creator", f"{len(coins)} coins", self.clock()))

    # --- persistence ---------------------------------------------------------------------------------------------
    def tick(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last_flush < FLUSH_S:
            return
        self.last_flush = now
        if now - self.last_age_scan >= 600:            # tokens crossing 24 h change outcome without any event
            self.last_age_scan = now
            for (cr,) in self.db.execute(
                    "SELECT DISTINCT creator FROM tokens WHERE creator IS NOT NULL AND migrated = 0 AND "
                    "COALESCE(created_ts, first_seen_ts) BETWEEN ? AND ?", (now - DAY - 900, now - DAY)):
                self.dirty_devs.add(cr)
        if now - self.last_soft >= SOFT_S:
            self.last_soft = now
            self.dirty_devs |= self.soft_devs
            self.soft_devs = set()
        self.flush()
        cut = now - KEEP_IN_MEMORY_S
        for m in [m for m, t in self.mem.items() if (t.get("updated_ts") or 0) < cut and m not in self.dirty]:
            del self.mem[m]

    def flush(self) -> None:
        if self.dirty:
            rows = [tuple(self.mem[m].get(c) for c in COLS) for m in self.dirty if m in self.mem]
            self.db.executemany(f"INSERT OR REPLACE INTO tokens ({','.join(COLS)}) VALUES "
                                f"({','.join('?' * len(COLS))})", rows)
            self.dirty.clear()
        devs, self.dirty_devs = self.dirty_devs, set()
        if self.backfill:
            devs |= set(self.backfill[:BACKFILL_BATCH])
            self.backfill = self.backfill[BACKFILL_BATCH:]
        for d in devs:
            self.refresh_dev(d)
        self.db.commit()

    def refresh_dev(self, wallet: str) -> dict:
        """Recompute and store one dev profile from its token records (and the paper ledger)."""
        now = self.clock()
        toks = [dict(zip(COLS, r)) for r in self.db.execute(
            f"SELECT {','.join(COLS)} FROM tokens WHERE creator=?", (wallet,))]
        cls = [classify(t, now) for t in toks]
        known = sum(c["known"] for c in cls)
        migrated = sum(c["success"] for c in cls)
        failed = sum(c["failed"] for c in cls)
        rug = sum(1 for c in cls if c["rug"])
        chk = sum(c["rug_checkable"] for c in cls)
        outs = [c["outcome_pct"] for c in cls if c["outcome_pct"] is not None]
        pnl = self.db.execute("SELECT COUNT(*), COALESCE(SUM(t.pnl_sol),0) FROM trades t JOIN tokens k ON "
                              "k.mint = t.mint WHERE k.creator=? AND t.gap=0", (wallet,)).fetchone() \
            if self._has_trades() else (0, 0.0)
        by_time = sorted(toks, key=lambda t: t.get("created_ts") or 0)       # unknown dates sort first, not last
        names: dict[str, int] = {}
        for t in toks:
            if t.get("name"):
                k = t["name"].strip().lower()
                names[k] = names.get(k, 0) + 1
        top_name = max(names, key=names.get) if names else None
        p = {"wallet": wallet, "created": len(toks), "observed": sum(c["observed"] for c in cls), "known": known,
             "migrated": migrated, "failed": failed, "dead": sum(c["dead"] for c in cls),
             "pending": sum(c["outcome"] == "PENDING" for c in cls), "rug": rug, "rug_checkable": chk,
             "seen_only": sum(1 for t in toks if not t.get("created_ts")),
             "unknown_age": sum(c["outcome"] == "UNKNOWN_AGE" for c in cls),
             "distinct_names": len(names), "max_same_name": names[top_name] if top_name else None,
             "top_name": top_name,
             "success_rate": migrated / known if known else None, "fail_rate": failed / known if known else 0.0,
             "rug_rate": rug / chk if chk else None,
             "median_outcome_pct": statistics.median(outs) if outs else None,
             "avg_outcome_pct": sum(outs) / len(outs) if outs else None, "outcome_n": len(outs),
             "paper_trades": pnl[0], "paper_pnl_sol": pnl[1],
             "first_seen_ts": min((t.get("first_seen_ts") or t.get("created_ts") or now) for t in toks) if toks else now,
             "last_token_ts": (by_time[-1].get("created_ts") or by_time[-1].get("first_seen_ts")) if toks else None,
             "latest_mint": by_time[-1]["mint"] if toks else None,
             "prev_mint": by_time[-2]["mint"] if len(toks) > 1 else None}
        risk, reason = risk_of(p)
        evidence = json.dumps({k: p[k] for k in ("created", "known", "migrated", "failed", "dead", "pending", "rug",
                                                "rug_checkable", "outcome_n")} |
                              {"rug_tokens": [{"mint": t["mint"], "evidence": c["rug_evidence"]}
                                              for t, c in zip(toks, cls) if c["rug"]][:10]}, ensure_ascii=False)
        old = self.db.execute("SELECT risk, risk_evidence, risk_ts FROM devs WHERE wallet=?", (wallet,)).fetchone()
        risk_ts = old[2] if old and old[0] == risk and old[1] == evidence else now
        if not old or old[0] != risk:
            self.db.execute("INSERT INTO dev_risk_log VALUES (?,?,?,?,?)", (wallet, risk, reason, evidence, now))
        p.update(risk=risk, risk_reason=reason, risk_evidence=evidence, risk_ts=risk_ts, updated_ts=now)
        cols = ("wallet", "first_seen_ts", "last_token_ts", "created", "observed", "known", "migrated", "failed",
                "dead", "pending", "rug", "rug_checkable", "success_rate", "fail_rate", "rug_rate",
                "median_outcome_pct", "avg_outcome_pct", "outcome_n", "paper_pnl_sol", "paper_trades", "latest_mint",
                "prev_mint", "risk", "risk_reason", "risk_evidence", "risk_ts", "updated_ts") + \
            tuple(c for c, _ in NEW_DEV_COLS)
        self.db.execute(f"INSERT OR REPLACE INTO devs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(p[c] for c in cols))
        return p

    def _has_trades(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone())


# --- read API (spec names) -------------------------------------------------------------------------------------
DEV_COLS = ("wallet", "first_seen_ts", "last_token_ts", "created", "observed", "known", "migrated", "failed", "dead",
            "pending", "rug", "rug_checkable", "success_rate", "fail_rate", "rug_rate", "median_outcome_pct",
            "avg_outcome_pct", "outcome_n", "paper_pnl_sol", "paper_trades", "latest_mint", "prev_mint", "risk",
            "risk_reason", "risk_evidence", "risk_ts", "updated_ts", "seen_only", "unknown_age", "distinct_names",
            "max_same_name", "top_name")


def get_dev_profile(db, wallet: str) -> dict | None:
    r = db.execute(f"SELECT {','.join(DEV_COLS)} FROM devs WHERE wallet=?", (wallet,)).fetchone()
    if not r:
        return None
    p = dict(zip(DEV_COLS, r))
    p["risk_evidence"] = json.loads(p["risk_evidence"]) if p["risk_evidence"] else None
    return p


def get_dev_tokens(db, wallet: str, page: int = 1, size: int = 25, now: float | None = None) -> dict:
    now = now or time.time()
    total = db.execute("SELECT COUNT(*) FROM tokens WHERE creator=?", (wallet,)).fetchone()[0]
    size = max(5, min(100, int(size)))
    pages = max(1, -(-total // size))
    page = max(1, min(int(page), pages))
    rows = []
    for r in db.execute(f"SELECT {','.join(COLS)} FROM tokens WHERE creator=? ORDER BY "
                        "created_ts IS NULL, COALESCE(created_ts, first_seen_ts) DESC LIMIT ? OFFSET ?", (wallet, size, (page - 1) * size)):
        t = dict(zip(COLS, r))
        t.update(classify(t, now))
        rows.append(t)
    mints = [t["mint"] for t in rows]
    if mints and db.execute("SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone():
        pnl = dict(((m, (n, p)) for m, n, p in db.execute(
            f"SELECT mint, COUNT(*), SUM(pnl_sol) FROM trades WHERE gap=0 AND mint IN ({','.join('?' * len(mints))}) "
            "GROUP BY mint", mints)))
        for t in rows:
            t["paper_trades"], t["paper_pnl_sol"] = pnl.get(t["mint"], (0, None))
    return {"total": total, "page": page, "pages": pages, "size": size, "rows": rows}


def get_token_creator(db, mint: str) -> str | None:
    r = db.execute("SELECT creator FROM tokens WHERE mint=?", (mint,)).fetchone()
    return r[0] if r else None


def get_dev_risk(db, wallet: str) -> dict | None:
    p = get_dev_profile(db, wallet)
    if not p:
        return None
    log = [dict(zip(("risk", "reason", "evidence", "ts"), r)) for r in db.execute(
        "SELECT risk, reason, evidence, ts FROM dev_risk_log WHERE wallet=? ORDER BY ts DESC LIMIT 20", (wallet,))]
    return {"wallet": wallet, "risk": p["risk"], "reason": p["risk_reason"], "evidence": p["risk_evidence"],
            "ts": p["risk_ts"], "history": log}


def dev_badge(db, creator: str | None, current_mint: str | None = None) -> dict | None:
    """KNOWN DEV / NEW DEV summary shown next to a token."""
    if not creator:
        return None
    p = get_dev_profile(db, creator)
    prev = (p["created"] - 1) if p else 0
    if not p or prev <= 0:
        return {"creator": creator, "known_dev": False, "label": "NEW DEV", "previous_tokens": max(prev, 0),
                "risk": p["risk"] if p else "UNKNOWN"}
    return {"creator": creator, "known_dev": True, "label": "KNOWN DEV", "previous_tokens": prev,
            "seen_only": p.get("seen_only"), "max_same_name": p.get("max_same_name"), "top_name": p.get("top_name"),
            "known": p["known"], "migrated": p["migrated"], "failed": p["failed"], "rug": p["rug"],
            "rug_checkable": p["rug_checkable"], "fail_rate": p["fail_rate"], "rug_rate": p["rug_rate"],
            "median_outcome_pct": p["median_outcome_pct"], "outcome_n": p["outcome_n"], "risk": p["risk"],
            "risk_reason": p["risk_reason"], "latest_mint": p["latest_mint"], "last_token_ts": p["last_token_ts"]}
