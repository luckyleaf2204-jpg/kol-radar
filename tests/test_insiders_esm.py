"""Migration v5 (esm_* tables) and the Early Smart Money import / ranking. Synthetic files only, no network."""
import json
import sqlite3

from insiders import backup as B
from insiders import esm as E
from insiders import track as T

CRE_A, CRE_B = "CreatorA1111111111111111111111111111111111", "CreatorB1111111111111111111111111111111111"
CRE_C = "CreatorC1111111111111111111111111111111111"     # a creator with no link evidence: stays ranked
W1, W2, W3 = "Wallet1111111111111111111111111111111111111", "Wallet2222222222222222222222222222222222222", "Wallet3333333333333333333333333333333333333"


def wallet(w, bucket, minute, roles=(), status="OK", sol=1.0):
    return {"wallet": w, "bucket": bucket, "first_buy_min": minute, "first_buy_sig": f"s{w[:6]}", "buys": 1, "sells": 0,
            "sol_in": sol, "sol_out": 0, "stable_in": 0, "stable_out": 0, "avg_cost_sol_per_token": 1e-6, "roles": list(roles),
            "window_result_sol": 0.5, "window_result_pct": 0.5, "bot_flag": False, "status": status, "insufficient": []}


def research(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    for sym, mint, cre, ws in (("AAA", "MintA", CRE_A, [wallet(CRE_A, "0-15", 0), wallet(W1, "0-15", 1), wallet(W2, "15-60", 20)]),
                               ("BBB", "MintB", CRE_B, [wallet(CRE_B, "0-15", 0), wallet(W1, "15-60", 30), wallet(W3, "60-120", 90)])):
        (d / f"window_{sym}.json").write_text(json.dumps({"coin": {"symbol": sym, "mint": mint, "t_create": 100, "creation_sig": "c", "creator": cre,
                                                                    "p0": 1, "p_15m": 2, "p_60m": 3, "p_120m": 4, "x_15m": 2, "x_60m": 3, "x_120m": 4,
                                                                    "peak_x": 5, "peak_min": 50},
                                                           "coverage": {"decoded": 10}, "wallets": ws}))
    (d / "entities.json").write_text(json.dumps({
        "classes": {CRE_A: "LIKELY_SAME_ENTITY", CRE_B: "LIKELY_SAME_ENTITY", W1: "INDEPENDENT", W2: "POSSIBLE_LINK", W3: "INSUFFICIENT_DATA",
                    "FeePayer": "LIKELY_SAME_ENTITY"},
        "entity_of": {CRE_A: "E001", CRE_B: "E001", "FeePayer": "E001"},
        "pairs": [{"a": CRE_A, "b": CRE_B, "signals": {"direct_transfer": {"sol": 16, "sig": "x"}}},
                  {"a": W1, "b": W2, "signals": {"same_slot": {"coin": "AAA"}}}]}))
    rank = lambda w, score, early, flags=(): {"wallet": w, "score": score, "coins": early, "early_coins": early, "avg_entry_min": 1, "class": "INDEPENDENT",
                                             "entity": None, "flags": list(flags), "insufficient": [],
                                             "per_coin": {s: {"points": 2, "rise_after_entry_x": 2.0} for s in early}}
    (d / "early_smart_money.json").write_text(json.dumps({
        "wallets": [rank(W1, 5, ["AAA", "BBB"]), rank(W2, 3, ["AAA"], ["COORDINATED_PATTERN:C001"]), rank(CRE_C, 2, ["AAA"], ["CREATOR:CCC"])],
        "creator_group": {"entities": ["E001"], "creators": {CRE_A: "AAA", CRE_B: "BBB", CRE_C: "CCC"}, "members": [CRE_A, CRE_B, "FeePayer"],
                          "excluded_wallets": [rank(CRE_A, 9, ["AAA"], ["CREATOR_GROUP"]), rank(CRE_B, 9, ["BBB"], ["CREATOR_GROUP"])]}}))
    (d / "coordinated.json").write_text(json.dumps({"groups": [{"id": "C001", "wallets": [W2], "shared_slots": 4}]}))
    (d / "outcomes.json").write_text(json.dumps({"coins": {"AAA": {"6h": {"price": 9, "x_vs_open": 9, "x_vs_120m": 2.25, "trades": 5},
                                                                   "24h": {"status": "INSUFFICIENT_DATA", "why": "mốc chưa tới"}}}}))
    (d / "candidates_MOONX_COGE.json").write_text(json.dumps({"x": {"ticker": "COGE", "mint": "Cand1", "overlap_verified_early": {"a": 1}}}))
    return d


def test_v4_database_migrates_to_v5_and_keeps_every_row_and_cohort(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    for sql in T.MIGRATIONS[:4]:
        con.executescript(sql)
    con.execute("PRAGMA user_version=4")
    con.execute("INSERT INTO mints (mint, name, newest_sig, pages) VALUES ('M', 'OLD', 'CP', 7)")
    for w, c in (("d", "DEPTH"), ("s", "STANK"), ("i", "INSIDERS")):
        con.execute("INSERT INTO wallets (wallet, cohort) VALUES (?, ?)", (w, c))
    con.execute("INSERT INTO wallet_pnl (wallet, mint, status) VALUES ('d', 'M', 'OK')")
    con.commit()
    before = B.table_rows(p)
    con.close()
    st = T.Store(p)
    after = B.table_rows(p)
    assert st.version() == 5 == len(T.MIGRATIONS)
    assert all(after[t] == n for t, n in before.items())
    assert st.q("SELECT cohort FROM wallets ORDER BY wallet") == [{"cohort": "DEPTH"}, {"cohort": "INSIDERS"}, {"cohort": "STANK"}]
    assert st.mint("M")["newest_sig"] == "CP" and st.mint("M")["pages"] == 7
    assert {"esm_coin", "esm_window_wallet", "esm_link", "esm_wallet_entity", "esm_flag", "esm_outcome", "esm_rank", "esm_pending_mint", "esm_queue", "esm_sig", "esm_event"} <= set(after)
    assert (tmp_path / "old.db.v4.bak").exists()


def test_import_ranks_without_creator_group_and_keeps_its_evidence(tmp_path):
    st = T.Store(tmp_path / "t.db")
    st.x("INSERT INTO wallets (wallet, cohort) VALUES (?, 'STANK')", (W1,))
    n = E.import_research(st, research(tmp_path), verified={"AAA": "ok"})
    assert n["coins"] == 2
    r = E.ranking(st)
    assert [x["wallet"] for x in r] == [W1, W2, CRE_C]            # creator group out; a creator without evidence stays
    assert r[0]["cohort"] == "STANK" and r[1]["flags"] == ["COORDINATED_PATTERN:C001"]
    cg = {x["wallet"] for x in E.flags(st, "CREATOR_GROUP")}
    assert cg == {CRE_A, CRE_B, "FeePayer"}                       # the whole entity is flagged, nothing deleted
    assert st.q("SELECT COUNT(*) AS n FROM esm_window_wallet WHERE wallet IN (?, ?)", (CRE_A, CRE_B))[0]["n"] == 2
    assert st.q("SELECT signal, strength FROM esm_link WHERE a=?", (CRE_A,)) == [{"signal": "direct_transfer", "strength": "STRONG"}]
    nb = st.q("SELECT buyer FROM esm_wallet_entity WHERE wallet='FeePayer'")[0]["buyer"]
    assert nb == 0                                                # a non-buyer cluster member is marked as such


def test_import_is_idempotent_and_leaves_other_tables_alone(tmp_path):
    st = T.Store(tmp_path / "t.db")
    st.x("INSERT INTO wallets (wallet, cohort) VALUES ('d', 'DEPTH')")
    d = research(tmp_path)
    E.import_research(st, d)
    first = B.table_rows(tmp_path / "t.db")
    E.import_research(st, d)
    assert B.table_rows(tmp_path / "t.db") == first
    assert st.q("SELECT wallet, cohort FROM wallets") == [{"wallet": "d", "cohort": "DEPTH"}]


def test_outcomes_are_stored_apart_and_never_change_the_score(tmp_path):
    st = T.Store(tmp_path / "t.db")
    d = research(tmp_path)
    E.import_research(st, d)
    s1 = [(x["wallet"], x["score"]) for x in E.ranking(st)]
    o = json.loads((d / "outcomes.json").read_text())
    o["coins"]["AAA"]["6h"]["x_vs_120m"] = 1000.0
    (d / "outcomes.json").write_text(json.dumps(o))
    E.import_research(st, d)
    assert [(x["wallet"], x["score"]) for x in E.ranking(st)] == s1
    oc = [c for c in E.coins(st) if c["symbol"] == "AAA"][0]["outcomes"]
    assert {x["horizon"]: x["status"] for x in oc} == {"6h": "OK", "24h": "INSUFFICIENT_DATA"}


def test_pending_mints_stay_unverified(tmp_path):
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    (p,) = E.pending_mints(st)
    assert p["symbol"] == "COGE" and p["status"] == "NEEDS_MINT_VERIFICATION" and p["candidates"][0]["mint"] == "Cand1"
    assert not st.q("SELECT 1 FROM esm_coin WHERE mint='Cand1'")


def test_a_failed_import_changes_nothing(tmp_path):
    st = T.Store(tmp_path / "t.db")
    d = research(tmp_path)
    E.import_research(st, d)
    before = B.table_rows(tmp_path / "t.db")
    (d / "entities.json").write_text(json.dumps({"classes": {}, "entity_of": {}, "pairs": [{"a": "x", "b": "y", "signals": {"s": None}}]}))
    try:
        E.import_research(st, d)
    except Exception:
        pass
    else:
        raise AssertionError("bad file must fail")
    assert B.table_rows(tmp_path / "t.db") == before


def test_creator_group_is_excluded_in_query_even_if_the_rank_rows_hold_it(tmp_path):
    """Defence in depth: a CREATOR_GROUP wallet that reached esm_rank (old file, hand edit) is still not ranked."""
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    st.x("INSERT INTO esm_rank (rules_version, wallet, score, coins, early_coins, avg_entry_min, flags) VALUES (?,?,?,?,?,?,?)",
         (E.RULES_VERSION, CRE_A, 99, '["AAA"]', '["AAA"]', 0, "[]"))
    assert CRE_A not in {x["wallet"] for x in E.ranking(st)}
    labels = {(f["wallet"], f["flag"]) for f in E.flags(st, "CREATOR")}
    assert (CRE_C, "CREATOR") in labels and (CRE_A, "CREATOR") in labels      # every creator stored, as a label


def test_coordinated_pattern_does_not_change_the_score(tmp_path):
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    w2 = [x for x in E.ranking(st) if x["wallet"] == W2][0]
    assert w2["score"] == 3 and w2["flags"] == ["COORDINATED_PATTERN:C001"]


def test_radar_lists_only_the_watchlist_and_never_the_creator_group(tmp_path):
    from insiders import esm_view as V
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    r = V.radar(st, live_on=False, live_note="off", min_score=4)
    ws = {s["wallet"] for s in r["smart"]}
    assert W1 in ws and CRE_A not in ws and CRE_B not in ws          # W1: early in 2 coins; creators never
    assert all(g["kind"] == "HISTORICAL" for g in r["signals"]) and r["live"] == {"on": False, "note": "off", "alerts": [], "last_event": None}
    assert all(s["why"] for s in r["smart"])


def test_live_alerts_come_only_from_real_tracker_events(tmp_path):
    from insiders import esm_view as V
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    st.x("INSERT INTO mints (mint, name) VALUES ('NewMint', 'NEW')")
    for sig, w, m in (("s1", W1, "NewMint"), ("s2", W1, "MintA"), ("s3", "Stranger", "NewMint")):
        st.x("INSERT INTO events (sig, wallet, mint, kind, block_time, token_delta, quote_sol, source) VALUES (?,?,?,?,?,?,?,?)",
             (sig, w, m, "BUY", 500, "2000000000000", 1.0, "poll"))
    assert V.radar(st, live_on=False)["live"]["alerts"] == []        # tracker off: nothing shown, nothing simulated
    al = V.radar(st, live_on=True)["live"]["alerts"]
    assert [(a["wallet"], a["mint"], a["symbol"]) for a in al] == [(W1, "NewMint", "NEW")]   # researched coin / stranger: no alert
    assert al[0]["kind"] == "LIVE" and al[0]["price_sol_per_1m"] == 0.5


def test_a_buy_in_the_creation_is_not_counted_as_performance_and_rank_is_kept(tmp_path):
    from insiders import esm_view as V
    st = T.Store(tmp_path / "t.db")
    E.import_research(st, research(tmp_path))
    before = {s["wallet"]: (s["rank"], s["score"]) for s in V.radar(st, False, min_score=1)["smart"]}
    st.x("UPDATE esm_coin SET creator=? WHERE mint='MintA'", (W2,))          # W2's only early buy becomes its own launch buy
    r = V.radar(st, False, min_score=1)
    w2 = next(s for s in r["smart"] if s["wallet"] == W2)
    assert (w2["rank"], w2["score"]) == before[W2]                            # rank and score untouched
    assert w2["creator_of"] == ["AAA"] and w2["best_rise"] is None and w2["avg_result_pct"] is None and w2["perf_entries"] == 0
    assert any("giá khởi tạo" in x for x in w2["why"])
    assert all(g["at_creation"] for g in r["signals"] if g["wallet"] == W2)
