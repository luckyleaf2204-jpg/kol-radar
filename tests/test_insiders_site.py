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
    assert st == 200 and "/app.js?v=" in html and "Insiders" in html
    st, js = call(base + "/app.js")
    assert st == 200 and "function renderInsiders" in js and SECRET not in js
    assert call(base + "/styles.css")[0] == 200 and call(base + "/nope")[0] == 404


def test_rescan_route_is_post_only_and_guarded(server, monkeypatch):
    base, svc = server
    assert call(base + "/api/insiders/run", "code-1")[0] == 404                   # GET: no such route
    monkeypatch.setattr(svc, "start", lambda runner=None: True)
    st, body = call(base + "/api/insiders/run", "code-1", method="POST")
    assert st == 200 and json.loads(body)["started"] is True
