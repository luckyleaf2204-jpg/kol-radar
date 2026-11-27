"""V2 network trace: token owners, relays (near 1:1), batch splits, shared counterparties, import into the existing
tables, independence in the Smart Money book. Fixtures are the real V2 cases (E007, 9Y2, HHLz, 2phTY, 2rbMg) cut down
to the edges that matter; the regression part reads the research folder when it is there (no network)."""
import json
from pathlib import Path

import pytest

from insiders import esm as E
from insiders import track as T
from insiders.scan import on_curve

ARU, Y2, MTZ, RAQ, MUH = ("ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn", "9Y2Es2V6QN4H637kTNd1LVZTthkRsint3WubyXEJzV7E",
                         "9mTZH73WW9ZTSEH2EJuhcdNWdPUQ2QPVhWkj4v2vrk6y", "4raqd4tCEnm2yGeUocVqPgN2MPB1W6A3EZfC8LoMChvf",
                         "9MUhLmpKm5Ut5cd6Q7yDjqVZYvtxwLFFL4qtw92aFtrM")
SIX, HXM = "6pNndy8sewtG7QVfJohKRnpTwdWSdHMi6YQgVNKithNh", "HxmwX3xRFxPE2LsnC9aRXQqh96fkyCSKnpXH9BLm3n47"
HHL, DYR, HNB = ("HHLzvoz1A95eKAQu3qs5i6qLC7VEm8R9qhQmqpRDFSjZ", "DyrKfa9unX9T5u3HvTbhfkpPVYwDuUrnBwFNf3uM37rH",
                 "HNbZoQEosLMZJ4uuhoWBmLQq8neNqh3mjZaqL1HMyBRv")
D23, CKZ, ER9 = ("2351du6dyERkj1YBu6D4s8Jc4SpmY17AqzSmeH4JgkpZ", "CKz4sXenERmvMHWPGGF94to7C98R8cZtuiQQUcELhUCd",
                 "7ER9dTZLgz28euwCRhwUAaG2EHvSAdL8LU4rh5zqwhUE")
PHT = "2phTYhFT1BtCGx3c6xpxQb4bxuP1aFRrDbpmxSM9weLE"             # off curve: a program account (fee vault), not a person
MINT = "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump"
RESEARCH = Path(__file__).resolve().parents[1] / "research" / "seven_coins"
ARCHIVE = Path(__file__).resolve().parents[1] / "research" / "archive_7coins_2026-11-26"


def e(src, dst, amount, ts, sig, asset="SOL"):
    return {"src": src, "dst": dst, "asset": asset, "amount": amount, "ts": ts, "sig": sig}


# the 9Y2 relay: two near 1:1 relays (18 s / 70 s, the loss is fees) and one funding that is NOT relayed 1:1
Y2_EDGES = [e(RAQ, Y2, 1.61944204, 1795456212, "yiKG"), e(Y2, MUH, 2.741547872, 1795457268, "relayNot1to1"),
            e(ARU, Y2, 3.621169905, 1795457640, "2KVd"), e(Y2, MTZ, 3.61765255 + 0.00148844, 1795457658, "AtYo"),
            e(ARU, Y2, 1.663028838, 1795466938, "3LAf"), e(Y2, MTZ, 1.659487298 + 0.00148844, 1795467008, "N6fv")]
HHL_EDGES = [e(HHL, DYR, 0.5, 1793241382, "3G46"), e(HHL, HNB, 0.495, 1793241382, "3G46"),
             e(HHL, DYR, 0.5, 1793241141, "365M"), e(HHL, HNB, 0.495, 1793241141, "365M"),
             e(HHL, DYR, 0.3, 1793241116, "2WZo"), e(HHL, HNB, 0.297, 1793241116, "2WZo"),
             e(HHL, DYR, 0.775887756, 1793241209, "52C2")]


def test_relay_found_only_for_near_one_to_one_flows():
    rows = T.pass_through(Y2_EDGES)
    assert [(r["relay"], r["src"], r["dst"], r["in_sig"], r["out_sig"]) for r in rows] == \
        [(Y2, ARU, MTZ, "2KVd", "AtYo"), (Y2, ARU, MTZ, "3LAf", "N6fv")]
    assert all(r["loss_sol"] < 0.01 and r["gap_s"] <= 70 for r in rows)
    assert not T.pass_through(Y2_EDGES, max_gap_s=10)                   # 18 s and 70 s: outside a 10 s window
    assert not T.pass_through(Y2_EDGES, busy={Y2})                      # a busy hub is never a relay


def test_recurring_batch_split_into_program_accounts_links_nobody():
    rows = T.batch_splits(HHL_EDGES)
    assert len(rows) == 1 and rows[0]["source"] == HHL and rows[0]["recipients"] == sorted([DYR, HNB])
    assert rows[0]["n_tx"] == 3 and rows[0]["recurring"]                # 52C2 paid DYR alone: not part of the split
    assert not rows[0]["persons"] and set(rows[0]["program_recipients"]) == {DYR, HNB}   # HHLz pays two program accounts
    people = [dict(x, dst={DYR: SIX, HNB: HXM}[x["dst"]]) for x in HHL_EDGES]
    assert T.batch_splits(people)[0]["persons"]


def test_relay_sides_say_when_they_are_program_accounts():
    r = T.pass_through(Y2_EDGES)[0]
    assert r["src_program"] and r["dst_program"]                          # 9Y2 moves its own SOL between two services


def test_shared_recipient_skips_program_accounts_and_direct_transfers():
    assert not on_curve(PHT)
    edges = [e(ER9, PHT, 0.158, 1, "a"), e(CKZ, PHT, 0.138, 2, "b"),     # the "shared recipient" of V2 is a program account
             e(D23, CKZ, 1.5, 3, "QVs6"), e(D23, ER9, 1.5, 3, "5ZgW"),   # E007 funding: a direct transfer, not "shared"
             e(ER9, SIX, 0.2, 4, "c"), e(CKZ, SIX, 0.3, 5, "d"), e(ER9, MUH, 0.2, 6, "e"), e(CKZ, MUH, 0.3, 7, "f")]
    wallets = {D23, CKZ, ER9}
    assert not on_curve(MUH)                                             # 9Y2's 2.74 SOL went to a program account too
    assert T.shared_counterparties(edges, wallets, "out") == {SIX: {"wallets": sorted([CKZ, ER9]), "sol": 0.5, "sigs": ["c", "d"]}}
    assert T.shared_counterparties(edges, wallets, "in") == {}
    assert T.shared_counterparties(edges, wallets, "out", busy={SIX}) == {}


def _tx(balances_pre, balances_post, keys, signers):
    return {"slot": 1, "blockTime": 10, "meta": {"err": None, "preTokenBalances": balances_pre, "postTokenBalances": balances_post},
            "transaction": {"message": {"accountKeys": [{"pubkey": k, "signer": k in signers} for k in keys]}}}


def _bal(i, owner, amount, mint=MINT):
    return {"accountIndex": i, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}


def test_token_transfer_sides_come_from_the_token_account_owner_not_the_fee_payer():
    keys = [D23, "AtaOfCkz111111111111111111111111111111111111", "AtaOfEr9111111111111111111111111111111111111"]
    tx = _tx([_bal(1, CKZ, 500), _bal(2, ER9, 0)], [_bal(1, CKZ, 200), _bal(2, ER9, 300)], keys, {D23})
    mv = T.token_moves(tx)
    assert mv == [{"src": CKZ, "dst": ER9, "mint": MINT, "raw": 300, "decimals": 6, "src_account": keys[1], "dst_account": keys[2],
                   "verified": "owner"}]                                 # D23 paid the fee: never taken as the sender


def test_token_from_a_pool_is_a_swap_and_two_by_two_is_ambiguous():
    swap = _tx([_bal(1, PHT, 900), _bal(2, CKZ, 0)], [_bal(1, PHT, 600), _bal(2, CKZ, 300)], [D23, "a", "b"], {D23})
    assert T.token_moves(swap) == []                                     # 2rbMg case: tokens came from a pool, bought by another signer
    multi = _tx([_bal(1, CKZ, 10), _bal(2, ER9, 10), _bal(3, D23, 0), _bal(4, SIX, 0)],
                [_bal(1, CKZ, 0), _bal(2, ER9, 0), _bal(3, D23, 10), _bal(4, SIX, 10)], [HHL, "a", "b", "c", "d"], {HHL})
    assert [(m["src"], m["dst"], m["verified"]) for m in T.token_moves(multi)] == [(None, None, "ambiguous")]
    failed = dict(swap, meta=dict(swap["meta"], err={"x": 1}))
    assert T.token_moves(failed) == []


# ------------------------------------------------------------------------------------------------ import + book
def _folder(tmp_path):
    from test_insiders_esm import research
    d = research(tmp_path)
    ent = json.loads((d / "entities.json").read_text())
    ent["flags"] = {Y2: [{"flag": "PASS_THROUGH", "src": ARU, "dst": MTZ, "sol_in": 3.62, "sol_out": 3.619, "gap_s": 18, "sigs": ["2KVd", "AtYo"]}],
                    DYR: [{"flag": "BATCH_SPLIT", "source": HHL, "with": [HNB], "n_tx": 3, "sigs": ["3G46"]}],
                    HNB: [{"flag": "BATCH_SPLIT", "source": HHL, "with": [DYR], "n_tx": 3, "sigs": ["3G46"]}]}
    (d / "entities.json").write_text(json.dumps(ent))
    (d / "network_trace.json").write_text(json.dumps({"edges": [dict(x, dir="out", hop=1, source="cache") for x in Y2_EDGES]
                                                      + [dict(e(CKZ, ER9, 300.0, 9, "tok1", MINT), dir="out_token", hop=1, source="detail")]}))
    (d / "tierA_review.json").write_text(json.dumps({"wallets": [{"wallet": DYR, "independence": "POSSIBLE_LINK", "group": DYR, "group_size": 1}]}))
    return d


def test_import_puts_v2_edges_in_the_existing_graph_table_and_keeps_live_edges(tmp_path):
    st = T.Store(tmp_path / "db.sqlite")
    st.add_edge("LiveSrc", "LiveDst", "SOL", 1.0, 5, "liveSig", "fund")
    d = _folder(tmp_path)
    n1 = E.import_research(st, d)
    rows = st.q("SELECT kind, COUNT(*) n FROM edges GROUP BY kind ORDER BY kind")
    assert rows == [{"kind": "fund", "n": 1}, {"kind": "research_sol", "n": 6}, {"kind": "research_token", "n": 1}]
    n2 = E.import_research(st, d)                                        # idempotent
    assert n1 == n2 and st.q("SELECT COUNT(*) n FROM edges")[0]["n"] == 8
    assert {r["flag"] for r in st.q("SELECT flag FROM esm_flag WHERE wallet IN (?,?,?)", (Y2, DYR, HNB))} == \
        {"PASS_THROUGH", "BATCH_SPLIT", "INDEPENDENCE"}
    assert T.pass_through(st.edges(research=True))[0]["relay"] == Y2     # the graph functions read them when asked
    assert [e["kind"] for e in st.edges()] == ["fund"]                   # the live tracker's view is unchanged


def test_book_reports_independence_and_keeps_the_multi_wallet_grouping(tmp_path):
    from insiders.newcoins import SmartBook
    st = T.Store(tmp_path / "db.sqlite")
    E.import_research(st, _folder(tmp_path))
    b = SmartBook(st)
    b.refresh(1e9)
    assert b.independence(DYR) == "POSSIBLE_LINK"                        # the tier-A review level
    assert b.independence("Unknown11111111111111111111111111111111111") == "INSUFFICIENT_DATA"   # never assumed independent
    assert b.group(DYR) == DYR and b.group(HNB) == HNB                   # MULTI_WALLET grouping unchanged by V2 flags
    assert b.group(Y2) == Y2
    assert any(f.startswith("BATCH_SPLIT:") for f in b.info[DYR]["flags"])    # the flag is read, only shown


# ------------------------------------------------------------------------------------- regression on the research
need = pytest.mark.skipif(not (RESEARCH / "network_trace.json").exists() or not (ARCHIVE / "entities.json").exists(),
                          reason="research folder (not in the repo) not present")


@need
def test_old_owner_groups_and_strong_classes_are_kept():
    old = json.loads((ARCHIVE / "entities.json").read_text(encoding="utf-8"))
    new = json.loads((RESEARCH / "entities.json").read_text(encoding="utf-8"))
    for g in old["entities"]:                                            # every old group still sits in ONE group
        assert len({new["entity_of"].get(w) for w in g["members"]}) == 1, g["id"]
    weaker = [w for w, c in old["classes"].items() if c == "LIKELY_SAME_ENTITY" and new["classes"].get(w) != "LIKELY_SAME_ENTITY"]
    assert weaker == []
    old_pairs = {(p["a"], p["b"], k) for p in old["pairs"] for k in p["signals"]}
    new_pairs = {(p["a"], p["b"], k) for p in new["pairs"] for k in p["signals"]}
    lost = old_pairs - new_pairs
    assert not lost, sorted(lost)[:5]


@need
def test_v2_cases_in_the_research_outputs():
    tr = json.loads((RESEARCH / "network_trace.json").read_text(encoding="utf-8"))
    ent = json.loads((RESEARCH / "entities.json").read_text(encoding="utf-8"))
    assert ent["entity_of"][D23] == ent["entity_of"][CKZ] == ent["entity_of"][ER9]         # E007
    assert any(r["relay"] == Y2 and r["src"] == ARU and r["dst"] == MTZ for r in tr["pass_through"])
    assert any(f["flag"] == "PASS_THROUGH" for f in ent["flags"].get(Y2, []))
    assert any(r["source"] == HHL and r["recurring"] and not r["persons"] for r in tr["batch_splits"])
    assert not ent["flags"].get(DYR) and not ent["flags"].get(HNB)        # paying two program accounts links nobody
    assert PHT not in tr["shared_recipients"]
    six = "6pNndy8sewtG7QVfJohKRnpTwdWSdHMi6YQgVNKithNh"                 # the MOONX funding missed by the single anchor
    assert any(x["src"] == "HxmwX3xRFxPE2LsnC9aRXQqh96fkyCSKnpXH9BLm3n47" and x["dst"] == six and x["amount"] == 1.0 for x in tr["edges"])
    m = {x["coin"]: x for x in tr["mismatch"]}
    assert set(m) == {"CHUCHU", "MOONX"} and all(x["wallet"] not in (x["signers"] or []) for x in m.values())


# ------------------------------------------------------------------------------- production path: seed re-import
def test_changed_seed_is_imported_once_and_other_tables_are_kept(tmp_path, monkeypatch):
    from insiders import site as S
    d = _folder(tmp_path)
    (d / "candidates_MOONX_COGE.json").write_text(json.dumps({"x": {"ticker": "AAA", "mint": "MintA"}, "y": {"ticker": "ZZZ", "mint": "MintZ"}}))
    st = T.Store(tmp_path / "insiders_track.db")
    st.add_edge("LiveSrc", "LiveDst", "SOL", 1.0, 5, "liveSig", "fund")
    st.x("INSERT INTO nc_coin (mint, symbol) VALUES ('NcMint', 'NC')")
    st.x("INSERT INTO esm_pending_mint VALUES ('AAA', 'NEEDS_MINT_VERIFICATION', '[]', 'old', 1)")
    monkeypatch.setenv("ESM_RESEARCH_DIR", str(d))
    a = S.open_esm(tmp_path)
    assert a.seed_info["imported"] and a.get("esm_seed_version") == S.seed_version(d)
    b = S.open_esm(tmp_path)                                             # same seed: not imported again
    assert b.seed_info["imported"] is None and b.seed_info["version"] == a.seed_info["version"]
    assert b.q("SELECT COUNT(*) n FROM nc_coin")[0]["n"] == 1 and b.q("SELECT COUNT(*) n FROM edges WHERE kind='fund'")[0]["n"] == 1
    assert {r["symbol"]: r["status"] for r in b.q("SELECT symbol, status FROM esm_pending_mint")} == \
        {"AAA": "VERIFIED", "ZZZ": "NEEDS_MINT_VERIFICATION"}                # a verified coin is kept and marked, never deleted
    assert [p["symbol"] for p in E.pending_mints(b)] == ["ZZZ"]
    (d / "verified.json").write_text(json.dumps({"AAA": "x"}))           # the seed changed: imported again
    assert S.open_esm(tmp_path).seed_info["imported"]


def test_backup_refuses_when_the_disk_is_too_full_and_leaves_nothing_behind(tmp_path, monkeypatch):
    import shutil
    from insiders import backup as B
    st = T.Store(tmp_path / "insiders_track.db")
    st.put("k", "v")
    real = shutil.disk_usage
    monkeypatch.setattr(B.shutil, "disk_usage", lambda p: real(p)._replace(free=1000))
    with pytest.raises(RuntimeError, match="không đủ dung lượng"):
        B.backup(tmp_path, reason="start")
    assert not (tmp_path / B.DIR).exists() or not any((tmp_path / B.DIR).iterdir())
    monkeypatch.setattr(B.shutil, "disk_usage", real)
    assert B.verify(B.backup(tmp_path, reason="start")) == []


def test_a_failed_copy_is_removed(tmp_path, monkeypatch):
    from insiders import backup as B
    T.Store(tmp_path / "insiders_track.db").put("k", "v")
    monkeypatch.setattr(B, "copy_db", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        B.backup(tmp_path)
    assert not any((tmp_path / B.DIR).iterdir())


def test_existing_research_is_not_updated_without_a_backup(tmp_path, monkeypatch):
    from insiders import site as S
    d = _folder(tmp_path)
    monkeypatch.setenv("ESM_RESEARCH_DIR", str(d))
    first = S.open_esm(tmp_path, backup_ok=False)                        # empty tables: the first import needs no backup
    assert first.seed_info["imported"]
    (d / "verified.json").write_text(json.dumps({"AAA": "changed"}))
    st = S.open_esm(tmp_path, backup_ok=False)
    assert st.seed_info["imported"] is None and "sao lưu" in st.seed_info["error"]
    assert st.get("esm_seed_version") == first.get("esm_seed_version")
    assert S.open_esm(tmp_path, backup_ok=True).seed_info["imported"]


# ------------------------------------------------------------------------- size thresholds (user, 2026-11-26)
def test_watchlist_keeps_only_wallets_that_bought_big_early():
    from insiders.newcoins import SMART_MIN_BUY_SOL, big
    assert SMART_MIN_BUY_SOL == 5
    e = lambda sol, bucket="0-15", at=False: {"sol_in": sol, "bucket": bucket, "at_creation": at}   # noqa: E731
    assert big({"entries": [e(0.4), e(6.0, "15-60")]})
    assert not big({"entries": [e(4.9), e(50.0, "60-120")]})            # big only late: not an early insider buy
    assert not big({"entries": [e(20.0, at=True)]})                       # bought inside the creation: not counted
    assert not big({"entries": []})
    assert big({"early_coins": ["A", "B"], "entries": [e(1.0), e(0.2, "15-60")]})          # repeat early wallet: >= 1 SOL
    assert not big({"early_coins": ["A", "B"], "entries": [e(0.9), e(0.2, "15-60")]})


def test_old_copies_are_pruned_before_the_space_check(tmp_path, monkeypatch):
    import shutil
    from insiders import backup as B
    from insiders import newcoins as N
    assert B.KEEP == 2 and N.KEEP_TRADES_S == 12 * 3600                # defaults of 2026-11-26
    T.Store(tmp_path / "insiders_track.db").put("k", "v")
    for i in range(4):
        B.backup(tmp_path, keep=10, now=lambda i=i: 1000.0 + i)
    real, seen = shutil.disk_usage, []

    def usage(p):                                                         # the check runs with one old copy left
        seen.append(len([d for d in (tmp_path / B.DIR).iterdir() if d.is_dir()]))
        return real(p)
    monkeypatch.setattr(B.shutil, "disk_usage", usage)
    out = B.backup(tmp_path, keep=2, now=lambda: 2000.0)
    assert seen == [1] and B.verify(out) == []
    assert len([d for d in (tmp_path / B.DIR).iterdir() if d.is_dir()]) == 2


def test_old_cohorts_join_the_watchlist_read_only(tmp_path):
    from insiders.newcoins import SmartBook, cohort_wallets
    cw = cohort_wallets()
    assert {c["cohort"] for c in cw} == {"DEPTH", "STANK", "INSIDERS"} and not any(c.get("skip") for c in cw)
    st = T.Store(tmp_path / "db.sqlite")
    E.import_research(st, _folder(tmp_path))
    b = SmartBook(st)
    b.refresh(1e9)
    for c in cw[:20]:
        assert c["wallet"] in b.smart and b.smart[c["wallet"]]["cohort"] == c["cohort"]
    assert st.q("SELECT COUNT(*) n FROM wallets")[0]["n"] == 0             # the cohort table is never written


def test_wallet_file_keeps_old_wallets_and_adds_new_ones(tmp_path):
    st = T.Store(tmp_path / "db.sqlite")
    E.import_research(st, _folder(tmp_path))
    old = {"coins": {"OLD": "MintOld"}, "wallets": {"KeptWallet111111111111111111111111111111111": {
        "sources": ["coin:OLD"], "coins": {}, "early_coins": ["OLD"], "added": "2026-01-01"}}}
    db = E.build_wallet_db(st, old, now=lambda: 1795600000.0)
    kept = db["wallets"]["KeptWallet111111111111111111111111111111111"]
    assert kept["added"] == "2026-01-01" and kept["in_latest_build"] is False      # never removed
    assert {"OLD", "AAA", "BBB"} <= set(db["coins"])
    from insiders.newcoins import cohort_wallets
    for c in cohort_wallets()[:10]:
        assert db["wallets"][c["wallet"]]["cohort"] == c["cohort"] and c["cohort"] in db["wallets"][c["wallet"]]["sources"]
    E.write_wallet_db(db, tmp_path / "w.json", tmp_path / "w.csv")
    again = E.build_wallet_db(st, E.load_wallet_db(tmp_path / "w.json"))
    assert set(again["wallets"]) == set(db["wallets"])                    # a rebuild adds nothing twice


def test_the_committed_wallet_file_is_the_watchlist(tmp_path):
    from insiders.newcoins import SmartBook
    wdb = E.load_wallet_db()
    assert wdb and wdb["count"] == len(wdb["wallets"]) >= 370
    st = T.Store(tmp_path / "db.sqlite")
    E.import_research(st, _folder(tmp_path))
    b = SmartBook(st)
    b.refresh(1e9)
    assert set(b.smart) <= set(wdb["wallets"]) and len(b.smart) >= len(wdb["wallets"]) - 50


def test_retention_truncates_the_wal_and_the_backup_estimate_ignores_it(tmp_path, monkeypatch):
    import shutil
    from insiders import backup as B
    st = T.Store(tmp_path / "insiders_track.db")
    assert st.db.execute("PRAGMA journal_size_limit").fetchone()[0] == T.WAL_LIMIT_BYTES
    st.db.executemany("INSERT INTO nc_trade (mint, sig, wallet, buy, t, smart) VALUES ('m', ?, 'w', 1, 0, 0)",
                      [(f"s{i}",) for i in range(20000)])
    st.db.commit()
    from insiders.newcoins import Engine
    e = Engine(st, now=lambda: 10 * 86400)
    e.retention(10 * 86400)
    assert st.q("SELECT COUNT(*) n FROM nc_trade")[0]["n"] == 0
    assert e.stat["wal_checkpoint"] is not None and e.stat["wal_checkpoint"][0] == 0      # 0 = not busy, done
    wal = tmp_path / "insiders_track.db-wal"
    assert not wal.exists() or wal.stat().st_size <= T.WAL_LIMIT_BYTES
    st.db.close()
    big = tmp_path / "other"
    big.mkdir()
    (big / "insiders_track.db").write_bytes(b"x" * 1000)
    (big / "insiders_track.db-wal").write_bytes(b"x" * 900_000)          # a huge WAL does not count
    assert B.needed_bytes(big, ["insiders_track.db"]) == 1000


# ------------------------------------------------------------------------- page: watched wallets, "Thêm coin"
def test_add_coin_request_is_validated_queued_once_and_marked_done_after_research(tmp_path):
    from insiders import site as S
    NEW_MINT = "8DzqPteAAAkhozWJeWFwYS9ZiZLD2psU6BPiGFuwpump"                # not researched yet
    st = T.Store(tmp_path / "db.sqlite")
    folder = _folder(tmp_path)
    E.import_research(st, folder)
    req = lambda d: S.nc_request(st, json.dumps(d).encode())          # noqa: E731
    assert req({"mint": "bad", "symbol": "X"})[0] == 400
    assert req({"mint": NEW_MINT, "symbol": "bad name!"})[0] == 400
    code, d = req({"mint": NEW_MINT, "symbol": "newc"})
    assert code == 200 and d["symbol"] == "NEWC" and d["status"] == "REQUESTED"
    assert req({"mint": NEW_MINT, "symbol": "OTHER"})[0] == 409                # already queued
    assert req({"mint": "7uqfEqdd9strJVYiju6kasjCj6AzjBF2S1GmWRhdpump", "symbol": "NEWC"})[0] == 409   # name taken
    code, r = S.nc_get(None, st, "/api/nc/requests", "")
    assert code == 200 and r["requests"][0]["mint"] == NEW_MINT and r["requests"][0]["status"] == "REQUESTED"
    w = json.loads((folder / "window_AAA.json").read_text())              # the research of that coin lands in the seed
    w["coin"].update(symbol="NEWC", mint=NEW_MINT)
    (folder / "window_NEWC.json").write_text(json.dumps(w))
    E.import_research(st, folder)
    assert st.q("SELECT status FROM esm_pending_mint WHERE symbol='NEWC'")[0]["status"] == "DONE"
    assert "NEWC" not in [p["symbol"] for p in E.pending_mints(st)]       # never shown as an unverified mint
    assert S.nc_get(None, st, "/api/nc/wallets", "")[1]["count"] == E.load_wallet_db()["count"]


# --------------------------------------------------------------------- watched wallets: balances (2026-11-27)
def test_balances_never_show_zero_for_unknown_and_keep_the_last_value_on_errors(tmp_path):
    from insiders.portfolio import Balances
    st = T.Store(tmp_path / "db.sqlite")
    calls = []

    def rpc(m, p):
        calls.append(m)
        if len(calls) > 1:
            raise RuntimeError("HTTP 429")
        return {"value": [None, {"lamports": 2_500_000_000, "owner": "11111111111111111111111111111111"},
                          {"lamports": 10_000_000, "owner": "Vault1111111111111111111111111111111111111"}]}
    b = Balances(st, lambda: [D23, CKZ, ER9], rpc, sol_usd=lambda: 200.0)
    assert b.get(D23)["status"] == "never" and b.get(D23)["sol"] is None          # unknown is not 0
    b.refresh()
    assert len(calls) == 1                                                         # 3 wallets, one call (100 a call)
    assert b.get(D23) | {"t": 0} == {"sol": 0.0, "t": 0, "status": "empty", "owner": None, "usd": 0.0, "program": False}
    assert b.get(CKZ)["sol"] == 2.5 and b.get(CKZ)["usd"] == 500.0 and not b.get(CKZ)["program"]
    assert b.get(ER9)["program"]                                                   # owned by another program: a vault
    b.refresh()                                                                    # the RPC fails: last value kept, marked stale
    assert b.get(CKZ)["sol"] == 2.5 and b.get(CKZ)["status"] == "stale"
    assert Balances(st, lambda: [], rpc).get(CKZ)["sol"] == 2.5                   # saved: survives a restart


def test_token_lookups_are_cached_and_budgeted(tmp_path, monkeypatch):
    from insiders import portfolio as PF
    monkeypatch.setattr(PF, "TOKENS_PER_HOUR", 1)
    st = T.Store(tmp_path / "db.sqlite")
    n = []

    def rpc(m, p):
        n.append(m)
        return {"value": 1_000_000_000} if m == "getBalance" else {"value": []}
    b = PF.Balances(st, lambda: [], rpc, prices=PF.Prices(get=lambda u: {}), sol_usd=lambda: 100.0)
    assert b.tokens(CKZ)["status"] == "ok" and b.tokens(CKZ)["sol"] == 1.0 and len(n) == 3   # second read: cache
    assert b.tokens(ER9)["status"] == "budget"                                                # over the hourly budget


def test_add_a_wallet_by_hand(tmp_path):
    from insiders import site as S
    from insiders.newcoins import SmartBook
    st = T.Store(tmp_path / "db.sqlite")
    E.import_research(st, _folder(tmp_path))
    assert S.nc_watch(st, b'{"wallet": "bad"}')[0] == 400
    assert S.nc_watch(st, json.dumps({"wallet": SIX, "label": "ví bạn gửi"}).encode())[0] == 200
    assert S.nc_watch(st, json.dumps({"wallet": SIX}).encode())[0] == 409
    b = SmartBook(st)
    b.refresh(1e9)
    if SIX not in (E.load_wallet_db() or {}).get("wallets", {}):
        assert b.smart[SIX]["sources"] == ["manual"]
    code, d = S.nc_get(None, st, "/api/nc/wallet", f"/api/nc/wallet?w={SIX}")
    assert code == 200 and d["wallet"] == SIX and d["balance"] == {"status": "off"}
