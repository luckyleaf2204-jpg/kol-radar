"""Read side of Early Smart Money for the page (/early/ and /api/insiders/esm/*). Only the v5 esm_* tables and the
cohort list (table `wallets`, read only). CREATOR_GROUP is never ranked; COORDINATED_PATTERN is a label only."""
from __future__ import annotations

import json

from insiders.esm import RULES_VERSION, SCORE_RULE, pending_mints

EARLY = ("0-15", "15-60")


def _loads(r: dict, *keys) -> dict:
    for k in keys:
        if isinstance(r.get(k), str):
            try:
                r[k] = json.loads(r[k])
            except ValueError:
                pass
    return r


def _ctx(store) -> dict:
    sym = {r["mint"]: r["symbol"] for r in store.q("SELECT mint, symbol FROM esm_coin")}
    flags: dict[str, list] = {}
    for f in store.q("SELECT wallet, flag, ref FROM esm_flag ORDER BY flag, ref"):
        flags.setdefault(f["wallet"], []).append(f["flag"] if f["flag"] == "CREATOR_GROUP" else f"{f['flag']}:{f['ref']}")
    ent = {r["wallet"]: r for r in store.q("SELECT wallet, entity, class, buyer FROM esm_wallet_entity")}
    cohort = {r["wallet"]: r["cohort"] for r in store.q("SELECT wallet, cohort FROM wallets")}
    return {"sym": sym, "flags": flags, "ent": ent, "cohort": cohort}


def summary(store) -> dict:
    if not store.q("SELECT COUNT(*) AS n FROM esm_coin")[0]["n"]:
        return {"imported": False}
    wt = wallet_table(store)
    cls: dict[str, int] = {}
    for r in wt:
        cls[r["class"]] = cls.get(r["class"], 0) + 1
    fl = {r["flag"]: r["n"] for r in store.q("SELECT flag, COUNT(DISTINCT wallet) AS n FROM esm_flag GROUP BY flag")}
    return {"imported": True, "rules_version": RULES_VERSION, "score_rule": SCORE_RULE,
            "coins": store.q("SELECT COUNT(*) AS n FROM esm_coin")[0]["n"], "wallets": len(wt),
            "ranked": sum(1 for r in wt if r["ranked"]), "early_2plus": sum(1 for r in wt if r["ranked"] and len(r["early_coins"]) >= 2),
            "classes": cls, "flags": fl, "entities": sum(1 for e in entity_table(store)),
            "pending": [r["symbol"] for r in store.q("SELECT symbol FROM esm_pending_mint ORDER BY symbol")],
            "imported_at": store.q("SELECT MAX(analyzed_at) AS t FROM esm_coin")[0]["t"]}


def wallet_table(store) -> list[dict]:
    cx = _ctx(store)
    rank = {r["wallet"]: _loads(r, "early_coins") for r in store.q("SELECT wallet, score, early_coins FROM esm_rank WHERE rules_version=?", (RULES_VERSION,))}
    per: dict[str, list] = {}
    for r in store.q("SELECT mint, wallet, bucket, first_buy_min, sol_in, stable_in, rise_x, roles, status, points, bot_flag FROM esm_window_wallet"):
        per.setdefault(r["wallet"], []).append(_loads(r, "roles"))
    out = []
    for w, rows in per.items():
        rk, e, fl = rank.get(w), cx["ent"].get(w) or {}, cx["flags"].get(w, [])
        cg = "CREATOR_GROUP" in fl
        out.append({"wallet": w, "score": None if cg or not rk else rk["score"], "ranked": bool(rk) and not cg,
                    "coins": [cx["sym"].get(r["mint"]) for r in rows],
                    "early_coins": [cx["sym"].get(r["mint"]) for r in rows if r["bucket"] in EARLY],
                    "first_min": min(r["first_buy_min"] for r in rows), "buckets": sorted({r["bucket"] for r in rows}),
                    "sol_in": round(sum(r["sol_in"] or 0 for r in rows), 3), "stable_in": round(sum(r["stable_in"] or 0 for r in rows), 2),
                    "best_rise": max((r["rise_x"] for r in rows if r["rise_x"]), default=None),
                    "roles": sorted({x for r in rows for x in r["roles"]}), "bot": any(r["bot_flag"] for r in rows),
                    "insufficient": any(r["status"] != "OK" for r in rows), "class": e.get("class") or "INSUFFICIENT_DATA",
                    "entity": e.get("entity"), "flags": fl, "cohort": cx["cohort"].get(w)})
    out.sort(key=lambda r: (not r["ranked"], -len(r["early_coins"]), -(r["score"] or 0), r["first_min"]))
    for i, r in enumerate((r for r in out if r["ranked"]), 1):
        r["rank"] = i
    return out


def wallet_detail(store, wallet: str) -> dict | None:
    cx = _ctx(store)
    rows = store.q("SELECT w.*, c.symbol, c.t_create, c.quote, c.x_120m AS coin_x120 FROM esm_window_wallet w JOIN esm_coin c ON c.mint=w.mint "
                   "WHERE w.wallet=? ORDER BY c.t_create", (wallet,))
    e = cx["ent"].get(wallet)
    if not rows and not e:
        return None
    for r in rows:
        _loads(r, "roles", "insufficient")
    links = []
    for lk in store.q("SELECT * FROM esm_link WHERE a=? OR b=? ORDER BY strength DESC, signal", (wallet, wallet)):
        other = lk["b"] if lk["a"] == wallet else lk["a"]
        oe = cx["ent"].get(other) or {}
        links.append({"other": other, "signal": lk["signal"], "strength": lk["strength"], "coin": lk["coin"], "sig": lk["sig"],
                      "evidence": _loads(lk, "evidence")["evidence"], "other_class": oe.get("class"), "other_buyer": oe.get("buyer"),
                      "other_flags": cx["flags"].get(other, [])})
    row = next((r for r in wallet_table(store) if r["wallet"] == wallet), None)
    members = [m["wallet"] for m in store.q("SELECT wallet FROM esm_wallet_entity WHERE entity=?", (e["entity"],))] if e and e["entity"] else []
    res = [r["result_sol"] for r in rows if r["result_sol"] is not None]
    return {"wallet": wallet, "class": (e or {}).get("class") or "INSUFFICIENT_DATA", "entity": (e or {}).get("entity"),
            "buyer": bool(rows), "entity_members": members, "flags": cx["flags"].get(wallet, []),
            "flag_detail": [_loads(f, "evidence") for f in store.q("SELECT flag, ref, evidence FROM esm_flag WHERE wallet=?", (wallet,))],
            "cohort": cx["cohort"].get(wallet), "rank": (row or {}).get("rank"), "score": (row or {}).get("score"), "coins": rows, "links": links,
            "perf": {"coins": len(rows), "early": sum(1 for r in rows if r["bucket"] in EARLY),
                     "sol_in": round(sum(r["sol_in"] or 0 for r in rows), 3), "sol_out": round(sum(r["sol_out"] or 0 for r in rows), 3),
                     "stable_in": round(sum(r["stable_in"] or 0 for r in rows), 2),
                     "result_sol": round(sum(res), 3) if res and len(res) == len(rows) else None,
                     "result_partial_sol": round(sum(res), 3) if res else None,
                     "insufficient": sum(1 for r in rows if r["status"] != "OK")}}


def entity_table(store) -> list[dict]:
    cx = _ctx(store)
    groups: dict[str, list] = {}
    for w, e in cx["ent"].items():
        if e["entity"]:
            groups.setdefault(e["entity"], []).append(w)
    per: dict[str, list] = {}
    for r in store.q("SELECT wallet, mint, bucket, first_buy_min, points, sol_in FROM esm_window_wallet"):
        per.setdefault(r["wallet"], []).append(r)
    out = []
    for g, mem in groups.items():
        best: dict[str, dict] = {}                            # one owner = one entry per coin: its best member
        for w in mem:
            for r in per.get(w, []):
                cur = best.get(r["mint"])
                if cur is None or (r["points"] or 0) > (cur["points"] or 0):
                    best[r["mint"]] = r
        fl = sorted({f for w in mem for f in cx["flags"].get(w, []) if not f.startswith("CREATOR:")})
        cg = "CREATOR_GROUP" in fl
        out.append({"entity": g, "members": len(mem), "buyers": sum(1 for w in mem if per.get(w)),
                    "coins": sorted(cx["sym"].get(m) for m in best),
                    "early_coins": sorted(cx["sym"].get(m) for m, r in best.items() if r["bucket"] in EARLY),
                    "score": None if cg else sum(r["points"] or 0 for r in best.values()), "ranked": not cg and bool(best),
                    "sol_in": round(sum(r["sol_in"] or 0 for w in mem for r in per.get(w, [])), 3),
                    "first_min": min((r["first_buy_min"] for r in best.values()), default=None), "flags": fl,
                    "classes": sorted({cx["ent"][w]["class"] for w in mem}),
                    "cohorts": sorted({cx["cohort"][w] for w in mem if cx["cohort"].get(w)})})
    out.sort(key=lambda r: (not r["ranked"], -len(r["early_coins"]), -(r["score"] or 0), -r["members"]))
    for i, r in enumerate((r for r in out if r["ranked"]), 1):
        r["rank"] = i
    return out


def entity_detail(store, entity: str) -> dict | None:
    mem = [r["wallet"] for r in store.q("SELECT wallet FROM esm_wallet_entity WHERE entity=?", (entity,))]
    if not mem:
        return None
    cx = _ctx(store)
    tbl = {r["wallet"]: r for r in wallet_table(store)}
    members = [tbl.get(w) or {"wallet": w, "coins": [], "early_coins": [], "class": cx["ent"][w]["class"], "flags": cx["flags"].get(w, []),
                              "cohort": cx["cohort"].get(w), "ranked": False, "score": None, "non_buyer": True, "entity": entity,
                              "first_min": None, "buckets": [], "sol_in": 0, "stable_in": 0, "best_rise": None, "roles": [],
                              "bot": False, "insufficient": False} for w in mem]
    ms = set(mem)
    links = [_loads(lk, "evidence") for lk in store.q("SELECT * FROM esm_link") if lk["a"] in ms and lk["b"] in ms]
    return {"entity": entity, "summary": next((r for r in entity_table(store) if r["entity"] == entity), None), "members": members, "links": links}


def coin_detail(store, mint: str) -> dict | None:
    c = store.q("SELECT * FROM esm_coin WHERE mint=?", (mint,))
    if not c:
        return None
    c = _loads(c[0], "coverage")
    cx = _ctx(store)
    c["outcomes"] = store.q("SELECT horizon, price, x_vs_open, x_vs_120m, trades, status, why FROM esm_outcome WHERE mint=?", (mint,))
    ws = []
    for r in store.q("SELECT * FROM esm_window_wallet WHERE mint=? ORDER BY first_buy_min", (mint,)):
        e = cx["ent"].get(r["wallet"]) or {}
        ws.append({**_loads(r, "roles", "insufficient"), "class": e.get("class") or "INSUFFICIENT_DATA", "entity": e.get("entity"),
                   "flags": cx["flags"].get(r["wallet"], []), "cohort": cx["cohort"].get(r["wallet"])})
    b = {}
    for k in ("0-15", "15-60", "60-120"):
        x = [w for w in ws if w["bucket"] == k]
        b[k] = {"buyers": len(x), "sol_in": round(sum(w["sol_in"] or 0 for w in x), 3), "stable_in": round(sum(w["stable_in"] or 0 for w in x), 2),
                "roles": {r: sum(1 for w in x if r in w["roles"]) for r in ("ACCUMULATE", "EARLY_SELL", "HOLD", "GOOD")},
                "insufficient": sum(1 for w in x if w["status"] != "OK"),
                "flagged": sum(1 for w in x if any(f == "CREATOR_GROUP" or f.startswith("COORDINATED") for f in w["flags"]))}
    c["buckets"], c["wallets"] = b, ws
    return c


def coins(store) -> dict:
    cs = []
    for c in store.q("SELECT mint, symbol, verified, t_create, creator, quote, x_15m, x_60m, x_120m, peak_x, status, note, coverage "
                     "FROM esm_coin ORDER BY t_create DESC"):
        _loads(c, "coverage")
        c["buyers"] = store.q("SELECT COUNT(*) AS n FROM esm_window_wallet WHERE mint=?", (c["mint"],))[0]["n"]
        c["by_bucket"] = {r["bucket"]: r["n"] for r in store.q("SELECT bucket, COUNT(*) AS n FROM esm_window_wallet WHERE mint=? GROUP BY bucket", (c["mint"],))}
        cs.append(c)
    return {"coins": cs, "pending": pending_mints(store)}


# ---------------------------------------------------------------------------------------------- RADAR
def _reason(rows: list[dict], cls: str, flags: list, links: int) -> list[str]:
    """Why the wallet is on the radar, in plain words, from stored evidence only (no new score)."""
    out = []
    early = [r for r in rows if r["bucket"] in EARLY]
    if early:
        out.append("vào sớm " + ", ".join(f"{r['symbol']} phút {r['first_buy_min']:g}" for r in early))
    made = [r["symbol"] for r in rows if r.get("at_creation")]
    if made:
        out.append("ví tạo coin " + ", ".join(made) + ": mua trong giao dịch tạo, giá vào = giá khởi tạo (không tính là hiệu suất)")
    rises = [f"{r['symbol']} {r['rise_x']:g}x" for r in early if r.get("rise_x") and not r.get("at_creation")]
    if rises:
        out.append("giá phút 120 / giá vốn: " + ", ".join(rises))
    good = [r["symbol"] for r in rows if "GOOD" in r["roles"] and not r.get("at_creation")]
    if good:
        out.append("GOOD (≥ +50% trong cửa sổ): " + ", ".join(good))
    out.append({"INDEPENDENT": "nguồn vốn độc lập", "POSSIBLE_LINK": "có liên kết trung bình với ví khác",
                "LIKELY_SAME_ENTITY": "cùng chủ với ví khác (bằng chứng mạnh)"}.get(cls, "chưa đủ dữ liệu nguồn vốn"))
    if any(f.startswith("COORDINATED") for f in flags):
        out.append("giao dịch đồng bộ với nhóm " + ", ".join(f.split(":")[1] for f in flags if f.startswith("COORDINATED")))
    if links:
        out.append(f"{links} liên kết ghi nhận")
    return out


def radar(store, live_on: bool, live_note: str | None = None, min_score: int = 4) -> dict:
    """The RADAR screen. Smart Money = ranked wallets (CREATOR_GROUP never) that entered early (0-60 min) in >= 1 coin;
    order = the approved evidence order (coins entered early, then the approved score, then the earliest entry).
    HISTORICAL signals come from the research windows; LIVE alerts only from the running tracker (table events),
    never simulated."""
    cx = _ctx(store)
    coins = {c["mint"]: c for c in store.q("SELECT mint, symbol, t_create, quote, x_120m, creator FROM esm_coin")}
    allw = [r for r in wallet_table(store) if r["ranked"] and r["early_coins"]]
    # the watchlist: early in >= 2 coins (always), or one coin with the approved score >= min_score (a filter, not a new score)
    rank = {r["wallet"]: r for r in allw if len(r["early_coins"]) >= 2 or (r["score"] or 0) >= min_score}
    per: dict[str, list] = {}
    for r in store.q("SELECT * FROM esm_window_wallet ORDER BY first_buy_min"):
        if r["wallet"] in rank:
            c = coins[r["mint"]]
            per.setdefault(r["wallet"], []).append({**_loads(r, "roles", "insufficient"), "symbol": c["symbol"], "t_create": c["t_create"],
                                                     "quote": c["quote"], "at_creation": r["wallet"] == c["creator"]})
    nlinks: dict[str, int] = {}
    for lk in store.q("SELECT a, b FROM esm_link"):
        for w in (lk["a"], lk["b"]):
            if w in rank:
                nlinks[w] = nlinks.get(w, 0) + 1
    smart = []
    for w, r in rank.items():
        rows = per.get(w, [])
        # a buy inside the coin's creation is priced at the launch price: shown, never counted as Smart Money performance
        perf = [x for x in rows if not x["at_creation"]]
        res = [x["result_pct"] for x in perf if x["result_pct"] is not None]
        rises = [x["rise_x"] for x in perf if x["rise_x"] and x["bucket"] in EARLY]
        smart.append({**{k: r[k] for k in ("wallet", "rank", "score", "early_coins", "coins", "first_min", "class", "entity", "flags",
                                           "cohort", "bot", "sol_in")},
                      "best_rise": max(rises) if rises else None, "creator_of": [x["symbol"] for x in rows if x["at_creation"]],
                      "multi": len(r["early_coins"]) >= 2, "links": nlinks.get(w, 0),
                      "avg_result_pct": round(sum(res) / len(res), 3) if res else None, "results_known": len(res), "perf_entries": len(perf),
                      "entries": [{k: x[k] for k in ("symbol", "mint", "bucket", "first_buy_min", "rise_x", "roles", "result_pct", "status",
                                                     "sol_in", "stable_in", "at_creation")} for x in rows],
                      "why": _reason(rows, r["class"], r["flags"], nlinks.get(w, 0))})
    smart.sort(key=lambda s: s["rank"])
    signals = []
    for s in smart:
        for x in per.get(s["wallet"], []):
            if x["bucket"] not in EARLY:
                continue
            signals.append({"kind": "HISTORICAL", "t": x["t_create"] + x["first_buy_min"] * 60, "wallet": s["wallet"], "rank": s["rank"],
                            "symbol": x["symbol"], "mint": x["mint"], "bucket": x["bucket"], "minute": x["first_buy_min"], "sig": x["first_buy_sig"],
                            "price_sol_per_1m": round(x["avg_cost_sol"] * 1e6, 6) if x["avg_cost_sol"] else None, "quote": x["quote"],
                            "sol_in": x["sol_in"], "stable_in": x["stable_in"], "multi": s["multi"], "class": s["class"], "flags": s["flags"],
                            "at_creation": x["at_creation"], "creator_of": s["creator_of"],
                            "why": s["why"]})
    signals.sort(key=lambda g: -g["t"])
    by_coin = []
    for m, c in sorted(coins.items(), key=lambda kv: -kv[1]["t_create"]):
        ws = [{"wallet": s["wallet"], "rank": s["rank"], "multi": s["multi"], "bucket": x["bucket"], "minute": x["first_buy_min"],
               "flags": s["flags"], "class": s["class"], "creator_of": s["creator_of"]} for s in smart for x in s["entries"] if x["mint"] == m and x["bucket"] in EARLY]
        by_coin.append({"mint": m, "symbol": c["symbol"], "t_create": c["t_create"], "quote": c["quote"], "x_120m": c["x_120m"],
                        "w0_15": sorted((w for w in ws if w["bucket"] == "0-15"), key=lambda w: w["minute"]),
                        "w15_60": sorted((w for w in ws if w["bucket"] == "15-60"), key=lambda w: w["minute"])})
    return {"live": {"on": live_on, "note": live_note, "alerts": live_alerts(store, set(rank), cx) if live_on else [],
                     "last_event": store.q("SELECT MAX(block_time) AS t FROM events")[0]["t"] if live_on else None},
            "imported_at": store.q("SELECT MAX(analyzed_at) AS t FROM esm_coin")[0]["t"], "min_score": min_score,
            "candidates": len(allw), "smart": smart, "signals": signals, "coins": by_coin, "groups": _groups(store, cx),
            "pending": [r["symbol"] for r in store.q("SELECT symbol FROM esm_pending_mint ORDER BY symbol")]}


def _groups(store, cx) -> dict:
    """Owner groups (strong evidence), coordinated bot groups (behaviour label) and creator groups, with evidence."""
    ent = entity_table(store)
    coord: dict[str, dict] = {}
    for f in store.q("SELECT wallet, ref, evidence FROM esm_flag WHERE flag='COORDINATED_PATTERN' ORDER BY ref"):
        g = coord.setdefault(f["ref"], {"id": f["ref"], "wallets": [], "evidence": _loads(f, "evidence")["evidence"]})
        g["wallets"].append(f["wallet"])
    return {"same_owner": [e for e in ent if e["ranked"] and e["members"] >= 2],
            "coordinated": sorted(coord.values(), key=lambda g: -len(g["wallets"])),
            "creator_groups": [e for e in ent if "CREATOR_GROUP" in e["flags"]],
            "creators": [{"wallet": f["wallet"], "coin": f["ref"], "in_group": "CREATOR_GROUP" in cx["flags"].get(f["wallet"], [])}
                         for f in store.q("SELECT wallet, ref FROM esm_flag WHERE flag='CREATOR' ORDER BY ref")]}


def live_alerts(store, smart: set, cx, limit: int = 200) -> list[dict]:
    """LIVE: buys seen by the running tracker (table events) by a Smart Money wallet in a coin that is NOT one of the
    researched coins. Real rows only; empty when nothing was seen."""
    if not smart:
        return []
    done = {r["mint"] for r in store.q("SELECT mint FROM esm_coin")}
    names = {r["mint"]: r["name"] for r in store.q("SELECT mint, name FROM mints")}
    marks = ",".join("?" * len(smart))
    rows = store.q(f"SELECT sig, wallet, mint, kind, block_time, token_delta, quote_sol, source FROM events WHERE kind IN ('BUY', 'CREATE') "
                   f"AND wallet IN ({marks}) ORDER BY block_time DESC LIMIT ?", (*smart, limit))
    out = []
    for r in rows:
        if r["mint"] in done:
            continue
        q = float(r["token_delta"] or 0)
        out.append({"kind": "LIVE", "t": r["block_time"], "wallet": r["wallet"], "mint": r["mint"], "symbol": names.get(r["mint"]),
                    "sol": r["quote_sol"], "price_sol_per_1m": round(r["quote_sol"] * 1e12 / q, 6) if r["quote_sol"] and q else None,
                    "sig": r["sig"], "source": r["source"], "flags": cx["flags"].get(r["wallet"], []),
                    "class": (cx["ent"].get(r["wallet"]) or {}).get("class")})
    return out
