"""PAPER KOL copy bot for pump.fun (direction C). Listens to every pump.fun bonding-curve trade, copies the buys
of the KOL wallets in kols.json on paper, and sells when the KOL sells. No wallet, no key, no transaction.

usage:
  python main.py                 run + dashboard http://127.0.0.1:8780 (Ctrl+C to stop; state kept in data/paper.db)
  python main.py --report        print the results so far"""
import argparse
import asyncio
import json
import os
import shutil
import urllib.request
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kolbot.config import Config  # noqa: E402
from kolbot.devs import DevTracker  # noqa: E402
from kolbot.kolhist import KolHistory  # noqa: E402
from kolbot.smart import SmartTracker  # noqa: E402
from kolbot.signals import SignalEngine, refuse_auto_trade  # noqa: E402
from kolbot.signal_paper import SignalPaper  # noqa: E402
from kolbot.sniper import S1_CFG, LaunchSniper, SniperSources, pinned_source_of  # noqa: E402
from kolbot.s3 import WINDOWS_MS, PreSniper  # noqa: E402
from kolbot.engine import Engine  # noqa: E402
from kolbot.meta import Meta  # noqa: E402
from kolbot.report import by_kol, summarize  # noqa: E402
from kolbot.store import Store  # noqa: E402
from kolbot.stream import listen  # noqa: E402
from kolbot.watch import Watch  # noqa: E402
from kolbot.web import build_state, make_routes, serve  # noqa: E402


def storage_info(db_path: str) -> dict:
    """Where the database lives and how full that disk is (the dashboard warns from 80 %)."""
    p = Path(db_path).resolve()
    try:
        du = shutil.disk_usage(p.parent)
        used_pct = round(100 * du.used / du.total, 1)
        free_gb = round(du.free / 1e9, 2)
    except OSError:
        used_pct = free_gb = None
    size = sum(f.stat().st_size for f in p.parent.glob(p.name + "*") if f.is_file())
    on_render = bool(os.environ.get("RENDER"))
    durable = (not on_render) or Path(db_path).as_posix().startswith("/var/data/")
    return {"db_path": str(p), "db_mb": round(size / 1e6, 1), "disk_used_pct": used_pct, "disk_free_gb": free_gb,
            "durable": durable, "warn": bool(used_pct is not None and used_pct >= 80) or not durable}


def load_roster(path: Path) -> dict[str, dict]:
    return {r["wallet"]: {"name": r.get("name") or r["wallet"][:8], "twitter": r.get("twitter") or ""}
            for r in json.loads(path.read_text(encoding="utf-8"))["wallets"]}


def load_kols(path: Path) -> dict[str, str]:
    return {w: r["name"] for w, r in load_roster(path).items()}


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
    ap.add_argument("--db", default=os.environ.get("KOL_DB") or str(ROOT / "data" / "paper.db"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8780)))
    ap.add_argument("--no-stream", action="store_true", help="dashboard only (tests / demo database)")
    a = ap.parse_args()
    refuse_auto_trade()                            # signals are display-only; automatic trading does not exist
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    cfg = Config.load(Path(a.config))
    roster = load_roster(Path(a.kols))
    names = {w: r["name"] for w, r in roster.items()}
    eng = Engine(cfg, set(names), Store(Path(a.db)))
    if a.report:
        print(report(eng, names))
        return
    print(f"[kol] PAPER bot · {len(names)} KOL wallets · settings {cfg.as_dict()}")
    st_info = storage_info(a.db)
    print(f"[kol] database {st_info['db_path']} ({st_info['db_mb']} MB, disk {st_info['disk_used_pct']}% used) · "
          + ("persistent" if st_info["durable"] else "WARNING: NOT persistent (wiped on every deploy/restart)"))
    stop = asyncio.Event()
    db = eng.store.db
    devs = DevTracker(db)
    hist = KolHistory(db, roster)
    smart = SmartTracker(db, roster)               # research only: never feeds the paper bot
    signals = SignalEngine(db, roster)             # Top 10 buys -> display-only signals
    watch, meta = Watch(names), Meta(on_creator=devs.ingest_api, on_coin=devs.ingest_coin)
    sigpaper = SignalPaper(Path(a.db).with_name("signal_paper.db"), lambda: meta.sol_usd,   # own file, paper only
                           log=lambda m: print(m.replace("[kol] COPY", "[sigpaper] PAPER BUY"), flush=True))
    books = {"10": sigpaper}
    # same rules, other source sets (user requests 2026-10-06): both lists Top 5 / 20; one list only Top 5 / 10
    for key, n, src, label in (("5", 5, None, "Top 5"), ("20", 20, None, "Top 20"),
                               ("smart5", 5, "smart", "Smart Top 5"), ("smart10", 10, "smart", "Smart Top 10"),
                               ("kol5", 5, "kol", "KOL Top 5"), ("kol10", 10, "kol", "KOL Top 10")):
        fname = f"signal_paper_top{n}.db" if src is None else f"signal_paper_{src}{n}.db"
        books[key] = SignalPaper(Path(a.db).with_name(fname), lambda: meta.sol_usd, top_n=n, source=src, label=label,
                                 log=lambda m, k=key: print(m.replace("[kol] COPY", f"[sigpaper {k}] PAPER BUY"),
                                                            flush=True))
    # pre-registered sniper books (docs/prereg_sniper_books.md, 2026-10-07): S1 launch sniper, S2 copy sniper bots
    s1 = SignalPaper(Path(a.db).with_name("signal_paper_s1.db"), lambda: meta.sol_usd, label="S1 · tự snipe token mới",
                     entry="launch", exit="none", cfg_overrides=S1_CFG,
                     log=lambda m: print(m.replace("[kol] COPY", "[sigpaper s1] PAPER BUY"), flush=True))
    s1_launch = LaunchSniper(s1, lambda w: (db.execute("SELECT risk FROM devs WHERE wallet=?", (w,)).fetchone()
                                            or [None])[0])
    # S2 amended 2026-10-07 (user): follow only the #1 sniper wallet; new file, the Top 10 run is kept apart
    s2 = SignalPaper(Path(a.db).with_name("signal_paper_s2_top1.db"), lambda: meta.sol_usd, top_n=1,
                     label="S2 · copy bot sniper #1", entry="topn",
                     log=lambda m: print(m.replace("[kol] COPY", "[sigpaper s2] PAPER BUY"), flush=True))
    # S2b (user, 2026-10-07): S2 with a 1 s delay instead of 3 s (prereg amendment 2)
    s2b = SignalPaper(Path(a.db).with_name("signal_paper_s2b.db"), lambda: meta.sol_usd, top_n=1,
                      label="S2b · copy sniper #1, trễ 1s", entry="topn", cfg_overrides={"delay_s": 1.0},
                      log=lambda m: print(m.replace("[kol] COPY", "[sigpaper s2b] PAPER BUY"), flush=True))
    sniper_src = SniperSources(db)
    # S3 PRE_SNIPER_SIGNAL (prereg section S3, 2026-10-07): one paper book per fixed window, research only
    s3_books = {w: SignalPaper(Path(a.db).with_name(f"signal_paper_s3_{w}ms.db"), lambda: meta.sol_usd,
                               label=f"S3 · cửa sổ {w}ms", entry="pre_sniper", exit="none",
                               cfg_overrides={**S1_CFG, "gap_flag_s": 0},      # any gap invalidates (amendment 4)
                               log=lambda m, w=w: print(m.replace("[kol] COPY", f"[s3 {w}ms] PAPER BUY"), flush=True))
                for w in WINDOWS_MS}
    s3 = PreSniper(Path(a.db).with_name("s3_signals.db"), s3_books,
                   lambda w: (db.execute("SELECT risk FROM devs WHERE wallet=?", (w,)).fetchone() or [None])[0])
    # pre-registered H1 / H2 (docs/prereg_signal_books.md, 2026-10-06): later exit; >= 2 sources within 10 min
    for key, entry, label in (("h1", "signal", "H1 · thoát khi nguồn bán ≥50%"),
                              ("h2", "confluence", "H2 · ≥2 ví nguồn cùng mua")):
        books[key] = SignalPaper(Path(a.db).with_name(f"signal_paper_{key}.db"), lambda: meta.sol_usd, label=label,
                                 entry=entry, exit="half_sold",
                                 log=lambda m, k=key: print(m.replace("[kol] COPY", f"[sigpaper {k}] PAPER BUY"),
                                                            flush=True))
    for b in books.values():                       # paused (user, 2026-10-07): data kept and shown, no new trades
        b.paused = True
    status = {"connected": False, "last_event": 0.0, "events": 0}

    def on_event(ev):
        ev["recv"] = time.time()                   # receive clock (ms): S3 windows are measured on it
        status["last_event"], status["events"] = ev["recv"], status["events"] + 1
        eng.on_event(ev)
        watch.on_event(ev)
        is_kol = ev.get("user") in names
        devs.on_event(ev, is_kol=is_kol)
        smart.on_event(ev)
        sig = signals.on_event(ev)
        src = signals.source_of(ev)
        s1_launch.on_event(ev)                     # 2026-10-07 (user): only S1 / S2 run; the other books are paused
        ssrc = pinned_source_of(ev)                # S2 / S2b pinned to BwWK17cb (amendment 3)
        s2.on_event(ev, None, ssrc)
        s2b.on_event(ev, None, ssrc)
        s3.on_event(ev)
        if is_kol and ev["kind"] == "trade":
            hist.on_kol_event(ev["user"], ev["ts"])

    def on_gap(a, b):
        eng.on_gap(a, b)
        smart.on_gap(a, b)
        s3.on_gap(a, b)                            # S3: invalidate windows / fills / trades overlapping the gap

    def log(m):
        status["connected"] = "connected" in m
        print(m, flush=True)

    async def timers():
        last = time.time()
        while not stop.is_set():
            eng.tick()
            watch.prune(set(eng.positions))
            meta.refresh_old(list(watch.mints)[:60])
            devs.tick()
            hist.tick()
            smart.tick()
            signals.tick()
            sniper_src.refresh()
            s1_launch.tick()
            s1.tick()
            s2.tick()
            s2b.tick()
            s3.tick()
            if time.time() - last >= 600:
                last = time.time()
                print(time.strftime("[kol] %Y-%m-%d %H:%M:%S\n") + report(eng, names))
            await asyncio.sleep(1)

    def health():
        return {"ok": True, "paper": True, "connected": status["connected"],
                "last_event_age_s": round(time.time() - status["last_event"]) if status["last_event"] else None,
                "closed": len(eng.closed), "open": len(eng.positions), "storage": storage_info(a.db)}

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

    routes = make_routes(eng.store.db, roster, get_live=lambda: build_state(eng, watch, meta, names, status),
                         get_status=lambda: dict(health(), events=status["events"], gaps=len(eng.gaps),
                                                 live=not a.no_stream, sol_usd=meta.sol_usd,
                                                 signals_new=db.execute("SELECT COUNT(*) FROM signals WHERE "
                                                                        "state='new'").fetchone()[0],
                                                 kols_tracked=len(names)),
                         symbols=lambda: {m: c.get("symbol") for m, c in meta.coins.items() if c.get("symbol")},
                         want_creator=meta.want_creator, signal_engine=signals, want_coin=meta.want,
                         signal_paper={**books, "s1": s1, "s2": s2, "s2b": s2b,
                                       **{f"s3_{w}": b for w, b in s3_books.items()}},
                         s3_report=s3.report,
                         now_mc=lambda: {m: c.vsol / c.vtok * 1e6 for m, c in list(eng.curves.items())})

    signals.refresh(force=True)
    from kolbot import api as _api
    t0 = time.time()
    _api.summary(db, roster)                       # warm the ranking / CI caches before the first visitor
    print(f"[kol] dashboard caches warm in {time.time() - t0:.1f}s · {len(eng.closed)} trades in the ledger")

    async def run():
        web = serve(routes, host=a.host, port=a.port, access_code=code, health=health)
        if a.no_stream:
            await web
            return
        await asyncio.gather(listen(on_event, on_gap, stop, log=log), timers(), meta.run(stop), keep_alive(), web)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("[kol] stopped\n" + report(eng, names))
    finally:
        devs.tick(force=True)
        hist.tick(force=True)
        smart.flush()
        eng.store.save(eng)
        for b in (*books.values(), s1, s2, s2b, *s3_books.values()):
            if b.eng:
                b.store.save(b.eng)


if __name__ == "__main__":
    main()
