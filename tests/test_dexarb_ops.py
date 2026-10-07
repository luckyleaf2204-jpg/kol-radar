"""Storage, paper-only guarantees, KOL cleanup scope, site config kept, prereg <-> code, API."""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from dexarb_fakes import VENUES, WETH, FakeEvm
from test_dexarb_core import Clock, lab, run_until
from dexarb import config as C
from dexarb import report
from dexarb.protocols import http_get_json
from dexarb.rpc import READ_ONLY, ForbiddenMethod, Rpc
from dexarb.store import Store, restore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


# --- storage ---------------------------------------------------------------------------------------------------------
def test_new_db_is_clean_versioned_and_refuses_old_files(tmp_path):
    st = Store(tmp_path / "dexarb.db")
    names = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("chains", "dex_protocols", "assets", "pools", "quote_snapshots", "opportunities", "simulation_results",
              "paper_accounts", "paper_balances", "paper_positions", "paper_legs", "paper_cycles", "fee_snapshots",
              "oracle_prices", "feed_health", "data_gaps", "experiment_config", "experiment_runs", "daily_rollups",
              "db_storage_metrics", "audit_events"):
        assert t in names
    assert not names & {"kols", "kol_roster", "trades", "sw_trades", "signals", "devs", "tokens"}
    assert st.version() == 1 and st.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    for bad in ("paper.db", "signal_paper_s1.db", "s3_signals.db", "mc.db"):
        with pytest.raises(ValueError):
            Store(tmp_path / bad)
    Store(tmp_path / "dexarb.db")                                    # reopening: schema created once
    assert Store(tmp_path / "dexarb.db").db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_quota_stops_raw_writes_but_counts_and_gaps(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    st.max_mb = 0.00001
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    assert st.db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
    assert st.db.execute("SELECT SUM(n) FROM opportunity_rollups").fetchone()[0] > 0
    assert st.db.execute("SELECT COUNT(*) FROM data_gaps WHERE reason='db_quota'").fetchone()[0] >= 1


def test_retention_dry_run_then_apply_keeps_referenced_rows(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, 30)
    n_legs = st.db.execute("SELECT COUNT(*) FROM paper_legs").fetchone()[0]
    later = clk() + 40 * 86400
    audits0 = st.db.execute("SELECT COUNT(*) FROM audit_events WHERE kind='retention'").fetchone()[0]
    dry = st.retention(later, dry_run=True)
    assert dry["dry_run"] and dry["deleted"] == 0 and dry["quotes_eligible"] > 0
    before = st.db.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    res = st.retention(later, dry_run=False)
    after = st.db.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    assert res["deleted"] > 0 and after < before
    refs = st.db.execute("SELECT COUNT(*) FROM paper_legs l LEFT JOIN quote_snapshots q ON q.id=l.exec_quote_id "
                         "WHERE l.exec_quote_id IS NOT NULL AND q.id IS NULL").fetchone()[0]
    assert refs == 0 and st.db.execute("SELECT COUNT(*) FROM paper_legs").fetchone()[0] == n_legs
    assert st.db.execute("SELECT COUNT(*) FROM audit_events WHERE kind='retention'").fetchone()[0] == audits0 + 2


def test_backup_and_restore(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    lb.scan("base")
    b = st.backup(tmp_path / "bk" / "dexarb.bak")
    assert b["integrity"] == "ok" and b["bytes"] > 0
    r = restore(tmp_path / "bk" / "dexarb.bak", tmp_path / "restored.db")
    assert r == {"integrity": "ok", "schema_version": 1}
    with pytest.raises(FileExistsError):
        restore(tmp_path / "bk" / "dexarb.bak", tmp_path / "restored.db")
    c = sqlite3.connect(tmp_path / "restored.db")
    assert c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == \
        st.db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]


def test_storage_metrics_and_bytes_per_day(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    st.record_metrics()
    lb.scan("base")
    clk.t += 3600
    st.record_metrics()
    bpd = st.bytes_per_day()
    assert bpd["opportunity_rollups"] > 0 and "paper_cycles" in bpd


# --- paper-only ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("m", ["eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "eth_signTransaction",
                               "personal_sign", "sendTransaction", "simulateTransaction", "requestAirdrop",
                               "wallet_sendCalls", "eth_requestAccounts"])
def test_rpc_refuses_signing_and_sending(m):
    sent = []
    rpc = Rpc("x", transport=lambda b: sent.append(b) or b"{}")
    with pytest.raises(ForbiddenMethod):
        rpc.call(m, [])
    with pytest.raises(ForbiddenMethod):
        rpc.batch([("eth_blockNumber", []), (m, [])])
    assert not sent and m not in READ_ONLY


@pytest.mark.parametrize("url", ["https://lite-api.jup.ag/swap/v1/swap", "https://lite-api.jup.ag/swap/v1/swap-instructions",
                                 "https://api.mainnet-beta.solana.com/", "https://example.com/quote"])
def test_http_client_only_reads_quotes(url):
    with pytest.raises(PermissionError):
        http_get_json(url)


def test_no_signing_or_key_code_in_runtime_path():
    banned = re.compile(r"sendRawTransaction|sendTransaction|signTransaction|sign_transaction|signMessage|"
                        r"private_?key|secret_?key|mnemonic|seed_?phrase|Keypair|eth_account|solders|web3|"
                        r"swap-instructions|swap/v1/swap\b", re.I)
    hits = []
    for f in list((ROOT / "dexarb").glob("*.py")) + [ROOT / "main.py"]:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line) and f.name != "rpc.py":
                hits.append(f"{f.name}:{i}: {line.strip()}")
    assert hits == []
    assert "POST" not in (ROOT / "dexarb" / "ui.html").read_text(encoding="utf-8")


# --- KOL cleanup -----------------------------------------------------------------------------------------------------
def old_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE kols (wallet TEXT PRIMARY KEY, display_name TEXT); CREATE TABLE kol_roster (wallet TEXT PRIMARY KEY);
        CREATE TABLE kol_daily (date TEXT, wallet TEXT); CREATE TABLE trades (id INTEGER PRIMARY KEY, mint TEXT, kol TEXT);
        CREATE TABLE gaps (start REAL, end REAL); CREATE TABLE state (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE signals (id INTEGER PRIMARY KEY, source TEXT, wallet TEXT);
        CREATE TABLE sw_trades (id INTEGER PRIMARY KEY, wallet TEXT); CREATE TABLE tokens (mint TEXT PRIMARY KEY);
        CREATE TABLE devs (wallet TEXT PRIMARY KEY);
        INSERT INTO kols VALUES ('K1','a'),('K2','b'); INSERT INTO kol_roster VALUES ('K1'),('K2');
        INSERT INTO kol_daily VALUES ('d','K1'); INSERT INTO trades VALUES (1,'m','K1'); INSERT INTO gaps VALUES (1,2);
        INSERT INTO state VALUES ('engine','{}'),('other','keep');
        INSERT INTO signals VALUES (1,'kol','K1'),(2,'smart','S1');
        INSERT INTO sw_trades VALUES (1,'K1'),(2,'S1'); INSERT INTO tokens VALUES ('m'); INSERT INTO devs VALUES ('D');""")
    c.commit()
    c.close()


def test_kol_cleanup_dry_run_scope_and_guarded_apply(tmp_path):
    import kol_cleanup as K
    db = tmp_path / "paper.db"
    old_db(db)
    for f in ("signal_paper_kol5.db", "signal_paper_kol10.db", "signal_paper_s1.db"):
        (tmp_path / f).write_bytes(b"x" * 10)
    dry = K.main(["--db", str(db)])
    p = dry["plan"]
    assert set(p["tables"]) == {"kols", "kol_roster", "kol_daily", "trades", "gaps"}
    assert p["rows"] == {"state WHERE k = 'engine'": 1, "signals WHERE source = 'kol'": 1}
    assert set(p["files"]) == {"signal_paper_kol5.db", "signal_paper_kol10.db"} and p["ambiguous"]["sw_trades"] == 1
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM kols").fetchone()[0] == 2      # dry run changed nothing
    with pytest.raises(SystemExit):
        K.main(["--db", str(db), "--apply", "--expect-fingerprint", p["fingerprint"]])          # no backup confirmation
    with pytest.raises(SystemExit):
        K.main(["--db", str(db), "--apply", "--backup-confirmed", "snap", "--expect-fingerprint", "wrong"])
    K.main(["--db", str(db), "--apply", "--backup-confirmed", "snap", "--expect-fingerprint", p["fingerprint"]])
    c = sqlite3.connect(db)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"kols", "kol_roster", "kol_daily", "trades", "gaps"}
    assert c.execute("SELECT k FROM state").fetchall() == [("other",)]
    assert c.execute("SELECT source FROM signals").fetchall() == [("smart",)]
    assert c.execute("SELECT COUNT(*) FROM sw_trades").fetchone()[0] == 2                   # ambiguous rows kept
    assert c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1
    assert not (tmp_path / "signal_paper_kol5.db").exists() and (tmp_path / "signal_paper_s1.db").exists()


def test_kol_cleanup_refuses_a_non_kol_database(tmp_path):
    import kol_cleanup as K
    st = Store(tmp_path / "dexarb.db")
    st.db.close()
    with pytest.raises(SystemExit):
        K.main(["--db", str(tmp_path / "dexarb.db")])


# --- site config kept ------------------------------------------------------------------------------------------------
def test_site_name_hostname_and_deploy_config_kept():
    y = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "name: kol-radar" in y and "startCommand: python main.py --host 0.0.0.0" in y
    assert "mountPath: /var/data" in y and "value: /var/data/paper.db" in y and "healthCheckPath: /healthz" in y
    assert re.search(r"APP_ACCESS_CODE\s+#.*\n\s+sync: false", y)
    ui = (ROOT / "dexarb" / "ui.html").read_text(encoding="utf-8")
    assert "<title>KOL Radar</title>" in ui and "kolCode" in ui                # same name, same stored access code
    assert not (ROOT / "kolbot").exists() and not (ROOT / "kols.json").exists()


# --- prereg <-> code -------------------------------------------------------------------------------------------------
def test_prereg_document_matches_config():
    doc = re.sub(r"\s+", " ", (ROOT / "docs" / "prereg_dex_arbitrage.md").read_text(encoding="utf-8"))
    for text in ("10 bps of size + 50 % of (gas + setup)", "net_profit_after_buffer > 5 bps of size",
                 "Sizes 100 and 1,000 quote units", "impact_high (> 1 % on a leg)", "within 5 s",
                 "`A_fast` (detection → leg 1: 2 s; leg 1 → leg 2: 4 s)", "`A_slow` (5 s; 15 s)",
                 "min-out = detection quote × (1 − 0.5 %)", "retried every 30 s", "After 1 h",
                 "Capital 10,000 quote units", "worth 50 quote units", "max 1 open cycle per token and 3 per chain",
                 "of all other rejections 1 % is stored raw", "< 30 closed signal cycles or < 7 days", "first 24 h"):
        assert text in doc, text
    assert C.BUFFER_BPS == 10 and C.BUFFER_COST_SHARE == 0.5 and C.MIN_NET_BPS == 5 and C.SIZES == (100.0, 1000.0)
    assert C.MAX_IMPACT == 0.01 and C.MAX_QUOTE_AGE_S == 5 and C.ARMS == {"A_fast": (2.0, 4.0), "A_slow": (5.0, 15.0)}
    assert C.SLIPPAGE_TOL_BPS == 50 and C.RETRY_S == 30 and C.EXIT_ANY_VENUE_AFTER_S == 3600
    assert C.CAPITAL == 10_000 and C.NATIVE_FLOAT_QUOTE == 50 and (C.MAX_OPEN_PER_TOKEN, C.MAX_OPEN_PER_CHAIN) == (1, 3)
    assert C.NEG_SAMPLE == 0.01 and (C.EVAL_MIN_CYCLES, C.EVAL_MIN_DAYS) == (30, 7)


# --- API / report ----------------------------------------------------------------------------------------------------
def test_report_pages_and_verdicts(tmp_path):
    lb, st, evm, clk = lab(tmp_path)
    evm.price(VENUES["uniswap_v2"], WETH.address, 0.98)
    lb.scan("base")
    run_until(lb, clk, 30)
    ov = report.overview(lb, st)
    assert ov["paper_only"] and ov["verdicts"] == [] or all(v["verdict"] == "NO_EVIDENCE_YET" for v in ov["verdicts"])
    clk.t += 86400 + 10                                              # past warm-up: still far below 30 cycles
    lb.scan("base")
    run_until(lb, clk, 30)
    vs = report.verdicts(st.db)
    assert vs and all(v["verdict"] == "NO_EVIDENCE_YET" for v in vs)
    opp = report.opportunities(st, 10)
    assert opp and "net_profit_after_buffer" in opp[0] and "buy_route" in opp[0]
    for page in (report.ledger(st), report.health(st), report.research(st), report.storage(st, (True, "local"))):
        json.loads(report.to_json(page))


def test_paper_disabled_when_disk_not_durable(tmp_path, monkeypatch):
    from dexarb.app import db_path
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DEXARB_DB", str(tmp_path / "dexarb.db"))
    p, durable, why = db_path()
    assert not durable and "disabled" in why
    monkeypatch.delenv("DEXARB_DB")
    monkeypatch.setenv("KOL_DB", "/var/data/paper.db")
    assert db_path()[0].as_posix() == "/var/data/dexarb.db" and db_path()[1]
    lb, st, evm, clk = lab(tmp_path / "x")
    assert lb.paper_enabled


def test_rollup_keeps_null_gross_for_failed_quotes(tmp_path):
    st = Store(tmp_path / "dexarb.db")
    st.rollup("d", "bnb", "ETH", 1000, "a", "b", "quote_failed", None)
    st.rollup("d", "bnb", "ETH", 1000, "a", "b", "quote_failed", None)
    st.rollup("d", "bnb", "ETH", 1000, "a", "c", "negative_spread", -0.5)
    st.rollup("d", "bnb", "ETH", 1000, "a", "c", "negative_spread", -0.2)
    rows = dict(((r[0], r[1]), (r[2], r[3])) for r in st.db.execute(
        "SELECT reason, sell_protocol, n, best_gross FROM opportunity_rollups"))
    assert rows[("quote_failed", "b")] == (2, None) and rows[("negative_spread", "c")] == (2, -0.2)


def test_baseline_respects_execution_limits(tmp_path):
    from dexarb_fakes import CBBTC
    lb, st, evm, clk = lab(tmp_path)
    for v in VENUES.values():                                     # every pool tiny: any 100+ trade has > 1 % impact
        for tok in (WETH.address, CBBTC.address):
            q_, t_ = evm.reserves[v][tok]
            evm.reserves[v][tok] = (q_ // 100_000, t_ // 100_000)
    lb.scan("base")
    assert st.db.execute("SELECT COUNT(*) FROM paper_cycles WHERE baseline=1").fetchone()[0] == 0
    assert st.db.execute("SELECT COUNT(*) FROM audit_events WHERE kind='baseline_skipped'").fetchone()[0] == 2
