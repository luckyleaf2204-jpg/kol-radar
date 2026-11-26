"""Early Smart Money (120-minute window) storage: import of the research results into the v5 esm_* tables and the
read side for the API / page. No network, no signing; DEPTH / STANK / INSIDERS (table `wallets`) are only read.

Rules (approved): window = t_create .. +7200 s, buckets 0-15 / 15-60 / 60-120; score only from the window;
after-2h outcomes in esm_outcome, never in esm_rank; CREATOR_GROUP out of the ranking (data kept);
COORDINATED_PATTERN is a behaviour label, not an ownership verdict; SOL-less data -> INSUFFICIENT_DATA."""
from __future__ import annotations

import json
import time
from pathlib import Path

RULES_VERSION = "esm-120m-v1"
SCORE_RULE = "0-15 phút = 2, 15-60 = 1; tăng sau khi vào (giá phút 120 / giá vốn) >= 2x +1, >= 5x +2; GOOD +1; mỗi chủ thể tính 1 lần mỗi coin"
STRONG = {"direct_transfer", "shared_funder", "same_tx", "paid_by", "distribution"}


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _read(folder: Path, name: str):
    f = folder / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def import_research(store, folder: str | Path, verified: dict | None = None, now=time.time) -> dict:
    """Load the research files into esm_*. Idempotent (same files -> same rows).
    V2 network trace (network_trace.json / tierA_review.json, when present): its SOL / token edges go into the
    existing graph table `edges` with kind research_sol / research_token (the rows of those two kinds are replaced
    on every import; live tracker edges are never touched), the PASS_THROUGH / BATCH_SPLIT flags (entities.json)
    and the tier-A independence level (INDEPENDENCE) into esm_flag. No other table is touched."""
    folder, ts, n = Path(folder), now(), {}
    verified = verified if verified is not None else (_read(folder, "verified.json") or {})
    ent = _read(folder, "entities.json") or {"classes": {}, "entity_of": {}, "pairs": []}
    esm = _read(folder, "early_smart_money.json") or {"wallets": [], "creator_group": {"excluded_wallets": []}}
    coord = _read(folder, "coordinated.json") or {"groups": []}
    outc = _read(folder, "outcomes.json") or {"coins": {}}
    macca = _read(folder, "macca_window.json")
    cand = _read(folder, "candidates_MOONX_COGE.json") or {}
    per_coin = {}
    for r in esm["wallets"] + esm.get("creator_group", {}).get("excluded_wallets", []):
        for sym, c in r["per_coin"].items():
            per_coin[(sym, r["wallet"])] = c
    with store.lock:
        db = store.db
        db.execute("BEGIN")
        try:
            coins = {}
            for f in sorted(folder.glob("window_*.json")):
                w = json.loads(f.read_text(encoding="utf-8"))
                c, sym = w["coin"], w["coin"]["symbol"]
                coins[sym] = c["mint"]
                quote = "USDC" if sym == "MACCA" and macca else "SOL"
                status = "DONE" if c.get("x_120m") is not None else "INSUFFICIENT_DATA"
                note = macca.get("score_note") if quote == "USDC" else None
                xs = macca["x_usdc"] if quote == "USDC" else {}
                db.execute("INSERT OR REPLACE INTO esm_coin VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (c["mint"], sym, verified.get(sym), c["t_create"], c.get("creation_sig"), c.get("creator"), quote,
                            c.get("p0"), c.get("p_15m"), c.get("p_60m"), c.get("p_120m"),
                            c.get("x_15m") or xs.get("15m"), c.get("x_60m") or xs.get("60m"), c.get("x_120m") or xs.get("120m"),
                            c.get("peak_x"), c.get("peak_min"), _j(w.get("coverage")), status, note, RULES_VERSION, "research/seven_coins", ts))
                rows = w["wallets"]
                if quote == "USDC":                              # MACCA: the USDC buyers (SOL score INSUFFICIENT_DATA)
                    rows = [{"wallet": x["wallet"], "bucket": x["bucket"], "first_buy_min": x["first_buy_min"], "first_buy_sig": x["first_buy_sig"],
                             "buys": x["buys"], "sells": 0, "sol_in": None, "sol_out": None, "stable_in": x["usdc_in"], "stable_out": 0,
                             "avg_cost_sol_per_token": None, "roles": [], "window_result_sol": None, "window_result_pct": None,
                             "bot_flag": False, "status": "INSUFFICIENT_DATA", "insufficient": ["mua bằng USDC: không có giá SOL"]} for x in macca["wallets"]]
                for x in rows:
                    pc = per_coin.get((sym, x["wallet"]), {})
                    db.execute("INSERT OR REPLACE INTO esm_window_wallet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (c["mint"], x["wallet"], x["bucket"], x["first_buy_min"], x.get("first_buy_sig"), x.get("buys"), x.get("sells"),
                                x.get("sol_in"), x.get("sol_out"), x.get("stable_in"), x.get("stable_out"), x.get("avg_cost_sol_per_token"),
                                pc.get("rise_after_entry_x"), _j(x.get("roles") or []), x.get("window_result_sol"), x.get("window_result_pct"),
                                int(bool(x.get("bot_flag"))), x.get("status"), _j(x.get("insufficient") or []),
                                pc.get("points") if quote == "SOL" else None, RULES_VERSION))
            n["coins"] = len(coins)
            buyers = {r[0] for r in db.execute("SELECT wallet FROM esm_window_wallet")}
            for p in ent["pairs"]:
                for sig_type, ev in p["signals"].items():
                    db.execute("INSERT OR REPLACE INTO esm_link VALUES (?,?,?,?,?,?,?,?)",
                               (p["a"], p["b"], sig_type, "STRONG" if sig_type in STRONG else "MEDIUM", ev.get("coin"), ev.get("sig"), _j(ev), RULES_VERSION))
            n["links"] = sum(len(p["signals"]) for p in ent["pairs"])
            for w, cls in ent["classes"].items():
                db.execute("INSERT OR REPLACE INTO esm_wallet_entity VALUES (?,?,?,?,?)",
                           (w, ent["entity_of"].get(w), cls, int(w in buyers), RULES_VERSION))
            mf = _read(folder, "macca_funding.json")
            if macca and mf:                                     # MACCA (USDC): approved = POSSIBLE_LINK where the funding links
                linked = {}
                for src, g in (mf.get("shared_funders") or {}).items():
                    for w in g["wallets"]:
                        linked[w] = src
                        db.execute("INSERT OR REPLACE INTO esm_link VALUES (?,?,?,?,?,?,?,?)",
                                   (w, src, "shared_funder_busy_hub", "MEDIUM", "MACCA", None,
                                    _j({"funded_wallets": g["n"], "busy_hub": g.get("busy_hub"),
                                        "note": "busy hub: không tính là tín hiệu mạnh"}), RULES_VERSION))
                for x in macca["wallets"]:
                    if x["wallet"] not in ent["classes"]:
                        db.execute("INSERT OR REPLACE INTO esm_wallet_entity VALUES (?,?,?,?,?)",
                                   (x["wallet"], None, "POSSIBLE_LINK" if x["wallet"] in linked else "INSUFFICIENT_DATA", 1, RULES_VERSION))
            cg = esm.get("creator_group", {})
            members = set(cg.get("members") or [])               # the approved group (creator + verified link), from merge
            creators = cg.get("creators") or {}
            flagged = 0
            db.execute("DELETE FROM esm_flag WHERE flag IN ('CREATOR_GROUP', 'CREATOR', 'COORDINATED_PATTERN')")   # re-import = same rows
            for w in sorted(members):
                e = ent["entity_of"].get(w)
                db.execute("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                           (w, "CREATOR_GROUP", e, _j({"creator_of": creators.get(w), "entity": e, "linked_creators": cg.get("linked_creators")}), ts))
                flagged += 1
            for w, sym in creators.items():                      # every creator is stored; a label only, never a removal
                db.execute("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                           (w, "CREATOR", sym, _j({"creator_of": sym, "in_creator_group": w in members}), ts))
                flagged += 1
            for g in coord["groups"]:
                for w in g["wallets"]:
                    db.execute("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                               (w, "COORDINATED_PATTERN", g["id"], _j({k: v for k, v in g.items() if k != "wallets"}), ts))
                    flagged += 1
            db.execute("DELETE FROM esm_flag WHERE flag IN ('PASS_THROUGH', 'BATCH_SPLIT', 'INDEPENDENCE')")
            for w, fl in (ent.get("flags") or {}).items():
                for f in fl:
                    ref = f.get("dst") if f["flag"] == "PASS_THROUGH" else f.get("source")
                    db.execute("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)", (w, f["flag"], ref, _j(f), ts))
                    flagged += 1
            review = _read(folder, "tierA_review.json")
            for r in (review or {}).get("wallets") or []:
                db.execute("INSERT OR REPLACE INTO esm_flag VALUES (?,?,?,?,?)",
                           (r["wallet"], "INDEPENDENCE", r["independence"],
                            _j({"group": r["group"], "group_size": r["group_size"], "funders_hop1": r.get("funders_hop1"),
                                "funders_exchange": r.get("funders_exchange"), "first_buy_notes": r.get("first_buy_notes"),
                                "note": "mức độc lập (bằng chứng), không phải kết luận danh tính"}), ts))
                flagged += 1
            n["flags"] = flagged
            trace = _read(folder, "network_trace.json")
            if trace:
                db.execute("DELETE FROM edges WHERE kind IN ('research_sol', 'research_token')")
                ne = 0
                for e in trace["edges"]:
                    if e["src"] and e["dst"] and e["src"] != e["dst"]:
                        ne += db.execute("INSERT OR IGNORE INTO edges (src, dst, asset, amount, ts, sig, kind) VALUES (?,?,?,?,?,?,?)",
                                         (e["src"], e["dst"], e["asset"], e["amount"], e["ts"], e["sig"],
                                          "research_sol" if e["asset"] == "SOL" else "research_token")).rowcount
                n["edges"] = ne
            for sym, o in outc["coins"].items():
                mint = coins.get(sym)
                for h in ("6h", "24h"):
                    x = o.get(h) or {}
                    db.execute("INSERT OR REPLACE INTO esm_outcome VALUES (?,?,?,?,?,?,?,?)",
                               (mint, h, x.get("price"), x.get("x_vs_open"), x.get("x_vs_120m"), x.get("trades"),
                                x.get("status") or "OK", x.get("why") or x.get("note")))
            db.execute("DELETE FROM esm_rank WHERE rules_version=?", (RULES_VERSION,))
            for r in esm["wallets"]:                             # the ranking: CREATOR_GROUP left out here AND in ranking()
                if "CREATOR_GROUP" in (r.get("flags") or []) or r["wallet"] in members:
                    continue
                db.execute("INSERT INTO esm_rank VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (RULES_VERSION, r["wallet"], r["score"], _j(r["coins"]), _j(r["early_coins"]), r["avg_entry_min"], r["class"],
                            r.get("entity"), _j(r.get("flags") or []), _j(r.get("insufficient") or []), ts))
            n["ranked"] = len(esm["wallets"])
            for tk in sorted({c["ticker"] for c in cand.values()} & set(coins)):   # its mint is verified now: the row is kept,
                db.execute("UPDATE esm_pending_mint SET status='VERIFIED', note=?, updated=? WHERE symbol=?",   # marked resolved
                           (f"mint đã xác minh: {coins[tk]} (cửa sổ 120 phút đã nạp)", ts, tk))
            for sym, mint in coins.items():                      # a coin asked for on the page is researched now
                db.execute("UPDATE esm_pending_mint SET status='DONE', note=?, updated=? WHERE status IN ('REQUESTED', 'RUNNING') "
                           "AND (symbol=? OR candidates LIKE ?)", (f"đã truy vết, cửa sổ 120 phút đã nạp ({mint})", ts, sym, f"%{mint}%"))
            for tk in sorted({c["ticker"] for c in cand.values()} - set(coins)):
                cs = [{"mint": c["mint"], "name": c.get("name"), "t_create_utc": c.get("t_create_utc"),
                       "overlap_early": len(c.get("overlap_verified_early") or {}), "first15_buyers": c.get("first15_buyers")}
                      for c in cand.values() if c["ticker"] == tk]
                db.execute("INSERT OR REPLACE INTO esm_pending_mint VALUES (?,?,?,?,?)",
                           (tk, "NEEDS_MINT_VERIFICATION", _j(sorted(cs, key=lambda c: -c["overlap_early"])),
                            "chưa có nguồn gốc xác nhận mint; ứng viên chỉ là bằng chứng", ts))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return n


def ranking(store, limit: int = 100, min_early_coins: int = 1) -> list[dict]:
    """The Early Smart Money ranking. CREATOR_GROUP is excluded again here (defence in depth)."""
    rows = store.q("SELECT r.* FROM esm_rank r WHERE r.rules_version=? AND NOT EXISTS "
                   "(SELECT 1 FROM esm_flag f WHERE f.wallet=r.wallet AND f.flag='CREATOR_GROUP') ", (RULES_VERSION,))
    out = []
    for r in rows:
        for k in ("coins", "early_coins", "flags", "insufficient"):
            r[k] = json.loads(r[k] or "[]")
        if len(r["early_coins"]) >= min_early_coins:
            out.append(r)
    out.sort(key=lambda r: (-len(r["early_coins"]), -r["score"], r["avg_entry_min"]))
    cohort = {x["wallet"]: x["cohort"] for x in store.q("SELECT wallet, cohort FROM wallets")}
    for r in out:
        r["cohort"] = cohort.get(r["wallet"])                   # DEPTH / STANK / INSIDERS: reference only
    return out[:limit]


def coins(store) -> list[dict]:
    rows = store.q("SELECT * FROM esm_coin ORDER BY t_create")
    for r in rows:
        r["coverage"] = json.loads(r["coverage"] or "null")
        r["outcomes"] = store.q("SELECT horizon, price, x_vs_open, x_vs_120m, trades, status, why FROM esm_outcome WHERE mint=?", (r["mint"],))
        r["buckets"] = store.q("SELECT bucket, COUNT(*) AS buyers, ROUND(SUM(COALESCE(sol_in, 0)), 3) AS sol_in, "
                               "ROUND(SUM(COALESCE(stable_in, 0)), 2) AS stable_in FROM esm_window_wallet WHERE mint=? GROUP BY bucket", (r["mint"],))
    return rows


def flags(store, flag: str) -> list[dict]:
    rows = store.q("SELECT * FROM esm_flag WHERE flag=? ORDER BY ref, wallet", (flag,))
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "null")
    return rows


def pending_mints(store) -> list[dict]:
    rows = store.q("SELECT * FROM esm_pending_mint WHERE status='NEEDS_MINT_VERIFICATION' ORDER BY symbol")
    for r in rows:
        r["candidates"] = json.loads(r["candidates"] or "[]")
    return rows


# ---------------------------------------------------------------------------------------------- window analysis
WIN = 7200
MARKS = (900, 3600, 7200)


def bucket(off: float) -> str:
    return "0-15" if off < 900 else "15-60" if off < 3600 else "60-120"


def analyze_window(rows: list[dict], t0: float, creator: str | None = None, dec: int = 6, skip: set | None = None) -> dict:
    """One coin's 120-minute window from decoded rows {s, t, slot, payer, ev: [events]} (successful transactions only).
    Same rules as research/seven_coins/analyze_window.py (approved 2026-11-25). Nothing after t0 + 7200 is read."""
    import statistics
    from collections import defaultdict

    from insiders.wallets import pair_pnl
    skip = skip or set()
    rows = sorted((r for r in rows if r.get("t") is not None and t0 <= r["t"] <= t0 + WIN and not r.get("missing")),
                  key=lambda r: (r["t"], r.get("slot") or 0))
    by, trades = defaultdict(list), []
    for r in rows:
        for e in r.get("ev") or []:
            w = e.get("wallet")
            if not w or w in skip:
                continue
            if e["kind"] == "CREATE" and creator is None:
                creator = w
            by[w].append({**e, "ts": r["t"], "off": r["t"] - t0, "sig": r["s"], "slot": r.get("slot"), "payer": r.get("payer")})
            q = int(e.get("tokens") or 0)
            if e["kind"] in ("BUY", "SELL", "CREATE") and (e.get("sol") or 0) > 0 and q > 0:
                trades.append((r["t"] - t0, e["sol"] / (q / 10 ** dec)))
    prices = [p for _, p in trades]
    p0 = statistics.median(prices[:20]) if len(prices) >= 3 else None

    def p_at(mark):
        before = [p for off, p in trades if off <= mark]
        return statistics.median(before[-10:]) if before else None
    marks = {f"p_{m // 60}m": p_at(m) for m in MARKS}
    peak, t_peak, roll = None, None, []
    for off, p in trades:
        roll.append(p)
        m = statistics.median(roll[-10:])
        if peak is None or m > peak:
            peak, t_peak = m, off
    p_end = marks["p_120m"]
    x = lambda v: round(v / p0, 3) if p0 and v else None          # noqa: E731
    coin = {"t_create": t0, "creator": creator, "p0": p0, **marks, "x_15m": x(marks["p_15m"]), "x_60m": x(marks["p_60m"]),
            "x_120m": x(p_end), "peak_x": x(peak), "peak_min": round(t_peak / 60, 1) if t_peak is not None else None,
            "priced_trades": len(trades)}
    wallets = []
    for w, evs in by.items():
        sol_buys = [e for e in evs if e["kind"] in ("BUY", "CREATE") and (e.get("sol") or 0) > 0]
        st_buys = [e for e in evs if e["kind"] == "RECEIVE" and e.get("quote")]
        st_sells = [e for e in evs if e["kind"] == "SEND" and e.get("quote")]
        buys = sorted(sol_buys + st_buys, key=lambda e: e["off"])
        sells = sorted([e for e in evs if e["kind"] == "SELL"] + st_sells, key=lambda e: e["off"])
        if not buys:
            continue
        pnl = pair_pnl([{"kind": e["kind"], "tokens": e.get("tokens"), "sol": e.get("sol"), "ts": e["ts"], "sig": e["sig"]} for e in evs],
                       (p_end / 10 ** dec, "giá cuối cửa sổ (ước tính)") if p_end else None)
        bought = sum(int(e.get("tokens") or 0) for e in buys)
        bought_sol = sum(int(e.get("tokens") or 0) for e in sol_buys)
        sold = sum(int(e.get("tokens") or 0) for e in sells)
        held = int(pnl["tok_left"])
        sol_in, sol_out = pnl["sol_in"], pnl["sol_out"]
        first = buys[0]["off"]
        holds = [s["off"] - max(b["off"] for b in buys if b["off"] <= s["off"]) for s in sells if any(b["off"] <= s["off"] for b in buys)]
        mtm = pnl["unrealized_sol"]
        result = pnl["realized_sol"] + (mtm or 0) if mtm is not None or held == 0 else None
        roles = []
        if len(buys) >= 2 and sol_in - sol_out >= 1 and not sells:
            roles.append("ACCUMULATE")
        if bought and sold >= 0.5 * bought:
            roles.append("EARLY_SELL")
        if bought and held >= 0.5 * bought:
            roles.append("HOLD")
        if result is not None and sol_in and result >= 0.5 * sol_in:
            roles.append("GOOD")
        insuff = []
        if st_buys or st_sells:
            result = None
            insuff.append("giao dịch định giá bằng " + "/".join(sorted({e["quote"] for e in st_buys + st_sells}))
                          + f" ({len(st_buys)} mua, {len(st_sells)} bán): không có giá SOL")
            roles = [r for r in roles if r != "GOOD"]
        if pnl["status"] != "OK":
            insuff.append(pnl["reason"])
        if result is None:
            insuff.append("không định giá được phần còn giữ")
        avg = (sol_in / (bought_sol / 10 ** dec)) if bought_sol and not st_buys else None
        wallets.append({
            "wallet": w, "is_creator": w == creator, "first_buy_min": round(first / 60, 2), "bucket": bucket(first),
            "first_buy_sig": buys[0]["sig"], "buys": len(buys), "sells": len(sells), "sol_in": round(sol_in, 4), "sol_out": round(sol_out, 4),
            "stable_in": round(sum(e.get("quote_amt") or 0 for e in st_buys), 2), "stable_out": round(sum(e.get("quote_amt") or 0 for e in st_sells), 2),
            "avg_cost_sol_per_token": avg, "rise_x": round(p_end / avg, 2) if p_end and avg else None, "roles": roles,
            "window_result_sol": round(result, 4) if result is not None else None,
            "window_result_pct": round(result / sol_in, 3) if result is not None and sol_in else None,
            "bot_flag": len(buys) + len(sells) >= 10 and bool(holds) and statistics.median(holds) < 60,
            "status": "OK" if not [i for i in insuff if i] else "INSUFFICIENT_DATA", "insufficient": [i for i in insuff if i]})
    wallets.sort(key=lambda w: w["first_buy_min"])
    return {"coin": coin, "wallets": wallets}


def points(w: dict) -> int:
    """Early Smart Money points of one wallet in one coin (window data only)."""
    pts = {"0-15": 2, "15-60": 1}.get(w["bucket"], 0)
    r = w.get("rise_x")
    pts += 2 if r and r >= 5 else 1 if r and r >= 2 else 0
    return pts + (1 if "GOOD" in w["roles"] else 0)


# ---------------------------------------------------------------------------------------------- the wallet file
WALLET_DB = Path(__file__).with_name("wallet_db.json")
WALLET_CSV = Path(__file__).with_name("wallet_db.csv")


def build_wallet_db(store, old: dict | None = None, now=time.time) -> dict:
    """The ONE watchlist file: the old research cohorts (DEPTH / STANK / INSIDERS, insiders/result.json, read only)
    + the big wallets of every research coin in esm_* (newcoins.big: >= 5 SOL in a coin entered early, or early in
    >= 2 coins with >= 1 SOL; CREATOR_GROUP and creators never). Merged into `old`: a wallet already in the file is
    never removed (only its coins / evidence are brought up to date, `in_latest_build` says whether it still
    qualifies); a new wallet is added with today's date."""
    from insiders import esm_view as V
    from insiders.newcoins import SMART_MIN_BUY_MULTI_SOL, SMART_MIN_BUY_SOL, big, cohort_wallets
    ts, day = now(), time.strftime("%Y-%m-%d", time.gmtime(now()))
    coins = {r["symbol"]: r["mint"] for r in store.q("SELECT symbol, mint FROM esm_coin ORDER BY t_create")}
    cg = {r["wallet"] for r in store.q("SELECT wallet FROM esm_flag WHERE flag='CREATOR_GROUP'")}
    built: dict[str, dict] = {}
    for x in V.radar(store, False, min_score=4)["smart"] if coins else []:
        if x["creator_of"] or x["wallet"] in cg or not big(x):
            continue
        early = [e for e in x["entries"] if e["bucket"] in ("0-15", "15-60") and not e["at_creation"]]
        built[x["wallet"]] = {
            "sources": [f"coin:{c}" for c in x["early_coins"]], "rank": x["rank"], "multi": x["multi"], "why": x["why"][:4],
            "coins": {e["symbol"]: {"mint": e["mint"], "bucket": e["bucket"], "minute": e["first_buy_min"], "sol_in": e["sol_in"],
                                    "rise_x": e["rise_x"], "roles": e["roles"]} for e in x["entries"]},
            "early_coins": x["early_coins"], "max_buy_sol": round(max((e["sol_in"] or 0) for e in early), 4) if early else None}
    for c in cohort_wallets():
        b = built.setdefault(c["wallet"], {"sources": [], "rank": None, "multi": False, "why": [], "coins": {}, "early_coins": [],
                                           "max_buy_sol": None})
        b["sources"] = [c["cohort"]] + b["sources"]
        b["cohort"], b["cohort_label"] = c["cohort"], c.get("label")
    flags: dict[str, list] = {}
    for f in store.q("SELECT wallet, flag, ref FROM esm_flag WHERE flag IN ('INDEPENDENCE', 'PASS_THROUGH', 'BATCH_SPLIT', 'COORDINATED_PATTERN')"):
        flags.setdefault(f["wallet"], []).append(f"{f['flag']}:{f['ref']}")
    ent = {r["wallet"]: r for r in store.q("SELECT wallet, entity, class FROM esm_wallet_entity")}
    out = {w: dict(e, in_latest_build=False) for w, e in ((old or {}).get("wallets") or {}).items()}   # nobody is dropped
    for w, b in built.items():
        e = ent.get(w) or {}
        lv = [f.split(":", 1)[1] for f in flags.get(w, []) if f.startswith("INDEPENDENCE:")]
        prev = out.get(w) or {}
        out[w] = {**prev, **b, "sources": sorted(set(prev.get("sources") or []) | set(b["sources"])),
                  "independence": lv[0] if lv else e.get("class") or "INSUFFICIENT_DATA", "entity": e.get("entity"),
                  "flags": flags.get(w, []), "added": prev.get("added") or day, "updated": day, "in_latest_build": True}
    return {"version": 1, "updated": ts,
            "rules": {"min_buy_sol": SMART_MIN_BUY_SOL, "min_buy_multi_sol": SMART_MIN_BUY_MULTI_SOL,
                      "early": "0-60 phút sau khi tạo coin", "cohorts": "DEPTH, STANK, INSIDERS (insiders/result.json)"},
            "coins": {**((old or {}).get("coins") or {}), **coins}, "count": len(out),
            "by_source": {k: sum(1 for e in out.values() if any(x == k or x.startswith(k + ":") for x in e["sources"]))
                          for k in ("DEPTH", "STANK", "INSIDERS", "coin")},
            "wallets": dict(sorted(out.items()))}


def write_wallet_db(db: dict, path: Path = WALLET_DB, csv_path: Path = WALLET_CSV) -> None:
    import csv
    path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:          # utf-8-sig: Excel reads the accents
        wr = csv.writer(fh)
        wr.writerow(["wallet", "sources", "cohort", "cohort_label", "early_coins", "max_buy_sol", "rank", "independence",
                     "entity", "flags", "added", "in_latest_build", "coins"])
        for w, e in db["wallets"].items():
            coins = "; ".join(f"{k} phút {c['minute']:g} {c['sol_in'] if c['sol_in'] is not None else '?'} SOL"
                              for k, c in (e.get("coins") or {}).items())
            wr.writerow([w, " ".join(e["sources"]), e.get("cohort") or "", e.get("cohort_label") or "",
                         " ".join(e.get("early_coins") or []), "" if e.get("max_buy_sol") is None else e["max_buy_sol"],
                         "" if e.get("rank") is None else e["rank"], e.get("independence"), e.get("entity") or "",
                         " ".join(e.get("flags") or []), e.get("added"), int(bool(e.get("in_latest_build"))), coins])


def load_wallet_db(path: Path = WALLET_DB) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    """python -m insiders.esm import [research_dir] [--db path]: load the research into the local database (idempotent,
    no network). Default db: the state directory used by main.py (INSIDERS_STATE_DIR / KOL_DB / ./data).
    python -m insiders.esm walletdb [seed_dir]: rebuild insiders/wallet_db.json / .csv from the research seed (an
    in-memory import: no network, no database touched), merged into the current file (no wallet is removed)."""
    import argparse
    from insiders.track import Store
    ap = argparse.ArgumentParser(prog="python -m insiders.esm")
    ap.add_argument("cmd", choices=["import", "walletdb"])
    ap.add_argument("folder", nargs="?", default=None)
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "walletdb":
        st = Store(":memory:")
        import_research(st, a.folder or Path(__file__).with_name("esm_seed"))
        old = load_wallet_db()
        db = build_wallet_db(st, old)
        write_wallet_db(db)
        added = len(set(db["wallets"]) - set((old or {}).get("wallets") or {}))
        print(json.dumps({"file": str(WALLET_DB), "count": db["count"], "added": added, "by_source": db["by_source"],
                          "coins": list(db["coins"])}, ensure_ascii=False))
        return 0
    folder = a.folder or str(Path(__file__).resolve().parents[1] / "research" / "seven_coins")
    if a.db is None:
        from insiders.site import state_dir
        a.db = str(state_dir()[0] / "insiders_track.db")
    st = Store(a.db)
    print(json.dumps({"db": a.db, "schema": st.version(), **import_research(st, folder)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
