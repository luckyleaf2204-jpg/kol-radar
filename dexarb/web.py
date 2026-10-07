"""Minimal asyncio HTTP server for the lab dashboard (same access-code scheme as before: env APP_ACCESS_CODE, header
X-Access-Code on every /api/* route). Every route is GET / read-only: there is no endpoint that builds, signs or
sends a transaction."""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

UI = Path(__file__).with_name("ui.html")


def _reply(writer, code: str, ctype: str, body: bytes, head_only: bool = False) -> None:
    writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                 "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + (b"" if head_only else body))


async def serve(routes: dict, host: str = "127.0.0.1", port: int = 8780, log=print, access_code: str | None = None,
                health=None):
    async def handle(reader, writer):
        try:
            line = (await reader.readline()).decode("latin-1")
            headers = {}
            while (h := await reader.readline()) not in (b"\r\n", b"\n", b""):
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            parts = line.split(" ")
            method, target = (parts[0], parts[1]) if len(parts) > 1 else ("GET", "/")
            u = urlsplit(target)
            path, query = u.path, parse_qs(u.query)
            head = method == "HEAD"
            if path.startswith("/api/"):
                if access_code and not hmac.compare_digest(headers.get("x-access-code", ""), access_code):
                    _reply(writer, "401 Unauthorized", "application/json", b'{"error":"code"}', head)
                elif path in routes:
                    h = routes[path]
                    need = "GET"
                    if isinstance(h, tuple):                  # write routes do not exist in the lab
                        raise ValueError("write routes are not allowed")
                    if need == "POST":
                        n = int(headers.get("content-length") or 0)
                        if n:
                            await reader.readexactly(min(n, 65536))
                    if (method == "POST") != (need == "POST"):
                        _reply(writer, "405 Method Not Allowed", "application/json", b'{"error":"method"}', head)
                    else:
                        ctype, body = h(query)
                        _reply(writer, "200 OK", ctype, body, head)
                else:
                    _reply(writer, "404 Not Found", "application/json", b'{"error":"route"}', head)
            elif path == "/healthz":
                _reply(writer, "200 OK", "application/json", json.dumps(health() if health else {"ok": True}).encode(),
                       head)
            elif path in ("/", "/index.html"):
                _reply(writer, "200 OK", "text/html; charset=utf-8", UI.read_bytes(), head)
            else:
                _reply(writer, "404 Not Found", "text/plain", b"", head)
        except Exception as e:
            log(f"[web] {type(e).__name__}: {e}")
            try:
                _reply(writer, "500 Internal Server Error", "application/json", b'{"error":"server"}')
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port)
    log(f"[web] dashboard http://{host}:{port}")
    async with server:
        await server.serve_forever()
