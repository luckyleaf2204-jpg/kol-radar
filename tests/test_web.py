"""Dashboard server: the access code guards every /api/* route; page and /healthz stay open."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot.web import serve  # noqa: E402

CRLF = "\r\n"


async def _get(port, path, headers=""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1{CRLF}Host: x{CRLF}{headers}{CRLF}".encode())
    await w.drain()
    data = await r.read()
    w.close()
    return data.split(b" ", 2)[1].decode(), data.split(b"\r\n\r\n", 1)[1]


def test_access_code():
    good, bad = "X-Access-Code: abc" + CRLF, "X-Access-Code: wrong" + CRLF

    async def main():
        routes = {"/api/state": lambda q: ("application/json", b'{"secret": 1}')}
        task = asyncio.create_task(serve(routes, port=18781, log=lambda *_: None, access_code="abc",
                                         health=lambda: {"ok": True}))
        await asyncio.sleep(0.2)
        try:
            assert (await _get(18781, "/api/state"))[0] == "401"
            assert (await _get(18781, "/api/state", bad))[0] == "401"
            code, body = await _get(18781, "/api/state?x=1", good)
            assert code == "200" and b"secret" in body
            assert (await _get(18781, "/api/nope", good))[0] == "404"
            assert (await _get(18781, "/api/nope"))[0] == "401"
            assert (await _get(18781, "/healthz"))[0] == "200"
            code, body = await _get(18781, "/")
            assert code == "200" and b"KOL Radar" in body
        finally:
            task.cancel()
    asyncio.run(main())


def test_all_routes_return_json(tmp_path):
    import json
    from kolbot.devs import DevTracker
    from kolbot.kolhist import KolHistory
    from kolbot.store import Store
    from kolbot.web import make_routes
    st = Store(tmp_path / "w.db")
    st.closed({"id": 1, "mint": "M", "kol": "K", "kol_sol": 1, "trigger_ts": 100, "entry_ts": 103.5, "entry_how": "q",
               "exit_ts": 200, "exit_kind": "kol_sold", "spend_sol": 0.1, "proceeds_sol": 0.05, "pnl_sol": -0.05,
               "net_pct": -50, "gap": False})
    st.db.commit()
    DevTracker(st.db).on_event({"kind": "create", "mint": "M", "name": "m", "symbol": "M", "creator": "D",
                                "user": "D", "ts": 90})
    KolHistory(st.db, {"K": {"name": "k"}}).tick(force=True)
    routes = make_routes(st.db, {"K": {"name": "k"}}, get_live=lambda: {"live": 1}, get_status=lambda: {"ok": 1})
    q = {"wallet": ["K"], "mint": ["M"]}
    for path in ("/api/summary", "/api/kols", "/api/kol", "/api/devs", "/api/dev", "/api/tokens", "/api/token",
                 "/api/trades", "/api/status", "/api/state"):
        ctype, body = routes[path](q)
        assert ctype == "application/json" and isinstance(json.loads(body), dict), path
    assert json.loads(routes["/api/kol"](q)[1])["n"] == 1
    assert json.loads(routes["/api/trades"](q)[1])["total"] == 1
    ctype, body = routes["/api/export.csv"]({})
    assert ctype.startswith("text/csv") and body.count(b"\n") == 2


def test_storage_info_durability(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("kolmain", Path(__file__).resolve().parents[1] / "main.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    db = tmp_path / "p.db"
    db.write_bytes(b"x" * 2_000_000)
    monkeypatch.delenv("RENDER", raising=False)
    info = m.storage_info(str(db))
    assert info["durable"] and info["db_mb"] == 2.0 and 0 <= info["disk_used_pct"] <= 100
    monkeypatch.setenv("RENDER", "true")                      # on Render only the mounted disk is durable
    assert not m.storage_info(str(db))["durable"] and m.storage_info(str(db))["warn"]
    assert m.storage_info("/var/data/paper.db")["durable"]
