"""Smart Caller Hunter: extraction, tradable evaluation, caller scoring / tiers, Signal Score, X budget, ingest /
import / discovery, reports, and the site routes (offline, deterministic)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from callers import config as C
from callers.evaluate import classify, evaluate_call
from callers.extract import extract, is_mint, parse_post_url, snowflake_time
from callers.score import caller_metrics, signal_score, wilson_low
from callers.service import CallerHunter
from callers.store import Store
from callers.xapi import BudgetExceeded, XClient, XDisabled

MINT = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
MINT2 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T = 1_790_000_000.0


class Clock:
    def __init__(self, t=T):
        self.t = t

    def __call__(self):
        return self.t


def candles(t0, n, step, prices, liq_vol=1000.0):
    """prices: list of closes (high = close * 1.2, low = close * 0.9)."""
    return [[int(t0 + i * step), p, p * 1.2, p * 0.9, p, liq_vol] for i, p in enumerate(prices[:n])]


class FakeMarket:
    def __init__(self, path=None, snap=None, safety=None):
        self.path = path or {}
        self.snap = snap or {"symbol": "CAT", "price_usd": 1.0, "mcap": 1e6, "liquidity_usd": 100_000,
                             "volume_h1": 30_000, "change_h1": 10, "dex": "raydium"}
        self.saf = safety or {"mint_authority": None, "freeze_authority": None, "top10_share": 0.2, "error": None}
        self.pool_cache = {}
        self.tickers = {}

    def snapshot(self, mint):
        return dict(self.snap, mint=mint)

    def safety(self, mint):
        return dict(self.saf)

    def resolve_ticker(self, t, before):
        return self.tickers.get(t, (None, "ambiguous_2_candidates"))

    def candles(self, mint, t0, t1, minutes=1):
        f = self.path.get(mint)
        return f(t0, t1, minutes) if f else []


# --- extraction ----------------------------------------------------------------------------------------------------
def test_extract_mints_tickers_and_links():
    assert is_mint(MINT) and not is_mint("So11111111111111111111111111111111111111112") and not is_mint("abc")
    f = extract(f"aping $CAT now https://pump.fun/coin/{MINT} ca: {MINT2}", ["https://dexscreener.com/solana/" + MINT])
    assert f.mints == [MINT, MINT2] and f.tickers == ["CAT"] and f.buy_words
    assert extract("gm $SOL nice day").tickers == []
    assert extract("0OIl" * 10).mints == []                     # not base58


def test_post_url_and_snowflake_time():
    u, pid = parse_post_url("https://x.com/elonmusk/status/1585841080431321088?s=20")
    assert u == "elonmusk" and pid == "1585841080431321088"
    assert snowflake_time(pid) == pytest.approx(1666928951.734, abs=0.01)   # 2022-10-28 03:49:11 UTC
    assert parse_post_url("https://example.com/x") is None


# --- tradable evaluation -------------------------------------------------------------------------------------------
def test_entry_after_latency_and_tradable_vs_theoretical():
    post = T
    fine = candles(post - 3600, 7 * 60 + 60, 60, [1.0] * 60 + [1.0] * 2 + [2.0] * 400)    # doubles 2 min after the post
    coarse = candles(post - 86400, 900, 900, [1.0] * 96 + [3.0] * 800)
    o = evaluate_call(post, fine, coarse, liquidity=1_000_000, dex="raydium", now=post + 8 * 86400, liq_price=1.0)
    assert o["status"] == "OK" and o["entry_raw"] == 1.0 and o["entry_ts"] >= post + C.DETECT_LATENCY_S
    h = o["horizons"]
    assert h["1h"]["theoretical_gain"] == pytest.approx(2.0 * 1.2 - 1)              # the wick
    assert h["1h"]["tradable_gain"] < h["1h"]["theoretical_gain"]                     # costs + close, not wick
    fee = C.FEE_BY_DEX["raydium"]
    eff = 1.0 * (1 + fee + C.EXTRA_SLIPPAGE + C.TRADE_USD / 500_000)
    sell = 2.0 * (1 - fee - C.EXTRA_SLIPPAGE - C.TRADE_USD * 2 / (1_000_000 * 2 ** 0.5 / 2))   # liq ~ sqrt(price)
    assert h["1h"]["tradable_gain"] == pytest.approx(sell / eff - 1)
    assert o["cls"] == "WIN_2X" and o["final"] == 1 and not o["rug"]                  # 3x coarse later -> 7d best
    assert o["best_tradable_gain"] >= 1.0


def test_horizons_pending_while_live_and_no_price():
    post = T
    fine = candles(post - 60, 30, 60, [1.0] * 30)
    o = evaluate_call(post, fine, [], liquidity=50_000, dex="pump-fun", now=post + 600)
    assert o["horizons"]["1h"]["status"] == "SO_FAR" and o["horizons"]["7d"]["status"] == "SO_FAR" and o["final"] == 0
    n = evaluate_call(post, [], [], liquidity=None, dex=None, now=post + 600)
    assert n["status"] == "NO_PRICE" and n["cls"] == "UNKNOWN"


def test_rug_and_late_runup():
    post = T
    fine = candles(post - 3600, 120, 60, [0.4] * 60 + [1.0] * 5 + [0.05] * 55)          # +150 % before, -95 % after
    o = evaluate_call(post, fine, [], liquidity=40_000, dex="pump-fun", now=post + 3 * 86400)
    assert o["rug"] and o["runup_1h"] == pytest.approx(1.5) and o["cls"] == "MISS"


def test_classes():
    assert [classify(g) for g in (0.2, 0.6, 1.0, 2.5, 5.5, 12, None)] == \
        ["MISS", "NEAR", "WIN_2X", "WIN_3_5X", "WIN_6_10X", "MEGA_WIN", "UNKNOWN"]


# --- caller score / tiers -------------------------------------------------------------------------------------------
def row(gain, runup=0.1, rug=False, t=T, r24=0.1):
    return {"post_time": t, "o": {"status": "OK", "best_tradable_gain": gain, "runup_1h": runup, "rug": rug,
                                  "horizons": {"24h": {"return_at": r24}, "7d": {"time_to_peak_s": 3600}}}}


def test_min_ten_calls_before_any_rank():
    m = caller_metrics([row(5.0)] * 9)
    assert m["tier"] == "CANDIDATE"                             # 9 x 6x is still not ranked
    assert caller_metrics([row(5.0)] * 2)["tier"] == "NEW"


def test_elite_qualified_rejected():
    good = [row(3.0 if i % 2 else 1.2, r24=0.4 + 0.01 * i, t=T + i * 7 * 86400) for i in range(12)]
    m = caller_metrics(good)
    assert m["tier"] == "ELITE" and m["score"] >= 75 and m["wins_2x"] == 12
    late = [row(1.5, runup=2.0, t=T + i * 86400) for i in range(12)]
    assert caller_metrics(late)["tier"] == "REJECTED"           # chases pumps: most calls after +100 %
    rugs = [row(-0.95, rug=True) for _ in range(12)]
    assert caller_metrics(rugs)["tier"] == "REJECTED"
    assert wilson_low(0, 10) == 0 and 0 < wilson_low(5, 10) < 0.5


def test_unverified_calls_do_not_count():
    rows = [row(2.0) for _ in range(10)] + [{"post_time": T, "o": {"status": "NO_PRICE"}}] * 5
    m = caller_metrics(rows)
    assert m["calls"] == 15 and m["verified"] == 10 and m["unverified"] == 5


# --- Signal Score ----------------------------------------------------------------------------------------------------
def test_signal_caps_and_labels():
    snap = {"liquidity_usd": 200_000, "volume_h1": 80_000, "change_h1": 10}
    safe = {"mint_authority": None, "freeze_authority": None, "top10_share": 0.2}
    s = signal_score({"tier": "ELITE", "score": 90}, 3, snap, safe, True)
    assert s["label"] == "HIGH_PRIORITY" and s["score"] >= 80
    unproven = signal_score({"tier": "NEW", "score": 0}, 3, snap, safe, True)
    assert unproven["score"] <= 49 and "CALLER_NOT_PROVEN" in unproven["flags"]
    risky = signal_score({"tier": "ELITE", "score": 90}, 3, snap, dict(safe, mint_authority="X"), True)
    assert risky["score"] <= 49 and "MINT_AUTHORITY_SET" in risky["flags"]
    thin = signal_score({"tier": "ELITE", "score": 90}, 3, dict(snap, liquidity_usd=5_000), safe, True)
    assert thin["label"] in ("IGNORE",)


# --- X budget ---------------------------------------------------------------------------------------------------------
def test_x_is_off_by_default_and_respects_budget(tmp_path):
    st = Store(tmp_path / "c.db")
    calls = []
    x = XClient(st, token="SECRET-X-TOKEN", budget=0, transport=lambda u, t: calls.append(u) or {})
    assert not x.enabled
    with pytest.raises(XDisabled):
        x.search("pump.fun")
    x = XClient(st, token="SECRET-X-TOKEN", budget=1.0,
                transport=lambda u, t: calls.append(u) or {"data": [{"id": "1", "text": "x", "created_at":
                                                                     "2026-11-17T00:00:00Z"}] * 10,
                                                           "includes": {"users": [{"id": "9", "username": "a"}]}})
    x.search("pump.fun", max_results=10)                         # worst case 10 posts + 10 users = 0.15 $
    assert st.spent() == pytest.approx(10 * C.X_COST_PER_POST + 1 * C.X_COST_PER_USER)
    with pytest.raises(BudgetExceeded):                          # 100 posts + 100 users = 1.5 $ > what is left
        x.search("pump.fun", max_results=100)
    assert len(calls) == 1 and "SECRET" not in json.dumps(x.status())
    with pytest.raises(PermissionError):
        x._get("/tweets", {}, 0, 0)                              # no other endpoint (no posting)


# --- ingest / import / discovery / evaluation --------------------------------------------------------------------------
def make(tmp_path, market=None, x=None):
    clk = Clock()
    st = Store(tmp_path / "c.db", clock=clk)
    return CallerHunter(st, market or FakeMarket(), x, clock=clk, log=lambda *_: None), st, clk


def test_live_ingest_creates_call_and_signal(tmp_path):
    h, st, clk = make(tmp_path)
    ids = h.ingest_post("alpha", "111", clk() - 30, f"loading {MINT}", [], "x_timeline")
    assert len(ids) == 1 and st.q("SELECT live FROM calls")[0]["live"] == 1
    sig = st.q("SELECT label, score FROM signals")[0]
    assert sig["label"] == "IGNORE" and sig["score"] <= 49            # new caller: never above ignore
    old = h.ingest_post("alpha", "112", clk() - 3 * 86400, f"{MINT2}", [], "x_timeline")
    assert old and st.q("SELECT COUNT(*) n FROM signals")[0]["n"] == 1  # an old post is history, not a signal
    assert h.ingest_post("alpha", "111", clk() - 30, f"loading {MINT}", [], "x_timeline") == []   # idempotent


def test_ticker_only_calls_resolve_only_when_unambiguous(tmp_path):
    m = FakeMarket()
    h, st, clk = make(tmp_path, m)
    assert h.ingest_post("b", "1", clk() - 3600, "buy $WIF now", [], "x") == []
    assert st.kv("unresolved:1:WIF")["reason"].startswith("ambiguous")
    m.tickers["CAT"] = (MINT, "resolved_unique")
    ids = h.ingest_post("b", "2", clk() - 3600, "aping $CAT", [], "x")
    assert ids and st.q("SELECT resolve_note FROM calls WHERE id=?", (ids[0],))[0]["resolve_note"] == "resolved_unique"


def test_manual_import_uses_post_id_time(tmp_path):
    h, st, clk = make(tmp_path)
    pid = "1857000000000000000"
    r = h.import_text(f"https://x.com/Callr/status/{pid}, {MINT}\nbad line\nCallr2, 2026-11-01T10:00:00Z, {MINT2}")
    assert r["added"] == 2 and len(r["errors"]) == 1
    c = st.q("SELECT username, post_time FROM calls ORDER BY id")
    assert c[0]["username"] == "Callr" and c[0]["post_time"] == pytest.approx(snowflake_time(pid))


def test_channel_csv_seed(tmp_path):
    from pathlib import Path
    h, st, clk = make(tmp_path)
    n = h.import_channel_csv(Path(__file__).resolve().parents[1] / "insiders" / "sol_pump_insiders_calls.csv")
    assert n > 50 and st.q("SELECT platform FROM callers")[0]["platform"] == "telegram"


def test_discovery_promotes_after_two_mints(tmp_path):
    h, st, clk = make(tmp_path)
    u = {"id": "7", "name": "A", "public_metrics": {"followers_count": 5}}
    h.discover("hunter1", {"id": "1", "text": f"{MINT} cc @friend", "created_at": "2026-11-16T00:00:00Z",
                           "entities": {"mentions": [{"username": "friend"}]}}, u)
    assert st.q("SELECT promoted FROM discovery WHERE username='hunter1'")[0]["promoted"] == 0
    assert st.q("SELECT COUNT(*) n FROM callers")[0]["n"] == 0          # one mint: not a caller yet
    h.discover("hunter1", {"id": "2", "text": MINT2, "created_at": "2026-11-16T01:00:00Z"}, u)
    assert st.q("SELECT promoted FROM discovery WHERE username='hunter1'")[0]["promoted"] == 1
    assert st.q("SELECT refs FROM discovery WHERE username='friend'")[0]["refs"] == 1
    assert st.q("SELECT source FROM callers WHERE username='hunter1'")[0]["source"] == "discovery"


def test_evaluation_schedule_and_caller_refresh(tmp_path):
    post = T - 8 * 86400
    path = lambda t0, t1, m: candles(t0, int((t1 - t0) / (60 * m)) + 1, 60 * m, [1.0] * 70 + [4.0] * 2000)  # noqa
    h, st, clk = make(tmp_path, FakeMarket(path={MINT: path}))
    for i in range(10):
        h.ingest_post("pro", f"p{i}", post + i * 3600, MINT, [], "x")
    assert len(h.due_calls(50)) == 10
    for r in h.due_calls(50):
        h.evaluate(r)
    assert h.due_calls(50) == []                                    # all final (7 d passed)
    h.refresh_callers()
    c = st.q("SELECT tier, score, metrics FROM callers WHERE username='pro'")[0]
    assert json.loads(c["metrics"])["verified"] == 10 and c["tier"] in ("ELITE", "QUALIFIED", "CANDIDATE")


# --- site routes ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def site_server(tmp_path, monkeypatch):
    from insiders import site
    from insiders.service import InsiderService
    from callers.web import CallersApp
    monkeypatch.setenv("APP_ACCESS_CODE", "code-1")
    monkeypatch.setenv("INSIDERS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET-X-TOKEN")
    svc = InsiderService(result_path=tmp_path / "r.json", key="HELIUS-SECRET")
    app = CallersApp(tmp_path, start=False, market=FakeMarket())
    httpd = site.serve(host="127.0.0.1", port=0, watch=False, svc=svc, callers_app=app)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()


def call(url, code=None, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=body if method == "POST" else None,
                                 headers={"X-Access-Code": code} if code else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_site_routes_auth_pages_and_no_secret(site_server):
    base, app = site_server
    assert call(base + "/callers/")[0] == 200 and "Smart Caller Hunter" in call(base + "/callers/")[1]
    assert call(base + "/callers/app.js")[0] == 200
    assert call(base + "/api/callers/overview")[0] == 401
    for p in ("overview", "signals", "leaderboard", "coins", "backtest?h=6h&group=day", "watchlist"):
        code, body = call(base + "/api/callers/" + p, "code-1")
        assert code == 200 and "SECRET" not in body, p
    assert "callers" in json.loads(call(base + "/healthz")[1])
    code, body = call(base + f"/api/callers/watchlist?kind=coin&key={MINT}&op=add", "code-1", "POST", b"")
    assert code == 200 and json.loads(body)["coins"][0]["key"] == MINT
    assert call(base + "/api/callers/watchlist?kind=coin&key=nope&op=add", "code-1", "POST", b"")[0] == 400
    code, body = call(base + "/api/callers/import", "code-1", "POST",
                      json.dumps({"text": f"https://x.com/zed/status/1857000000000000000, {MINT2}"}).encode())
    assert code == 200 and json.loads(body)["added"] == 1
    lb = json.loads(call(base + "/api/callers/leaderboard", "code-1")[1])
    assert any(c["username"] == "zed" for c in lb["callers"])


def test_geckoterminal_calls_are_paced_and_retried_once():
    from callers.market import GT, Market
    clk = Clock()
    slept, seen = [], []

    def sleep(s):
        slept.append(s)
        clk.t += s

    def get(url):
        seen.append(url)
        if len(seen) == 2:
            raise OSError("HTTP Error 429: Too Many Requests")
        return {"ok": True}
    m = Market(get=get, sleep=sleep, now=clk, gt_gap_s=2.5)
    m.get(GT + "/a")
    m.get(GT + "/b")                                   # waits 2.5 s, hits 429, sleeps 10 s, retries
    m.get("https://api.dexscreener.com/x")            # not GeckoTerminal: not paced
    assert seen == [GT + "/a", GT + "/b", GT + "/b", "https://api.dexscreener.com/x"]
    assert slept[0] == pytest.approx(2.5) and 10 in slept


def test_old_call_liquidity_is_scaled_back_to_the_call_price():
    """A coin now at 50x with $1M liquidity had ~1M/sqrt(50) at the call: impact must use that, not today's pool."""
    post = T
    fine = candles(post - 3600, 120, 60, [0.02] * 60 + [0.02] * 2 + [0.04] * 58)
    naive = evaluate_call(post, fine, [], liquidity=1_000_000, dex="raydium", now=post + 8 * 86400)
    scaled = evaluate_call(post, fine, [], liquidity=1_000_000, dex="raydium", now=post + 8 * 86400, liq_price=1.0)
    assert scaled["liquidity_at_entry_est"] == pytest.approx(1_000_000 * 0.02 ** 0.5)
    assert scaled["buy_impact"] > naive["buy_impact"] and scaled["best_tradable_gain"] < naive["best_tradable_gain"]
