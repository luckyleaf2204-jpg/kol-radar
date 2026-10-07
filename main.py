"""KOL Radar site — now the DEX Arbitrage Paper Lab (same site name, hostname and Render service). PAPER ONLY:
no wallet keys, no signing, no transaction is ever built for sending or sent.

usage:
  python main.py                       scanner + paper ledger + dashboard http://127.0.0.1:8780
  python main.py --no-scan             dashboard only
  python main.py --chains base,solana  limit the chains scanned
Env: APP_ACCESS_CODE (dashboard code, managed in Render), DEXARB_DB (default: dexarb.db next to KOL_DB on the
persistent disk), DEXARB_RPC_<CHAIN>, DEXARB_DB_MAX_MB, DEXARB_RETENTION=apply (else dry-run)."""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dexarb import report  # noqa: E402
from dexarb.app import Lab, db_path  # noqa: E402
from dexarb.store import Store  # noqa: E402
from dexarb.web import serve  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8780)))
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--chains", default=os.environ.get("DEXARB_CHAINS", ""))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    path, durable, why = db_path()
    store = Store(path)
    print(f"[dexarb] PAPER lab · db {path} (schema v{store.version()}, {store.size_mb():.1f} MB) · {why}")
    lab = Lab(store, [c for c in a.chains.split(",") if c] or None, paper_enabled=durable)
    code = os.environ.get("APP_ACCESS_CODE") or None
    if a.host not in ("127.0.0.1", "localhost") and not code:
        print("[dexarb] WARNING: public host without APP_ACCESS_CODE: anyone with the URL can read the dashboard")

    def js(fn):
        return lambda q: ("application/json", report.to_json(fn(q)))

    def health():
        return {"ok": True, "paper": True, "app": "dexarb", "schema_version": store.version(),
                "storage": {"db_path": str(path), "db_mb": round(store.size_mb(), 1), "durable": durable},
                "chains": {c: {"last_scan_age_s": round(time.time() - s["last_scan"]) if s["last_scan"] else None}
                           for c, s in lab.stats.items()}}
    routes = {
        "/api/overview": js(lambda q: report.overview(lab, store)),
        "/api/opportunities": js(lambda q: report.opportunities(store, int((q.get("limit") or [100])[0]),
                                                                (q.get("status") or [None])[0])),
        "/api/ledger": js(lambda q: report.ledger(store)),
        "/api/health": js(lambda q: report.health(store)),
        "/api/research": js(lambda q: report.research(store)),
        "/api/storage": js(lambda q: report.storage(store, (durable, why))),
        "/api/chains": js(lambda q: report.chain_list()),
    }
    stop = asyncio.Event()

    async def run():
        web = serve(routes, host=a.host, port=a.port, access_code=code, health=health)
        if a.no_scan:
            await web
            return
        await asyncio.gather(web, lab.run(stop))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("[dexarb] stopped")


if __name__ == "__main__":
    main()
