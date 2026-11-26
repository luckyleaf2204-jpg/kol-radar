"""Wallet trace (READ-ONLY public chain data; nothing is signed or sent): start from ANY wallet and follow SOL and token
transfers both ways (who funded it, where its money / tokens went) over a bounded number of hops.

Reuses what the tracker already has, no parallel store:
  Store / edges      every flow found is an edge of the existing `edges` table (kind 'trace'; the UNIQUE (sig, src,
                     dst, asset) key makes a flow already found by the funding step the same row);
  wallet_info        the hub test (exchange / router / bot: >= 100 tx at >= 1 a minute) shared with the tracker: a hub
                     is shown but never expanded (it would pull in thousands of unrelated wallets);
  WalletHistory.ingest  the same decoder (watch.analyse) writes the traced wallets' buys / sells / sends / receives
                     into `wallet_trades` (the transaction history shown with the trace);
  rpc_usage / Budget metered calls (worker "trace") with a per-minute bucket, a per-job cap and a daily cap.
New tables (migration v8): trace_job (one trace) and trace_node (one wallet of a trace: hop, how it was reached, state: todo -> done / leaf (last layer: found, not
read) / skipped (exchange, service) / error (3 failed tries)).

Cross-reference (no network): every wallet of a trace is matched with the Early Smart Money data (entity / class /
rank, CREATOR / CREATOR_GROUP / COORDINATED flags, coins it created), the New Coins creators (DEV), the DEPTH / STANK /
INSIDERS cohorts (`wallets`) and the known exchange labels; the shortest money path from the root to each matched
wallet is listed with its transaction signatures.

Wording: a wallet reached by a transfer is a RELATED wallet with on-chain evidence, never a claim of common owner."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque

from insiders.scan import KNOWN_LABELS, on_curve
from insiders.track import WSOL, STABLES, Budget, _env_f, is_service, merge_edges

TRACE_VERSION = "trace-v1"
WORKER = "trace"
CALLS_PER_MIN = _env_f("INSIDER_TRACE_CALLS_PER_MIN", 60)
DAY_CAP = int(_env_f("INSIDER_TRACE_DAY_CAP", 20_000))
JOB_CAP = int(_env_f("INSIDER_TRACE_JOB_CAP", 1_500))        # calls one trace may spend
MAX_HOPS = 3
MAX_SIGS = 100
MAX_NODES = int(_env_f("INSIDER_TRACE_MAX_NODES", 150))
HUB_SIGS = 100                                               # the hub test reads this many signatures (one call)
HUB_TX_PER_MIN = 1.0
DIRECTIONS = ("both", "in", "out")
LAMPORTS = 1e9


def enabled_by_env() -> bool:
    return (os.environ.get("INSIDER_TRACE") or "0") == "1"


# ---------------------------------------------------------------------------------------------------- decoding
def _instructions(tx: dict) -> list[dict]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    ins = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ins += inner.get("instructions") or []
    return ins


def flows(tx: dict, wallet: str) -> list[dict]:
    """Plain transfers INTO and OUT OF `wallet` in one transaction:
      SOL    system transfer / createAccount / transferWithSeed instructions (top level and inner) with the wallet as
             source or destination (amount in SOL, exact lamports);
      token  balance deltas per owner and mint (pre / post token balances): the wallet's tokens going to, or coming
             from, another WALLET (an on-curve owner). Pools, curves and vaults are program accounts (off curve), so a
             swap is never taken for a transfer. WSOL and USDC / USDT are not tokens here.
      FEE    the fee payer of a transaction in which another wallet also signs or has its token balance changed (a trade
             made for it): payer -> that wallet, amount = the fee in SOL (a relation, not value moved).
    A failed transaction moves nothing. Each row: src, dst, asset ('SOL', 'FEE' or the mint), amount, dir ('in' | 'out')."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    out: list[dict] = []
    for i in _instructions(tx):
        p = i.get("parsed")
        if not (isinstance(p, dict) and p.get("type") in ("transfer", "createAccount", "transferWithSeed")):
            continue
        if i.get("program") not in (None, "system"):
            continue                                         # spl-token "transfer" has no lamports: handled below
        info = p.get("info") or {}
        src, dst = info.get("source"), info.get("destination") or info.get("newAccount")
        lam = int(info.get("lamports") or 0)
        if not (src and dst and lam) or src == dst:
            continue
        if src == wallet:
            out.append({"src": wallet, "dst": dst, "asset": "SOL", "amount": lam / LAMPORTS, "dir": "out"})
        elif dst == wallet:
            out.append({"src": src, "dst": wallet, "asset": "SOL", "amount": lam / LAMPORTS, "dir": "in"})
    # fee paid for someone else (Early Smart Money 'paid_by'): the fee payer (first account) signs a transaction in which
    # ANOTHER wallet signs too or has its token balance changed (a buy / sell made for it). Asset 'FEE', amount = the
    # fee in SOL: an on-chain relation, not value moved. Pools / vaults (off curve), exchanges and fee wallets never count.
    ks = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in ks]
    signers = {k.get("pubkey") for k in ks if isinstance(k, dict) and k.get("signer")}
    fee = int(meta.get("fee") or 0) / LAMPORTS
    moved: dict[str, int] = defaultdict(int)
    for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
        for b in rows or []:
            if b.get("owner") and b.get("mint") not in (WSOL, None, *STABLES):
                moved[(b["owner"], b["mint"])] += sign * int((b.get("uiTokenAmount") or {}).get("amount") or 0)
    touched = {o for (o, _), d in moved.items() if d} | signers
    fee_rel: list[dict] = []
    if keys:
        payer = keys[0]
        ok = lambda o: on_curve(o) and KNOWN_LABELS.get(o, (None, None))[1] not in ("exchange", "fee")  # noqa: E731
        if payer == wallet:
            fee_rel = [{"src": wallet, "dst": o, "asset": "FEE", "amount": fee, "dir": "out"} for o in sorted(touched) if o != wallet and ok(o)]
        elif wallet in touched and ok(payer):
            fee_rel = [{"src": payer, "dst": wallet, "asset": "FEE", "amount": fee, "dir": "in"}]
    by: dict[tuple[str, str], int] = defaultdict(int)
    dec: dict[str, int] = {}
    for rows, sign in ((meta.get("preTokenBalances"), -1), (meta.get("postTokenBalances"), 1)):
        for b in rows or []:
            m, o = b.get("mint"), b.get("owner")
            if not o or not m or m == WSOL or m in STABLES:
                continue
            amt = b.get("uiTokenAmount") or {}
            dec[m] = int(amt.get("decimals") or 0)
            by[(o, m)] += sign * int(amt.get("amount") or 0)
    mints = {m for (_, m) in by}
    for m in mints:
        mine = by.get((wallet, m), 0)
        if not mine:
            continue
        others = [(o, d) for (o, mm), d in by.items() if mm == m and o != wallet and d and (d < 0) == (mine > 0)]
        if any(not on_curve(o) for o, _ in others):
            continue                                         # a pool / vault on the other side: a swap, not a transfer
        left = abs(mine)
        for o, d in sorted(others, key=lambda x: -abs(x[1])):
            amt = min(left, abs(d))
            if amt <= 0:
                break
            left -= amt
            a = amt / 10 ** dec.get(m, 0)
            if mine > 0:
                out.append({"src": o, "dst": wallet, "asset": m, "amount": a, "dir": "in"})
            else:
                out.append({"src": wallet, "dst": o, "asset": m, "amount": a, "dir": "out"})
    linked = {(f["src"], f["dst"]) for f in out}
    out += [f for f in fee_rel if (f["src"], f["dst"]) not in linked and (f["dst"], f["src"]) not in linked]
    return out


# ---------------------------------------------------------------------------------------------------- worker
class Capped(Exception):
    pass


class Tracer:
    """Trace jobs on the shared Store. `rpc(method, params)` is the read-only client (watch.Rpc)."""

    def __init__(self, store, rpc, now=time.time, budget: Budget | None = None, day_cap: int | None = None):
        from insiders.wallets import WalletHistory
        self.s, self.rpc, self.now = store, rpc, now
        self.budget = budget or Budget(CALLS_PER_MIN)
        self.day_cap = DAY_CAP if day_cap is None else day_cap
        self.hist = WalletHistory(store, rpc=None, now=now, enabled=False)   # its decoder only (ingest)
        self.stat = {"steps": 0, "calls": 0, "errors": 0, "last_error": None, "safe_stop": None}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ jobs
    def start(self, root: str, hops: int = 2, direction: str = "both", min_sol: float = 0.1, sigs: int = 40,
              max_nodes: int = MAX_NODES, call_cap: int = JOB_CAP) -> dict:
        from insiders.track import valid_mint
        if not valid_mint(root):
            raise ValueError("wallet")
        if direction not in DIRECTIONS:
            raise ValueError("direction")
        hops = max(1, min(MAX_HOPS, int(hops)))
        sigs = max(5, min(MAX_SIGS, int(sigs)))
        min_sol = max(0.0, float(min_sol))
        t = self.now()
        with self.s.lock:
            cur = self.s.db.execute(
                "INSERT INTO trace_job (root, hops, direction, min_sol, sigs, max_nodes, call_cap, status, created, updated, version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (root, hops, direction, min_sol, sigs, int(max_nodes), int(call_cap), "running", t, t,
                                                   TRACE_VERSION))
            jid = cur.lastrowid
            self.s.db.execute("INSERT INTO trace_node (job, wallet, hop, state, updated) VALUES (?,?,0,'todo',?)", (jid, root, t))
            self.s.db.commit()
        return self.job_row(jid)

    def job_row(self, jid: int) -> dict | None:
        r = self.s.q("SELECT * FROM trace_job WHERE id=?", (jid,))
        return r[0] if r else None

    def jobs(self, limit: int = 30) -> list[dict]:
        return self.s.q("SELECT j.*, (SELECT COUNT(*) FROM trace_node n WHERE n.job=j.id) nodes, "
                        "(SELECT COUNT(*) FROM trace_node n WHERE n.job=j.id AND n.state='todo') todo "
                        "FROM trace_job j ORDER BY id DESC LIMIT ?", (limit,))

    def cancel(self, jid: int) -> bool:
        return self.s.x("UPDATE trace_job SET status='stopped', note='dừng bởi người dùng', updated=? WHERE id=? AND status='running'",
                        (self.now(), jid)) > 0

    # ------------------------------------------------------------ calls
    def _call(self, job: dict, method: str, params: list):
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        used = self.s.q("SELECT COALESCE(SUM(calls),0) n FROM rpc_usage WHERE worker=? AND day=?", (WORKER, day))[0]["n"]
        if used >= self.day_cap:
            self.stat["safe_stop"] = f"đạt trần ngày {self.day_cap} lượt"
            raise Capped(self.stat["safe_stop"])
        if (job["calls"] or 0) >= job["call_cap"]:
            raise Capped(f"đạt trần {job['call_cap']} lượt của lần truy vết")
        while not self.budget.take():                         # wait for the bucket: a wallet is never left half read
            time.sleep(0.2)
        self.stat["calls"] += 1
        job["calls"] = (job["calls"] or 0) + 1
        self.s.x("UPDATE trace_job SET calls=calls+1, updated=? WHERE id=?", (self.now(), job["id"]))
        try:
            res = self.rpc(method, params)
        except Exception:
            self.s.usage(WORKER, method, None, err=True)
            raise
        self.s.usage(WORKER, method, res)
        return res

    def _hub(self, job: dict, wallet: str, sigs: list[dict]) -> bool:
        """The shared wallet_info hub flag; computed from the signature page already read when unknown."""
        if KNOWN_LABELS.get(wallet, (None, None))[1] in ("exchange", "fee"):
            return True
        r = self.s.q("SELECT busy FROM wallet_info WHERE wallet=?", (wallet,))
        if r and r[0]["busy"] is not None:
            return bool(r[0]["busy"])
        ts = [x.get("blockTime") for x in sigs if x.get("blockTime")]
        busy = len(ts) >= HUB_SIGS and (max(ts) - min(ts)) / 60 <= len(ts) / HUB_TX_PER_MIN
        self.s.x("INSERT OR REPLACE INTO wallet_info (wallet, busy, checked, note) VALUES (?,?,?,?)",
                 (wallet, int(busy), self.now(), "trace"))
        return busy

    # ------------------------------------------------------------ one step = one wallet of one trace
    def step(self) -> int:
        """Expands the next wallet (lowest hop first) of the oldest running trace. Returns flows recorded."""
        with self.lock:
            job = (self.s.q("SELECT * FROM trace_job WHERE status='running' ORDER BY id LIMIT 1") or [None])[0]
            if not job:
                return 0
            node = (self.s.q("SELECT * FROM trace_node WHERE job=? AND state='todo' ORDER BY hop, rowid LIMIT 1", (job["id"],)) or [None])[0]
            if not node:
                self.s.x("UPDATE trace_job SET status='done', updated=? WHERE id=?", (self.now(), job["id"]))
                return 0
            self.stat["steps"] += 1
            try:
                return self._expand(job, node)
            except Capped as e:
                self.s.x("UPDATE trace_job SET status='capped', note=?, updated=? WHERE id=?", (str(e), self.now(), job["id"]))
                return 0
            except Exception as e:                           # an RPC outage: the wallet stays 'todo', retried next step
                from insiders.vip import redact
                self.stat["errors"] += 1
                self.stat["last_error"] = redact(f"{type(e).__name__}: {str(e)[:120]}")
                tries = (node["tries"] or 0) + 1
                self.s.x("UPDATE trace_node SET reason=?, tries=?, state=?, updated=? WHERE job=? AND wallet=?",
                         (self.stat["last_error"] + (f" (sau {tries} lượt)" if tries >= 3 else ""), tries,
                          "error" if tries >= 3 else "todo", self.now(), job["id"], node["wallet"]))
                return 0

    def _set_node(self, job: dict, w: str, **kw) -> None:
        kw["updated"] = self.now()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.s.x(f"UPDATE trace_node SET {cols} WHERE job=? AND wallet=?", (*kw.values(), job["id"], w))

    def _expand(self, job: dict, node: dict) -> int:
        w, hop = node["wallet"], node["hop"]
        if is_service(w) and hop > 0:
            self._set_node(job, w, state="skipped", reason="sàn / dịch vụ (nhãn đã biết): không mở rộng")
            return 0
        page = self._call(job, "getSignaturesForAddress", [w, {"limit": max(HUB_SIGS, job["sigs"])}]) or []
        hub = self._hub(job, w, page)
        ok = [x for x in page if x.get("err") is None][: job["sigs"]]
        n_flows, n_tx = 0, 0
        for x in ok:
            tx = self._call(job, "getTransaction", [x["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                     "maxSupportedTransactionVersion": 1}])
            if not tx:
                continue
            n_tx += 1
            self.hist.ingest(w, tx)                          # the wallet's trades (wallet_trades), same decoder as Gate B
            for f in flows(tx, w):
                if f["asset"] == "SOL" and f["amount"] < job["min_sol"]:
                    continue
                self.s.add_edge(f["src"], f["dst"], f["asset"], f["amount"], tx.get("blockTime"), x["signature"], "trace")
                n_flows += 1
                if hub and hop > 0:
                    continue                                 # a hub's counterparties are not followed
                other = f["dst"] if f["dir"] == "out" else f["src"]
                if job["direction"] != "both" and f["dir"] != job["direction"]:
                    continue
                if hop + 1 > job["hops"] or not on_curve(other):
                    continue
                cnt = self.s.q("SELECT COUNT(*) n FROM trace_node WHERE job=?", (job["id"],))[0]["n"]
                if cnt >= job["max_nodes"]:
                    continue
                self.s.x("INSERT OR IGNORE INTO trace_node (job, wallet, hop, via, via_sig, state, updated) VALUES (?,?,?,?,?,?,?)",
                         (job["id"], other, hop + 1, w, x["signature"],
                          "skipped" if is_service(other) else ("todo" if hop + 1 < job["hops"] else "leaf"), self.now()))
        ts = [x.get("blockTime") for x in ok if x.get("blockTime")]
        reason = None
        if hub:
            reason = "hub (≥ 100 giao dịch, ≥ 1 giao dịch/phút): hiển thị, không mở rộng" + (" (ví gốc vẫn đọc)" if hop == 0 else "")
        elif len(page) >= max(HUB_SIGS, job["sigs"]) and len(ok) >= job["sigs"]:
            reason = f"đọc {job['sigs']} giao dịch gần nhất (cũ hơn chưa đọc)"
        self._set_node(job, w, state="done", reason=reason, sigs_read=len(ok), tx_read=n_tx, flows=n_flows,
                       oldest_ts=min(ts) if ts else None, newest_ts=max(ts) if ts else None)
        return n_flows

    def run_forever(self, stop: threading.Event, every_s: float = 0.5) -> None:
        while not stop.is_set():
            try:
                busy = self.step()
            except Exception as e:                           # never kill the thread
                self.stat["last_error"] = f"{type(e).__name__}"
                busy = 0
            stop.wait(every_s if busy or self.s.q("SELECT 1 FROM trace_job WHERE status='running' LIMIT 1") else 5.0)

    # ------------------------------------------------------------ reading (no network)
    def annotate(self, wallets: list[str]) -> dict[str, dict]:
        """What the system already knows about each wallet: ESM entity / class / rank / flags / coins created, New Coins
        creator (DEV), cohort (DEPTH / STANK / INSIDERS), exchange / fee label, hub flag."""
        out = {w: {"tags": []} for w in wallets}
        if not wallets:
            return out
        marks = ",".join("?" * len(wallets))
        for r in self.s.q(f"SELECT wallet, entity, class FROM esm_wallet_entity WHERE wallet IN ({marks})", wallets):
            out[r["wallet"]].update(entity=r["entity"], esm_class=r["class"])
        for r in self.s.q(f"SELECT wallet, score FROM esm_rank WHERE wallet IN ({marks})", wallets):
            out[r["wallet"]]["esm_score"] = r["score"]
        for r in self.s.q(f"SELECT wallet, flag, ref FROM esm_flag WHERE wallet IN ({marks})", wallets):
            o = out[r["wallet"]]
            o.setdefault("flags", []).append(r["flag"] + (":" + r["ref"] if r["ref"] else ""))
            if r["flag"] in ("CREATOR", "CREATOR_GROUP"):
                o["tags"].append(r["flag"])
        for r in self.s.q(f"SELECT creator, symbol, mint FROM esm_coin WHERE creator IN ({marks})", wallets):
            o = out[r["creator"]]
            o.setdefault("creator_of", []).append(r["symbol"] or r["mint"][:6])
            o["tags"].append("CREATOR")
        for r in self.s.q(f"SELECT creator, COUNT(*) n, GROUP_CONCAT(symbol, ',') syms FROM nc_coin WHERE creator IN ({marks}) GROUP BY creator", wallets):
            o = out[r["creator"]]
            o["dev_coins"] = r["n"]
            o["dev_symbols"] = [s for s in (r["syms"] or "").split(",") if s][:8]
            o["tags"].append("DEV")
        for r in self.s.q(f"SELECT wallet, cohort, label FROM wallets WHERE wallet IN ({marks})", wallets):
            out[r["wallet"]].update(cohort=r["cohort"], cohort_label=r["label"])
            out[r["wallet"]]["tags"].append(r["cohort"])
        for r in self.s.q(f"SELECT wallet, busy FROM wallet_info WHERE wallet IN ({marks})", wallets):
            if r["busy"]:
                out[r["wallet"]]["hub"] = True
                out[r["wallet"]]["tags"].append("HUB")
        for w in wallets:
            lab = KNOWN_LABELS.get(w)
            if lab:
                out[w].update(label=lab[0], label_kind=lab[1])
                out[w]["tags"].append(lab[1].upper())
            if out[w].get("entity"):
                out[w]["tags"].append(out[w]["entity"])
            out[w]["tags"] = sorted(set(out[w]["tags"]))
        return out

    def view(self, jid: int, focus: str | None = None, history_limit: int = 300) -> dict | None:
        job = self.job_row(jid)
        if not job:
            return None
        nodes = self.s.q("SELECT * FROM trace_node WHERE job=? ORDER BY hop, wallet", (jid,))
        members = {n["wallet"] for n in nodes}
        marks = ",".join("?" * len(members))
        raw = self.s.q(f"SELECT * FROM edges WHERE hidden=0 AND src IN ({marks}) AND dst IN ({marks})", (*members, *members))
        # flows of the expanded wallets to wallets that were not added (min SOL, node cap, hub) stay visible as leaves
        done = [n["wallet"] for n in nodes if n["state"] == "done"]
        if done:
            dm = ",".join("?" * len(done))
            raw += [e for e in self.s.q(f"SELECT * FROM edges WHERE hidden=0 AND kind='trace' AND (src IN ({dm}) OR dst IN ({dm}))",
                                        (*done, *done)) if not (e["src"] in members and e["dst"] in members)]
        raw = [e for e in raw if e["asset"] != "SOL" or (e["amount"] or 0) >= job["min_sol"]]
        merged = list(merge_edges(raw).values())
        allw = sorted(members | {m["src"] for m in merged} | {m["dst"] for m in merged})
        ann = self.annotate(allw)
        hop = {n["wallet"]: n["hop"] for n in nodes}
        edges = [{"src": m["src"], "dst": m["dst"], "asset": m["asset"], "amount": round(m["amount"], 6), "n": m["n"],
                  "first": m["first"], "last": m["ts"], "sigs": m["sigs"]} for m in merged]
        flows_ = sorted(({"sig": e["sig"], "ts": e["ts"], "src": e["src"], "dst": e["dst"], "asset": e["asset"],
                          "amount": e["amount"], "kind": e["kind"]} for e in raw), key=lambda e: -(e["ts"] or 0))
        who = focus if focus in members else job["root"]
        hist = self.s.q("SELECT * FROM wallet_trades WHERE wallet=? ORDER BY ts DESC LIMIT ?", (who, history_limit))
        return {"job": job, "nodes": [{**n, **ann.get(n["wallet"], {})} for n in nodes],
                "leaves": [{"wallet": w, **ann.get(w, {})} for w in allw if w not in members],
                "edges": edges, "flows": flows_[:1000], "flows_total": len(flows_),
                "matches": self.matches(job["root"], merged, ann, hop), "history": {"wallet": who, "trades": hist},
                "rpc_calls": job["calls"]}

    def matches(self, root: str, merged: list[dict], ann: dict, hop: dict) -> list[dict]:
        """Every wallet of the trace the system already knows (DEV / creator, cohort, ESM entity, exchange) with the
        shortest money path from the root (edges followed either way, each step carrying its signatures)."""
        adj: dict[str, list[tuple[str, dict, str]]] = defaultdict(list)
        for m in merged:
            adj[m["src"]].append((m["dst"], m, "out"))
            adj[m["dst"]].append((m["src"], m, "in"))
        prev: dict[str, tuple[str, dict, str] | None] = {root: None}
        dq = deque([root])
        while dq:
            a = dq.popleft()
            for b, m, d in adj.get(a, []):
                if b not in prev:
                    prev[b] = (a, m, d)
                    dq.append(b)
        out = []
        for w, a in ann.items():
            if w == root or not a.get("tags") or w not in prev:
                continue
            path, cur = [], w
            while prev.get(cur):
                p, m, d = prev[cur]
                path.append({"from": m["src"], "to": m["dst"], "asset": m["asset"], "amount": round(m["amount"], 6),
                             "n": m["n"], "sigs": m["sigs"], "dir": d})
                cur = p
            path.reverse()
            out.append({"wallet": w, "hop": hop.get(w, len(path)), "tags": a["tags"], "entity": a.get("entity"),
                        "cohort": a.get("cohort"), "creator_of": a.get("creator_of"), "dev_coins": a.get("dev_coins"),
                        "label": a.get("label"), "path": path})
        return sorted(out, key=lambda x: (len(x["path"]), x["wallet"]))

    def status(self) -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        used = self.s.q("SELECT COALESCE(SUM(calls),0) n FROM rpc_usage WHERE worker=? AND day=?", (WORKER, day))[0]["n"]
        return {**self.stat, "version": TRACE_VERSION, "calls_today": used, "day_cap": self.day_cap, "job_cap": JOB_CAP,
                "running": self.s.q("SELECT COUNT(*) n FROM trace_job WHERE status='running'")[0]["n"]}


def main(argv=None) -> int:
    """CLI (development): python -m insiders.trace <db> <wallet> [hops] [direction] [min_sol] [sigs]
    runs one trace to the end on the public RPC (or INSIDER_TRACE_RPC) and prints the JSON view."""
    import sys
    from insiders.track import Store
    from insiders.watch import Rpc
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2:
        print(main.__doc__)
        return 2
    st = Store(a[0])
    url = os.environ.get("INSIDER_TRACE_RPC")
    tr = Tracer(st, Rpc(url=url, gap_s=0.35) if url else Rpc(gap_s=0.35))
    job = tr.start(a[1], int(a[2]) if len(a) > 2 else 2, a[3] if len(a) > 3 else "both",
                   float(a[4]) if len(a) > 4 else 0.1, int(a[5]) if len(a) > 5 else 40)
    while tr.job_row(job["id"])["status"] == "running":
        if not tr.step():
            time.sleep(0.2)
    print(json.dumps(tr.view(job["id"]), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
