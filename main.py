"""KOL Radar site — now the Insiders page of the "SOL Pump Insiders" call channel (research only, read-only chain
data; PAPER only: no wallet keys, nothing is signed or sent). The previous DEX Arbitrage lab is kept in
dexarb_main.py / dexarb/ and is not started.

usage:
  python main.py                      page + 2-minute deployer watch   http://127.0.0.1:8780
  python main.py --no-watch           page only
Env: APP_ACCESS_CODE (page code, managed in Render), KOL_DB (its directory = the persistent disk for the watch and
paper state), HELIUS_API_KEY (optional: server rescan), TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (optional: STRONG
alerts), INSIDER_ALERT_LEVELS, INSIDER_PAPER_USD, INSIDER_WATCH_EVERY_S, INSIDER_WATCH=0 / INSIDER_PAPER=0."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from insiders.site import run_forever  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8780)))
    ap.add_argument("--no-watch", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    run_forever(host=a.host, port=a.port, watch=not a.no_watch)


if __name__ == "__main__":
    main()
