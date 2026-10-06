"""Dashboard server: the access code guards /api/state; page and /healthz stay open."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot.web import serve  # noqa: E402


async def _get(port, path, headers=""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode())
    await w.drain()
    data = await r.read()
    w.close()
    return data.split(b" ", 2)[1].decode(), data.split(b"\r\n\r\n", 1)[1]


def test_access_code():
    async def main():
        task = asyncio.create_task(serve(lambda: {"secret": 1}, port=18781, log=lambda *_: None,
                                         access_code="abc", health=lambda: {"ok": True}))
        await asyncio.sleep(0.2)
        try:
            assert (await _get(18781, "/api/state"))[0] == "401"
            assert (await _get(18781, "/api/state", "X-Access-Code: wrong\r\n"))[0] == "401"
            code, body = await _get(18781, "/api/state?x=1", "X-Access-Code: abc\r\n")
            assert code == "200" and b"secret" in body
            assert (await _get(18781, "/healthz"))[0] == "200"
            code, body = await _get(18781, "/")
            assert code == "200" and b"KOL Radar" in body
        finally:
            task.cancel()
    asyncio.run(main())
