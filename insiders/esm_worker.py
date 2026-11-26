"""Early Smart Money worker: the 120-minute window of each queued mint, then its big wallets join the watchlist.

ON by default since 2026-11-26 (the user asked for the tracing to run by itself); INSIDER_ESM=0 turns it off. Started
by insiders.site in its own thread. Read-only on chain, never signs or sends. Every RPC call goes through `rpc` (Router:
the server's Helius key first, at most ESM_HELIUS_MAX_DAY calls a day, then the public RPC; metered in rpc_usage, at
most ESM_MAX_CALLS_DAY a day) and is counted per coin; a per-step budget stops it.

Intake: a coin asked for on the page (esm_pending_mint status REQUESTED) is queued (RUNNING); the creation check below
is its verification (the oldest transaction must create that mint), so a wrong address ends REJECTED.

Stages per mint (esm_queue.state):
  DISCOVER  page getSignaturesForAddress back to the oldest signature (the creation); signatures kept in esm_sig.
            More than MAX_PAGES pages -> INSUFFICIENT_DATA "chưa tới giao dịch tạo" (no getTransactionsForAddress).
  CREATION  read the oldest successful signature: must create the mint (initializeMint / pump Create) -> t_create.
  WAIT      until t_create + 7200 s has passed: nothing is scored early.
  DECODE    getTransaction of each successful signature inside the window -> esm_event (stablecoin quote kept);
            not served after MAX_TRIES -> counted as not_served (coverage), never guessed.
  ANALYZE   insiders.esm.analyze_window -> esm_coin + esm_window_wallet (points from the window only).
  FUNDING   for every BIG wallet of the coin (newcoins.big: >= 5 SOL early, or early in >= 2 coins with >= 1 SOL):
            SOL >= 0.05 sent to it in the 72 h before its first buy (FUND_SIGS transactions) -> edges (research_sol);
            a watched wallet that funded it, or a non-exchange funder shared with a watched wallet = STRONG link
            (esm_link) -> independence LIKELY_SAME_ENTITY; only exchanges -> INSUFFICIENT_DATA; else INDEPENDENT
            (esm_flag INDEPENDENCE). Then DONE: the big wallets are on the watchlist (newcoins.SmartBook).
DISCOVER keeps only the signatures that can still be inside the window (the oldest page + 2 h), so a coin with a
million transactions does not fill the disk."""
from __future__ import annotations

import json
import os
import time

from insiders import esm as E
from insiders.scan import KNOWN_LABELS

PAGE = 1000
MAX_PAGES = int(os.environ.get("ESM_MAX_PAGES") or 1200)          # x 1000 signatures back to the creation
MAX_DECODE = int(os.environ.get("ESM_MAX_DECODE") or 60000)       # window transactions read at most per coin
MAX_CALLS_DAY = int(os.environ.get("ESM_MAX_CALLS_DAY") or 700000)  # RPC calls a day, all coins, Helius + public
HELIUS_MAX_DAY = int(os.environ.get("ESM_HELIUS_MAX_DAY") or 500000)  # Helius calls a day (user: 4 M credits, 5 days)


class Router:
    """Helius first (the server's HELIUS_API_KEY, never shown), at most HELIUS_MAX_DAY calls a UTC day; past that, or
    when Helius fails, the public RPC. Each call is metered by provider in rpc_usage (esm_helius / esm_public)."""

    def __init__(self, store, helius, public, now=time.time):
        self.s, self.helius, self.public, self.now = store, helius, public, now
        self.stat = {"helius": 0, "public": 0, "helius_errors": 0, "provider": "helius" if helius else "public"}

    def helius_today(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        return self.s.q("SELECT COALESCE(SUM(calls), 0) AS n FROM rpc_usage WHERE day=? AND worker='esm_helius'", (day,))[0]["n"]

    def __call__(self, method, params):
        if self.helius is not None and self.helius_today() < HELIUS_MAX_DAY:
            try:
                res = self.helius(method, params)
                self.s.usage("esm_helius", method, res)
                self.stat.update(helius=self.stat["helius"] + 1, provider="helius")
                return res
            except RuntimeError:
                self.s.usage("esm_helius", method, err=True)
                self.stat["helius_errors"] += 1
        res = self.public(method, params)
        self.s.usage("esm_public", method, res)
        self.stat.update(public=self.stat["public"] + 1, provider="public")
        return res
MAX_TRIES = 3
FUND_SIGS, FUND_MIN_SOL, FUND_WINDOW_S = 10, 0.05, 72 * 3600
TX_OPTS = {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def enabled() -> bool:
    return (os.environ.get("INSIDER_ESM") or "1") != "0"


class EsmWorker:
    def __init__(self, store, rpc, now=time.time, on=None, budget_per_step: int = 200):
        self.s, self.rpc, self.now = store, rpc, now
        self.on = enabled() if on is None else on
        self.budget = budget_per_step

    # -- queue (verified mints only; a ticker without a confirmed mint stays in esm_pending_mint)
    def add(self, mint: str, symbol: str | None = None) -> bool:
        m = self.s.q("SELECT verified FROM mints WHERE mint=?", (mint,))
        if not m or not m[0]["verified"]:
            return False
        self.s.x("INSERT OR IGNORE INTO esm_queue (mint, symbol, state, updated) VALUES (?,?,'DISCOVER',?)", (mint, symbol, self.now()))
        return True

    def intake(self) -> int:
        """Coins asked for on the page (esm_pending_mint REQUESTED) -> esm_queue; the request shows RUNNING."""
        n = 0
        for r in self.s.q("SELECT symbol, candidates FROM esm_pending_mint WHERE status='REQUESTED'"):
            try:
                mint = (json.loads(r["candidates"] or "[]") or [{}])[0].get("mint")
            except (ValueError, AttributeError):
                mint = None
            if not mint:
                continue
            self.s.x("INSERT OR IGNORE INTO esm_queue (mint, symbol, state, updated) VALUES (?,?,'DISCOVER',?)", (mint, r["symbol"], self.now()))
            self.s.x("UPDATE esm_pending_mint SET status='RUNNING', note=?, updated=? WHERE symbol=?",
                     ("đang truy vết: tìm giao dịch tạo coin", self.now(), r["symbol"]))
            n += 1
        return n

    def calls_today(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        return self.s.q("SELECT COALESCE(SUM(calls), 0) AS n FROM rpc_usage WHERE day=? AND worker='esm'", (day,))[0]["n"]

    def _call(self, mint, method, params):
        self.left -= 1
        self.s.x("UPDATE esm_queue SET calls=calls+1 WHERE mint=?", (mint,))
        try:
            res = self.rpc(method, params)
        except RuntimeError:
            self.s.usage("esm", method, err=True)
            raise
        self.s.usage("esm", method, res)
        return res

    def _request(self, q, status, note):
        """The page's request for this coin (if any) follows the queue."""
        self.s.x("UPDATE esm_pending_mint SET status=?, note=?, updated=? WHERE status IN ('REQUESTED', 'RUNNING') "
                 "AND (symbol=? OR candidates LIKE ?)", (status, note, self.now(), q["symbol"] or "", f"%{q['mint']}%"))

    def _set(self, mint, **kw):
        kw["updated"] = self.now()
        self.s.x(f"UPDATE esm_queue SET {', '.join(k + '=?' for k in kw)} WHERE mint=?", (*kw.values(), mint))
        if kw.get("state") == "INSUFFICIENT_DATA":
            q = self.s.q("SELECT mint, symbol FROM esm_queue WHERE mint=?", (mint,))[0]
            self._request(q, "REJECTED", "không truy vết được: " + (kw.get("note") or "thiếu dữ liệu"))
        elif kw.get("state") in ("CREATION", "WAIT", "DECODE", "ANALYZE", "FUNDING"):
            q = self.s.q("SELECT mint, symbol FROM esm_queue WHERE mint=?", (mint,))[0]
            self._request(q, "RUNNING", {"CREATION": "đã tới giao dịch tạo, đang kiểm tra", "WAIT": "chờ hết 120 phút đầu của coin",
                                         "DECODE": "đang giải mã giao dịch 120 phút đầu", "ANALYZE": "đang chấm ví",
                                         "FUNDING": "đang truy nguồn tiền của ví lớn"}[kw["state"]])

    def step(self) -> dict:
        """One bounded round over the queue. Returns {mint: state}. Does nothing at all when the worker is off."""
        if not self.on:
            return {}
        self.intake()
        if self.calls_today() >= MAX_CALLS_DAY:                   # the day's public RPC allowance is used: wait for tomorrow
            return {"_capped": "ESM_MAX_CALLS_DAY"}
        self.left, out = self.budget, {}
        for q in self.s.q("SELECT * FROM esm_queue WHERE state NOT IN ('DONE', 'INSUFFICIENT_DATA') ORDER BY updated"):
            if self.left <= 0:
                break
            fn = getattr(self, "_" + q["state"].lower())
            try:
                fn(q)
            except RuntimeError as e:                             # RPC refused / capped: retried next round, nothing lost
                self._set(q["mint"], note=f"lỗi RPC: {e}"[:200])
            out[q["mint"]] = self.s.q("SELECT state FROM esm_queue WHERE mint=?", (q["mint"],))[0]["state"]
        return out

    def _discover(self, q):
        mint, before = q["mint"], q["before_sig"]
        while self.left > 0:
            opts = {"limit": PAGE, **({"before": before} if before else {})}
            page = self._call(mint, "getSignaturesForAddress", [mint, opts]) or []
            with self.s.lock:
                self.s.db.executemany("INSERT OR IGNORE INTO esm_sig (mint, sig, block_time, err) VALUES (?,?,?,?)",
                                      [(mint, x["signature"], x.get("blockTime"), int(x.get("err") is not None)) for x in page])
                self.s.db.commit()
            pages = (q["pages"] or 0) + 1
            q["pages"] = pages
            if page:
                before = page[-1]["signature"]
                self._set(mint, before_sig=before, pages=pages, oldest_sig=before, oldest_ts=page[-1].get("blockTime"))
                if page[-1].get("blockTime"):                        # newer than the oldest + 2 h: can never be in the window
                    self.s.x("DELETE FROM esm_sig WHERE mint=? AND block_time > ?", (mint, page[-1]["blockTime"] + E.WIN + 120))
            if len(page) < PAGE:
                self._set(mint, state="CREATION")
                return
            if pages >= MAX_PAGES:
                self._set(mint, state="INSUFFICIENT_DATA", note=f"lịch sử > {MAX_PAGES} trang: chưa tới giao dịch tạo")
                return

    CREATION_TRIES = 25                                        # transactions of the oldest second checked for the creation

    def _creation(self, q):
        """The creation is among the transactions of the OLDEST second: snipers / bundles land in the same block as the
        create, so every one of them is tried, the one listed last by the RPC (the oldest) first."""
        mint = q["mint"]
        first = self.s.q("SELECT MIN(block_time) AS t FROM esm_sig WHERE mint=? AND err=0", (mint,))[0]["t"]
        cand = self.s.q("SELECT sig, block_time FROM esm_sig WHERE mint=? AND err=0 AND block_time=? ORDER BY rowid DESC LIMIT ?",
                        (mint, first, self.CREATION_TRIES)) if first is not None else []
        if not cand:
            self._set(mint, state="INSUFFICIENT_DATA", note="không có chữ ký thành công")
            return
        tried = set(json.loads(q.get("note") or "{}").get("tried", [])) if (q.get("note") or "").startswith("{") else set()
        for c in cand:
            if c["sig"] in tried:
                continue
            if self.left <= 0:
                self._set(mint, note=json.dumps({"tried": sorted(tried)}))
                return
            tx = self._call(mint, "getTransaction", [c["sig"], TX_OPTS])
            if not tx:
                self._set(mint, note=json.dumps({"tried": sorted(tried)}))     # not served: the same one next round
                return
            tried.add(c["sig"])
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
            ins = list(msg.get("instructions") or [])
            for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
                ins += inner.get("instructions") or []
            made = any(isinstance(i.get("parsed"), dict) and i["parsed"].get("type") in ("initializeMint", "initializeMint2")
                       and (i["parsed"].get("info") or {}).get("mint") == mint for i in ins)
            logs = " ".join((tx.get("meta") or {}).get("logMessages") or [])
            made = made or (PUMP in keys and mint in keys and ("Instruction: Create" in logs))
            if made:
                self._set(mint, state="WAIT", t_create=tx.get("blockTime"), creation_sig=c["sig"], creator=keys[0] if keys else None, note=None)
                return
        self._set(mint, state="INSUFFICIENT_DATA", note="chữ ký cũ nhất không phải giao dịch tạo mint")

    def _wait(self, q):
        if self.now() >= (q["t_create"] or 0) + E.WIN:
            self._set(q["mint"], state="DECODE")

    def _decode(self, q):
        from insiders.track import parse_mint_events
        mint, t0 = q["mint"], q["t_create"]
        total = self.s.q("SELECT COUNT(*) AS n FROM esm_sig WHERE mint=? AND err=0 AND block_time BETWEEN ? AND ?", (mint, t0, t0 + E.WIN))[0]["n"]
        if total > MAX_DECODE:
            self._set(mint, state="INSUFFICIENT_DATA", note=f"{total} giao dịch trong 120 phút đầu > giới hạn {MAX_DECODE}")
            return
        todo = self.s.q("SELECT sig FROM esm_sig WHERE mint=? AND err=0 AND state='todo' AND block_time BETWEEN ? AND ? "
                        "ORDER BY block_time LIMIT ?", (mint, t0, t0 + E.WIN, max(self.left, 0)))
        for r in todo:
            tx = self._call(mint, "getTransaction", [r["sig"], TX_OPTS])
            if not tx:
                self.s.x("UPDATE esm_sig SET tries=tries+1, state=CASE WHEN tries+1>=? THEN 'not_served' ELSE state END "
                         "WHERE mint=? AND sig=?", (MAX_TRIES, mint, r["sig"]))
                continue
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
            try:
                evs = parse_mint_events(tx, mint)
                state = "done"
            except Exception:                                     # recorded, never dropped silently
                evs, state = [], "decode_error"
            with self.s.lock:
                self.s.db.executemany("INSERT OR IGNORE INTO esm_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      [(mint, r["sig"], e["wallet"], e["kind"], e.get("sol"), str(e.get("tokens") or 0), e.get("quote"),
                                        e.get("quote_amt"), tx.get("slot"), tx.get("blockTime"), keys[0] if keys else None,
                                        (tx.get("meta") or {}).get("fee"), int(bool(e.get("signer")))) for e in evs])
                self.s.db.execute("UPDATE esm_sig SET state=? WHERE mint=? AND sig=?", (state, mint, r["sig"]))
                self.s.db.commit()
        left = self.s.q("SELECT COUNT(*) AS n FROM esm_sig WHERE mint=? AND err=0 AND state='todo' AND block_time BETWEEN ? AND ?",
                        (mint, t0, t0 + E.WIN))[0]["n"]
        if not left:
            self._set(mint, state="ANALYZE")

    def _analyze(self, q):
        mint, t0 = q["mint"], q["t_create"]
        sigs = self.s.q("SELECT sig, block_time, err, state FROM esm_sig WHERE mint=? AND block_time BETWEEN ? AND ?", (mint, t0, t0 + E.WIN))
        rows = {}
        for e in self.s.q("SELECT * FROM esm_event WHERE mint=? ORDER BY block_time", (mint,)):
            r = rows.setdefault(e["sig"], {"s": e["sig"], "t": e["block_time"], "slot": e["slot"], "payer": e["payer"], "ev": []})
            r["ev"].append({"wallet": e["wallet"], "kind": e["kind"], "sol": e["sol"], "tokens": e["tokens"], "signer": e["signer"],
                            **({"quote": e["quote"], "quote_amt": e["quote_amt"]} if e["quote"] else {})})
        a = E.analyze_window(list(rows.values()), t0, q["creator"], skip=set(KNOWN_LABELS))
        ok = [s for s in sigs if not s["err"]]
        cov = {"window_signatures": len(sigs), "window_successful": len(ok), "decoded": sum(1 for s in ok if s["state"] == "done"),
               "not_served": sum(1 for s in ok if s["state"] == "not_served"), "decode_errors": sum(1 for s in ok if s["state"] == "decode_error"),
               "unread": sum(1 for s in ok if s["state"] == "todo")}
        c = a["coin"]
        complete = cov["not_served"] == 0 and cov["decode_errors"] == 0 and cov["unread"] == 0
        with self.s.lock:
            db = self.s.db
            db.execute("INSERT OR REPLACE INTO esm_coin VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (mint, q["symbol"], "mints.verified", t0, q["creation_sig"], c["creator"], "SOL", c["p0"], c["p_15m"], c["p_60m"],
                        c["p_120m"], c["x_15m"], c["x_60m"], c["x_120m"], c["peak_x"], c["peak_min"], json.dumps(cov),
                        "DONE" if complete and c["x_120m"] is not None else "INSUFFICIENT_DATA",
                        None if complete else "cửa sổ chưa giải mã đủ", E.RULES_VERSION, "worker", self.now()))
            for w in a["wallets"]:
                db.execute("INSERT OR REPLACE INTO esm_window_wallet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (mint, w["wallet"], w["bucket"], w["first_buy_min"], w["first_buy_sig"], w["buys"], w["sells"], w["sol_in"],
                            w["sol_out"], w["stable_in"], w["stable_out"], w["avg_cost_sol_per_token"], w["rise_x"], json.dumps(w["roles"]),
                            w["window_result_sol"], w["window_result_pct"], int(w["bot_flag"]), w["status"], json.dumps(w["insufficient"], ensure_ascii=False),
                            E.points(w), E.RULES_VERSION))
            db.commit()
        self._set(mint, state="FUNDING", note=None if complete else "thiếu dữ liệu: xem coverage")

    # -- FUNDING: the big wallets of the coin, where their SOL came from (72 h before the first buy)
    def big_wallets(self, mint) -> list[dict]:
        from insiders.newcoins import big
        creators = {r["creator"] for r in self.s.q("SELECT creator FROM esm_coin WHERE creator IS NOT NULL")}
        cg = {r["wallet"] for r in self.s.q("SELECT wallet FROM esm_flag WHERE flag IN ('CREATOR_GROUP', 'CREATOR')")}
        out = []
        for r in self.s.q("SELECT wallet, first_buy_sig, first_buy_min FROM esm_window_wallet WHERE mint=? AND bucket IN ('0-15', '15-60')", (mint,)):
            w = r["wallet"]
            if w in creators or w in cg:
                continue
            rows = self.s.q("SELECT w.mint, w.bucket, w.sol_in, c.symbol, c.creator FROM esm_window_wallet w JOIN esm_coin c ON c.mint=w.mint "
                            "WHERE w.wallet=?", (w,))
            entries = [{"bucket": x["bucket"], "sol_in": x["sol_in"], "at_creation": x["creator"] == w} for x in rows]
            early = sorted({x["symbol"] for x in rows if x["bucket"] in ("0-15", "15-60")})
            if big({"entries": entries, "early_coins": early}):
                out.append(r)
        return out

    def _funding(self, q):
        from insiders.track import sol_and_token_in
        mint, t0 = q["mint"], q["t_create"]
        done = {r["wallet"] for r in self.s.q("SELECT wallet FROM esm_flag WHERE flag='FUNDING_READ' AND ref=?", (mint,))}
        watched = self.watched()
        for r in self.big_wallets(mint):
            w = r["wallet"]
            if w in done:
                continue
            if self.left < FUND_SIGS + 1:
                return                                               # next round
            t_buy = t0 + (r["first_buy_min"] or 0) * 60
            sigs = self._call(mint, "getSignaturesForAddress", [w, {"limit": FUND_SIGS, "before": r["first_buy_sig"]}]) or []
            funders = []
            for x in sigs:
                if x.get("err") is not None or (x.get("blockTime") and t_buy - x["blockTime"] > FUND_WINDOW_S):
                    continue
                tx = self._call(mint, "getTransaction", [x["signature"], TX_OPTS])
                for src, asset, amt in sol_and_token_in(tx or {}, w):
                    if asset == "SOL" and amt >= FUND_MIN_SOL:
                        funders.append(src)
                        self.s.add_edge(src, w, "SOL", amt, (tx or {}).get("blockTime"), x["signature"], "research_sol")
            self.s.x("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                     (w, "FUNDING_READ", mint, json.dumps({"funders": sorted(set(funders))}), self.now()))
            self._independence(w, mint, sorted(set(funders)), watched)
        self._set(mint, state="DONE")
        n = len(self.big_wallets(mint))
        self._request(q, "DONE", f"xong: {n} ví lớn đã vào danh sách theo dõi")

    def watched(self) -> set:
        from insiders.esm import load_wallet_db
        ws = set(((load_wallet_db() or {}).get("wallets") or {}))
        return ws | {r["wallet"] for r in self.s.q("SELECT wallet FROM esm_flag WHERE flag='FUNDING_READ'")}

    def _independence(self, w, mint, funders, watched):
        """Same rules as the research (entity.py): a direct transfer from a watched wallet, or a non-exchange funder shared
        with a watched wallet, is a STRONG link; exchange-only funding says nothing about the owner."""
        level, why = "INDEPENDENT", "đã truy nguồn tiền, không thấy liên kết mạnh"
        for f in funders:
            if f in watched and f != w:
                self.s.x("INSERT OR REPLACE INTO esm_link VALUES (?,?,?,?,?,?,?,?)",
                         (*sorted((w, f)), "direct_transfer", "STRONG", mint, None, json.dumps({"source": "worker"}), E.RULES_VERSION))
                level, why = "LIKELY_SAME_ENTITY", f"nhận SOL trực tiếp từ ví đang theo dõi {f}"
        own = [f for f in funders if not KNOWN_LABELS.get(f)]
        if level == "INDEPENDENT" and own:
            q = ",".join("?" * len(own))
            for e in self.s.q(f"SELECT DISTINCT src, dst FROM edges WHERE kind='research_sol' AND src IN ({q}) AND dst != ?", (*own, w)):
                if e["dst"] in watched:
                    self.s.x("INSERT OR REPLACE INTO esm_link VALUES (?,?,?,?,?,?,?,?)",
                             (*sorted((w, e["dst"])), "shared_funder", "STRONG", mint, None, json.dumps({"funder": e["src"], "source": "worker"}),
                              E.RULES_VERSION))
                    level, why = "LIKELY_SAME_ENTITY", f"cùng ví cấp vốn {e['src']} với ví đang theo dõi {e['dst']}"
                    break
        if level == "INDEPENDENT" and funders and not own:
            level, why = "INSUFFICIENT_DATA", "nguồn tiền chỉ từ sàn: không biết chủ"
        if not self.s.q("SELECT 1 FROM esm_flag WHERE wallet=? AND flag='INDEPENDENCE'", (w,)):     # the research level wins
            self.s.x("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                     (w, "INDEPENDENCE", level, json.dumps({"why": why, "funders": funders, "source": "worker"}, ensure_ascii=False), self.now()))
