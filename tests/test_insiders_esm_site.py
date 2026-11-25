"""Early Smart Money page + API on the site server: access code, validation, real rows from the v5 tables (synthetic
research folder, no network), CREATOR_GROUP never ranked, gzip for large bodies, keep-alive."""
import gzip
import http.client
import json
import threading

import pytest

from insiders import site
from insiders.service import InsiderService
from tests.test_insiders_esm import CRE_A, W1, W2, research


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_CODE", "code-1")
    monkeypatch.setenv("INSIDERS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ESM_RESEARCH_DIR", str(research(tmp_path)))
    httpd = site.serve(host="127.0.0.1", port=0, watch=False, svc=InsiderService(result_path=tmp_path / "r.json", key="K"), callers_app=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def get(port, path, code="code-1", gz=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-Access-Code": code} if code else {}
    if gz:
        h["Accept-Encoding"] = "gzip"
    c.request("GET", path, headers=h)
    r = c.getresponse()
    body = r.read()
    if r.getheader("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return r.status, body.decode(), r


def test_page_is_served_and_api_needs_the_code(server):
    s, body, _ = get(server, "/early/", code=None)
    assert s == 200 and "/early/early.js?v=" in body and "__V__" not in body
    assert get(server, "/early/early.js", code=None)[0] == 200 and get(server, "/early/early.css", code=None)[0] == 200
    assert get(server, "/api/insiders/esm/summary", code=None)[0] == 401
    assert get(server, "/api/insiders/esm/summary", code="bad")[0] == 401


def test_real_rows_and_creator_group_never_ranked(server):
    s, body, _ = get(server, "/api/insiders/esm/summary")
    sm = json.loads(body)
    assert s == 200 and sm["imported"] and sm["coins"] == 2
    ws = json.loads(get(server, "/api/insiders/esm/wallets")[1])["wallets"]
    ranked = [w["wallet"] for w in ws if w["ranked"]]
    assert W1 in ranked and CRE_A not in ranked
    cre = next(w for w in ws if w["wallet"] == CRE_A)
    assert cre["score"] is None and "CREATOR_GROUP" in cre["flags"]
    d = json.loads(get(server, f"/api/insiders/esm/wallet?wallet={CRE_A}")[1])
    assert d["coins"][0]["bucket"] == "0-15" and d["rank"] is None and "CREATOR_GROUP" in d["flags"]
    assert any(lk["signal"] == "direct_transfer" and lk["strength"] == "STRONG" for lk in d["links"])
    assert W2 in {w["wallet"] for w in ws if w["ranked"]}
    e = json.loads(get(server, "/api/insiders/esm/entity?id=E001")[1])
    assert e["summary"]["ranked"] is False and any(m.get("non_buyer") for m in e["members"])
    c = json.loads(get(server, "/api/insiders/esm/coin?mint=MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")[1])
    assert c == {"error": "coin"}


def test_inputs_are_validated(server):
    assert get(server, "/api/insiders/esm/wallet?wallet=not-an-address")[0] == 400
    assert get(server, "/api/insiders/esm/entity?id=DROP%20TABLE")[0] == 400
    assert get(server, "/api/insiders/esm/nope")[0] == 404


def test_large_bodies_are_gzipped_and_connections_kept_alive(server):
    s, body, r = get(server, "/early/early.js", code=None, gz=True)
    assert s == 200 and r.getheader("Content-Encoding") == "gzip" and "Early Smart Money" in body
    s, body, r = get(server, "/early/early.js", code=None, gz=False)
    assert r.getheader("Content-Encoding") is None and "Early Smart Money" in body
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    for _ in range(3):                                         # one connection, three requests (HTTP/1.1 keep-alive)
        c.request("GET", "/early/early.css")
        assert c.getresponse().read()
