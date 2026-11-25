"""Early Smart Money worker: the 120-minute window of each queued, VERIFIED mint. Prepared, OFF by default.

Runs only when INSIDER_ESM=1 (or enabled=True in a test); nothing starts it in the service: no thread, no schedule,
no server connection. Read-only on chain, never signs or sends. Every RPC call goes through `rpc` (the metered,
capped caller of Gate B in production) and is counted per coin; a per-step budget stops it.

Stages per mint (esm_queue.state):
  DISCOVER  page getSignaturesForAddress back to the oldest signature (the creation); signatures kept in esm_sig.
            More than MAX_PAGES pages -> INSUFFICIENT_DATA "chưa tới giao dịch tạo" (no getTransactionsForAddress).
  CREATION  read the oldest successful signature: must create the mint (initializeMint / pump Create) -> t_create.
  WAIT      until t_create + 7200 s has passed: nothing is scored early.
  DECODE    getTransaction of each successful signature inside the window -> esm_event (stablecoin quote kept);
            not served after MAX_TRIES -> counted as not_served (coverage), never guessed.
  ANALYZE   insiders.esm.analyze_window -> esm_coin + esm_window_wallet (points from the window only) -> DONE.
Funding / owner groups / ranking are NOT run here: they need an approved request budget (see docs)."""
from __future__ import annotations

import json
import os
import time

from insiders import esm as E
from insiders.scan import KNOWN_LABELS

PAGE = 1000
MAX_PAGES = 200
MAX_TRIES = 3
TX_OPTS = {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def enabled() -> bool:
    return os.environ.get("INSIDER_ESM") == "1"


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

    def _call(self, mint, method, params):
        self.left -= 1
        self.s.x("UPDATE esm_queue SET calls=calls+1 WHERE mint=?", (mint,))
        return self.rpc(method, params)

    def _set(self, mint, **kw):
        kw["updated"] = self.now()
        self.s.x(f"UPDATE esm_queue SET {', '.join(k + '=?' for k in kw)} WHERE mint=?", (*kw.values(), mint))

    def step(self) -> dict:
        """One bounded round over the queue. Returns {mint: state}. Does nothing at all when the worker is off."""
        if not self.on:
            return {}
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
            if len(page) < PAGE:
                self._set(mint, state="CREATION")
                return
            if pages >= MAX_PAGES:
                self._set(mint, state="INSUFFICIENT_DATA", note=f"lịch sử > {MAX_PAGES} trang: chưa tới giao dịch tạo")
                return

    def _creation(self, q):
        mint = q["mint"]
        cand = self.s.q("SELECT sig, block_time FROM esm_sig WHERE mint=? AND err=0 ORDER BY block_time, sig LIMIT 1", (mint,))
        if not cand:
            self._set(mint, state="INSUFFICIENT_DATA", note="không có chữ ký thành công")
            return
        tx = self._call(mint, "getTransaction", [cand[0]["sig"], TX_OPTS])
        if not tx:
            self._set(mint, note="giao dịch tạo chưa được phục vụ")
            return
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
        ins = list(msg.get("instructions") or [])
        for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
            ins += inner.get("instructions") or []
        made = any(isinstance(i.get("parsed"), dict) and i["parsed"].get("type") in ("initializeMint", "initializeMint2")
                   and (i["parsed"].get("info") or {}).get("mint") == mint for i in ins)
        made = made or (PUMP in keys and "Instruction: Create" in " ".join((tx.get("meta") or {}).get("logMessages") or []))
        if not made:
            self._set(mint, state="INSUFFICIENT_DATA", note="chữ ký cũ nhất không phải giao dịch tạo mint")
            return
        self._set(mint, state="WAIT", t_create=tx.get("blockTime"), creation_sig=cand[0]["sig"], creator=keys[0] if keys else None)

    def _wait(self, q):
        if self.now() >= (q["t_create"] or 0) + E.WIN:
            self._set(q["mint"], state="DECODE")

    def _decode(self, q):
        from insiders.track import parse_mint_events
        mint, t0 = q["mint"], q["t_create"]
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
        self._set(mint, state="DONE", note=None if complete else "thiếu dữ liệu: xem coverage")
