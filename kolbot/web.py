"""Local dashboard: a tiny HTTP server (stdlib asyncio) serving ui.html and /api/state as JSON. Binds to 127.0.0.1
only. Read-only: there is nothing to click that trades."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from pathlib import Path

from kolbot.engine import Curve, sell_sol
from kolbot.report import by_kol, summarize
from kolbot.watch import SUPPLY_TOKENS

UI = Path(__file__).with_name("ui.html")
RAW_SUPPLY = SUPPLY_TOKENS * 10 ** 6


def build_state(eng, watch, meta, names: dict[str, str], status: dict) -> dict:
    sol_usd = meta.sol_usd
    tokens = []
    for r in watch.rows():
        meta.want(r["mint"])
        coin = meta.coins.get(r["mint"]) or {}
        creator = coin.get("creator")
        dev_net = r["net"].get(creator) if creator else None
        pos = eng.positions.get(r["mint"])
        paper = None
        if pos:
            c = eng.curves.get(pos.mint) or (Curve(**pos.curve) if pos.curve else None)
            now_sol = max(0.0, sell_sol(c, pos.tokens, eng.cfg)) if c else 0.0
            paper = {"entry_mc_sol": pos.entry_px * RAW_SUPPLY, "spend_sol": pos.spend_sol,
                     "pct": 100 * (now_sol / pos.spend_sol - 1), "kol": names.get(pos.kol, pos.kol[:8]),
                     "selling": pos.sell_due_ts is not None}
        r = {k: v for k, v in r.items() if k != "net"}
        r.update({"coin": coin, "creator": creator, "creator_rating": meta.creators.get(creator) if creator else None,
                  "dev_sold": dev_net is not None and dev_net > 0, "paper": paper})
        tokens.append(r)
    s = summarize(eng.closed)
    closed = [dict(c, kol_name=names.get(c["kol"], c["kol"][:8]),
                   symbol=(meta.coins.get(c["mint"]) or {}).get("symbol")) for c in eng.closed[-40:]][::-1]
    return {"now": time.time(), "sol_usd": sol_usd, "status": status, "tokens": tokens,
            "feed": [dict(a, symbol=(meta.coins.get(a["mint"]) or {}).get("symbol")) for a in list(watch.feed)[:60]],
            "paper": {"cash": eng.cash, "equity": eng.equity(), "start": eng.cfg.starting_sol, "open": len(eng.positions),
                      "summary": s, "by_kol": by_kol(eng.closed, names, 10), "counts": eng.counts, "closed": closed,
                      "settings": {k: getattr(eng.cfg, k) for k in ("position_sol", "delay_s", "max_open",
                                                                    "min_kol_buy_sol", "extra_slippage_pct")}},
            "kols_tracked": len(names)}


def _reply(writer, code: str, ctype: str, body: bytes, head_only: bool = False) -> None:
    writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                 "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + (b"" if head_only else body))


async def serve(get_state, host: str = "127.0.0.1", port: int = 8780, log=print, access_code: str | None = None,
                health=None):
    """access_code set (env APP_ACCESS_CODE on a server): /api/state needs header X-Access-Code. The page and
    /healthz carry no data and stay open."""
    async def handle(reader, writer):
        try:
            line = (await reader.readline()).decode("latin-1")
            headers = {}
            while (h := await reader.readline()) not in (b"\r\n", b"\n", b""):
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            parts = line.split(" ")
            method, path = (parts[0], parts[1].split("?")[0]) if len(parts) > 1 else ("GET", "/")
            head = method == "HEAD"
            if path == "/api/state":
                if access_code and not hmac.compare_digest(headers.get("x-access-code", ""), access_code):
                    _reply(writer, "401 Unauthorized", "application/json", b'{"error":"code"}', head)
                else:
                    _reply(writer, "200 OK", "application/json", json.dumps(get_state(), default=str).encode(), head)
            elif path == "/healthz":
                _reply(writer, "200 OK", "application/json", json.dumps(health() if health else {"ok": True}).encode(),
                       head)
            elif path in ("/", "/index.html"):
                _reply(writer, "200 OK", "text/html; charset=utf-8", UI.read_bytes(), head)
            else:
                _reply(writer, "404 Not Found", "text/plain", b"", head)
        except Exception as e:
            log(f"[web] {type(e).__name__}: {e}")
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
