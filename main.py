"""PAPER KOL copy bot for pump.fun (direction C). Listens to every pump.fun bonding-curve trade, copies the buys
of the KOL wallets in kols.json on paper, and sells when the KOL sells. No wallet, no key, no transaction.

usage:
  python main.py                 run + dashboard http://127.0.0.1:8780 (Ctrl+C to stop; state kept in data/paper.db)
  python main.py --report        print the results so far"""
import argparse
import asyncio
import json
import os
import urllib.request
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kolbot.config import Config  # noqa: E402
from kolbot.engine import Engine  # noqa: E402
from kolbot.meta import Meta  # noqa: E402
from kolbot.report import by_kol, summarize  # noqa: E402
from kolbot.store import Store  # noqa: E402
from kolbot.stream import listen  # noqa: E402
from kolbot.watch import Watch  # noqa: E402
from kolbot.web import build_state, serve  # noqa: E402


def load_kols(path: Path) -> dict[str, str]:
    return {r["wallet"]: r.get("name") or r["wallet"][:8]
            for r in json.loads(path.read_text(encoding="utf-8"))["wallets"]}


def report(eng: Engine, names: dict[str, str]) -> str:
    s = summarize(eng.closed)
    L = [f"PAPER · cash {eng.cash:.3f} SOL · equity {eng.equity():.3f} SOL (start {eng.cfg.starting_sol}) · "
         f"open {len(eng.positions)}",
         f"{s['status']} n={s['n']} · {s.get('verdict', 'Chưa có lệnh đóng')}"]
    if s["n"]:
        L.append(f"PnL {s['pnl_sol']:+.4f} SOL · mean {s['mean_net_pct']:+.2f}% · median {s['median_net_pct']:+.2f}% · "
                 f"win {s['win_rate_pct']}% · CI95 theo KOL {s['ci95_by_kol']} · exits {s['exit_kinds']}")
        L.append("KOL (lệnh, PnL SOL, TB %): " + "; ".join(f"{a} {b}, {c:+}, {d:+}%" for a, b, c, d in by_kol(
            eng.closed, names)))
    L.append(f"KOL buys seen {eng.counts['kol_buys']} · copied {eng.counts['copied']} · skipped {eng.counts['skipped']}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--kols", default=str(ROOT / "kols.json"))
    ap.add_argument("--db", default=str(ROOT / "data" / "paper.db"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8780)))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    cfg = Config.load(Path(a.config))
    names = load_kols(Path(a.kols))
    eng = Engine(cfg, set(names), Store(Path(a.db)))
    if a.report:
        print(report(eng, names))
        return
    print(f"[kol] PAPER bot · {len(names)} KOL wallets · settings {cfg.as_dict()}")
    stop = asyncio.Event()
    watch, meta = Watch(names), Meta()
    status = {"connected": False, "last_event": 0.0, "events": 0}

    def on_event(ev):
        status["last_event"], status["events"] = time.time(), status["events"] + 1
        eng.on_event(ev)
        watch.on_event(ev)

    def on_gap(a, b):
        eng.on_gap(a, b)

    def log(m):
        status["connected"] = "connected" in m
        print(m, flush=True)

    async def timers():
        last = time.time()
        while not stop.is_set():
            eng.tick()
            watch.prune(set(eng.positions))
            meta.refresh_old(list(watch.mints)[:60])
            if time.time() - last >= 600:
                last = time.time()
                print(time.strftime("[kol] %Y-%m-%d %H:%M:%S\n") + report(eng, names))
            await asyncio.sleep(1)

    def health():
        return {"ok": True, "paper": True, "connected": status["connected"],
                "last_event_age_s": round(time.time() - status["last_event"]) if status["last_event"] else None,
                "closed": len(eng.closed), "open": len(eng.positions)}

    async def keep_alive():
        """Render free plan sleeps after 15 min without inbound traffic: ping our own public URL."""
        url = os.environ.get("RENDER_EXTERNAL_URL")
        while url and not stop.is_set():
            await asyncio.sleep(600)
            try:
                await asyncio.to_thread(lambda: urllib.request.urlopen(url + "/healthz", timeout=20).read())
            except Exception as e:
                print(f"[kol] keep-alive {type(e).__name__}", flush=True)

    code = os.environ.get("APP_ACCESS_CODE") or None
    if a.host not in ("127.0.0.1", "localhost") and not code:
        print("[kol] WARNING: public host without APP_ACCESS_CODE: anyone with the URL can read the dashboard")

    async def run():
        await asyncio.gather(listen(on_event, on_gap, stop, log=log), timers(), meta.run(stop), keep_alive(),
                             serve(lambda: build_state(eng, watch, meta, names, status), host=a.host, port=a.port,
                                   access_code=code, health=health))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        eng.store.save(eng)
        print("[kol] stopped\n" + report(eng, names))


if __name__ == "__main__":
    main()
