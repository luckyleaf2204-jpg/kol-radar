"""The Insiders site server (KOL Radar host): access code, routes, health, no secret in any response."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from insiders import site
from insiders.service import InsiderService

SECRET = "HELIUS-SECRET-site-0000"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_CODE", "code-1")
    monkeypatch.setenv("INSIDERS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    svc = InsiderService(result_path=tmp_path / "r.json", key=SECRET)
    svc.result = {"tokens": [{"mint": "M", "status": "ok", "buyers": [{"wallet": "w", "pre_call": True}]}],
                  "tickers": {"M": "TICK"}}
    httpd = site.serve(host="127.0.0.1", port=0, watch=False, svc=svc)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", svc
    httpd.shutdown()


def call(url, code=None, method="GET"):
    req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"X-Access-Code": code} if code else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_health_is_public_and_minimal(server):
    base, _ = server
    st, body = call(base + "/healthz")
    d = json.loads(body)
    assert st == 200 and d["app"] == "insiders" and d["paper"] is True and "tokens" not in body and SECRET not in body


def test_api_needs_the_code_and_never_returns_the_key(server):
    base, _ = server
    assert call(base + "/api/insiders")[0] == 401
    assert call(base + "/api/insiders", "wrong")[0] == 401
    st, body = call(base + "/api/insiders", "code-1")
    d = json.loads(body)
    assert st == 200 and d["result"]["tokens"][0]["n_buyers"] == 1 and SECRET not in body
    st, body = call(base + "/api/insiders/watch", "code-1")
    assert st == 200 and json.loads(body)["tickers"] == {"M": "TICK"}


def test_page_and_assets_are_served(server):
    base, _ = server
    st, html = call(base + "/")
    assert st == 200 and "INSIDERS" in html and "PAPER MODE" in html and "__V__" not in html
    v = html.split("/app.js?v=")[1].split('"')[0]
    assert len(v) == 10 and f"/styles.css?v={v}" in html                    # cache-busting tag from the files
    st, js = call(base + "/app.js")
    assert st == 200 and "function viewSignals" in js and "function viewPaper" in js and SECRET not in js
    assert call(base + "/styles.css")[0] == 200 and call(base + "/nope")[0] == 404


def test_new_dashboard_has_every_screen_and_never_signs():
    js = (site.PAGE / "app.js").read_text(encoding="utf-8")
    for tab in ("activity", "flows", "wallets", "settings"):                     # tracing only (2026-10-14)
        assert f'["{tab}",' in js
    assert "n_families" in js and "function viewFlows" in js and "QUÉT VÒNG" in js
    for bad in ("signTransaction", "sendTransaction", "Keypair", "/swap/v1/swap"):
        assert bad not in js


def test_legacy_research_page_is_kept(server):
    base, _ = server
    st, html = call(base + "/legacy/")
    assert st == 200 and "/legacy/app.js?v=" in html
    st, js = call(base + "/legacy/app.js")
    assert st == 200 and "function renderInsiders" in js and SECRET not in js
    assert call(base + "/legacy/styles.css")[0] == 200


def test_wallet_route_validates_and_needs_the_code(server, monkeypatch):
    base, svc = server

    class W:
        state = {}

        def wallet_view(self, addr):
            return {"wallet": addr, "role": "holder"} if addr.startswith("9Wz") else None
    monkeypatch.setattr(svc, "watcher", W(), raising=False)
    good = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    assert call(base + "/api/insiders/wallet?addr=" + good)[0] == 401
    assert call(base + "/api/insiders/wallet?addr=bad!", "code-1")[0] == 400
    st, body = call(base + "/api/insiders/wallet?addr=" + good, "code-1")
    assert st == 200 and json.loads(body)["role"] == "holder"
    assert call(base + "/api/insiders/wallet?addr=" + "G2YxRa6wt1qePMwfJzdXZG62ej4qaTC7YURzuh2Lwd3t", "code-1")[0] == 404


def test_rescan_route_is_post_only_and_guarded(server, monkeypatch):
    base, svc = server
    assert call(base + "/api/insiders/run", "code-1")[0] == 404                   # GET: no such route
    monkeypatch.setattr(svc, "start", lambda runner=None: True)
    st, body = call(base + "/api/insiders/run", "code-1", method="POST")
    assert st == 200 and json.loads(body)["started"] is True


def test_page_has_group_badges_and_filters(server):
    base, _ = server
    st, js = call(base + "/legacy/app.js")
    assert "function insGroup" in js and "const INS_TEAM" in js and "data-insfilter" in js and "BÁN HỘ DEV" in js



def test_chart_route_validates_the_mint(server, monkeypatch):
    base, svc = server
    monkeypatch.setattr(svc, "chart", lambda mint: {"candles": [[60, 1, 1, 1, 1, 1]], "agg": 1})
    assert call(base + "/api/insiders/chart?mint=bad!", "code-1")[0] == 400
    st, body = call(base + "/api/insiders/chart?mint=" + "7uqfEqdd9strJVYiju6kasjCj6AzjBF2S1GmWRhdpump", "code-1")
    assert st == 200 and json.loads(body)["candles"][0][0] == 60
    assert call(base + "/api/insiders/chart?mint=x", None)[0] == 401


def test_coin_route_validates_and_needs_the_code(server, monkeypatch):
    base, svc = server

    class W:
        state = {}

        def coin_view(self, mint):
            return {"mint": mint, "wallets": [{"wallet": "a"}, {"wallet": "b"}]} if mint.endswith("pump") else None
    monkeypatch.setattr(svc, "watcher", W(), raising=False)
    m = "7uqfEqdd9strJVYiju6kasjCj6AzjBF2S1GmWRhdpump"
    assert call(base + "/api/insiders/coin?mint=" + m)[0] == 401
    assert call(base + "/api/insiders/coin?mint=bad!", "code-1")[0] == 400
    st, body = call(base + "/api/insiders/coin?mint=" + m, "code-1")
    assert st == 200 and len(json.loads(body)["wallets"]) == 2
    assert call(base + "/api/insiders/coin?mint=" + "G2YxRa6wt1qePMwfJzdXZG62ej4qaTC7YURzuh2Lwd3t", "code-1")[0] == 404


def test_insiders_endpoints_survive_a_parent_cycle_600_wallets_and_devstats_errors(server, monkeypatch):
    """Production shape (2026-10-18): 600 followed wallets, devstats last_error HTTPError, and a parent loop left by a
    wallet pruned after 7 days and re-adopted under its own child. status() must stay JSON, not drop the socket."""
    from insiders import vip as VP
    from insiders.devstats import DevStats
    from insiders.watch import Watcher

    base, svc = server
    monkeypatch.setattr(VP, "MAX_WALLETS", 600)
    now = 1_800_000_000.0
    wallet = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    boss = "G2YxRa6wt1qePMwfJzdXZG62ej4qaTC7YURzuh2Lwd3t"
    a = "7GSMwnWB5YWmCwQax4bzfeDMvSZEogp6TAehxrJddDyr"
    b = "8xvHWupToGmcv5NoWek7J11LFGGJ7Nkx9Myp6n9LcSLH"
    mint = "7uqfEqdd9strJVYiju6kasjCj6AzjBF2S1GmWRhdpump"
    result = {"tokens": [{"mint": mint, "status": "ok", "buyers": [{"wallet": wallet, "pre_call": True}]}],
              "tickers": {mint: "TICK"},
              "vip": {"holders": [{"wallet": wallet, "profit_sol": 1, "coins": ["TICK"]}],
                      "successors": [{"wallet": boss, "kind": "person", "sol": 1, "from": [wallet]}]}}
    w = Watcher(result, svc.result_path, now=lambda: now)
    vs = VP.VipStream(w, result, rpc=lambda m, p: [], now=lambda: now)
    w.vip, w.mode = vs, "vip"
    kids = vs.state["children"]
    kids[a] = {"parent": b, "since": now, "sol": 1.0, "dir": "out"}          # loop, planted after load (live process)
    kids[b] = {"parent": a, "since": now - 1, "sol": 1.0, "dir": "out"}
    for i in range(596):
        kids[f"K{i:043d}"] = {"parent": wallet, "since": now - 10 - i, "sol": 0.5, "dir": "out"}
    w.state["coins"] = {mint: {"first_ts": None, "last_ts": now, "qualified": True, "symbol": "TICK", "wallets": {
        a: {"why": "a", "buy_sol": 2.0, "sell_sol": 0.0, "tok_in": 1, "tok_out": 0, "trades": [[None, "BUY", 2.0, 5, "s1"]]},
        b: {"why": "b", "buy_sol": 4.0, "sell_sol": 0.0, "tok_in": 1, "tok_out": 0, "trades": [[100, "BUY", 4.0, 5, "s2"]]}}}}
    ds = DevStats("k", lambda m, p: {"value": 0}, None, state=w.state, now=lambda: now, sleep=lambda s: None)
    ds.stat.update(done=28, errors=5, last_error="HTTPError")
    w.devstats = ds
    svc.result, svc.watcher = result, w

    st, body = call(base + "/healthz")
    h = json.loads(body)
    assert st == 200 and h["vip"]["wallets"] == 600
    assert h["devstats"]["errors"] == 5 and h["devstats"]["last_error"] == "HTTPError"
    assert call(base + "/api/insiders/watch")[0] == 401 and call(base + "/api/insiders")[0] == 401
    for path in ("/api/insiders/watch", "/api/insiders"):
        st, body = call(base + path, "code-1")
        d = json.loads(body)
        assert st == 200, path
        watch = d["watch"]
        assert watch["vip"]["wallets"] == 600 and watch["board"][0]["first_ts"] == 100
        assert watch["board"][0]["mint"] == mint and "api-key" not in body


def test_handler_returns_json_500_instead_of_dropping_the_socket(server, monkeypatch):
    base, svc = server

    class Boom:
        state = {"polls": 0}

        def status(self):
            raise RuntimeError("boom api-key=SECRET-SHOULD-NOT-LEAK")
    monkeypatch.setattr(svc, "watcher", Boom(), raising=False)
    for path in ("/api/insiders", "/api/insiders/watch"):
        st, body = call(base + path, "code-1")
        assert st == 500 and json.loads(body) == {"error": "internal"} and "SECRET" not in body

    class Bad:
        state = {"polls": 0}

        def status(self):
            return {"when": {1, 2}}                                 # a set is not JSON
    monkeypatch.setattr(svc, "watcher", Bad(), raising=False)
    st, body = call(base + "/api/insiders/watch", "code-1")
    assert st == 500 and json.loads(body) == {"error": "internal"}
