# KOL Radar — DEX Arbitrage Paper Lab (PAPER ONLY)

The site keeps its name, hostname and Render service (`kol-radar`), but the application is now a **DEX-to-DEX
arbitrage paper lab**. The old KOL copy bot / sniper / signal books are removed from the app (their code stays in git
history before branch `dexarb-lab`; their pre-registrations are in `docs/history/`).

**No wallet, no seed phrase, no private key, no signing, no sending.** Every RPC call goes through an allow-list of
read methods; the quote client only calls quote endpoints. A paper NET ESTIMATE is not a real profit, and arbitrage
is never risk-free.

```
python main.py                        # scanner + paper ledger + dashboard http://127.0.0.1:8780
python main.py --no-scan              # dashboard only
python main.py --chains base,solana   # some chains only
python tools/dexarb_verify.py         # live read-only connector check -> docs/dexarb_connector_verification.json
python tools/kol_cleanup.py --db /var/data/paper.db      # DRY-RUN of the KOL data removal (see the script)
python -m pytest -q tests/
```

* Method: `docs/prereg_dex_arbitrage.md` (pre-registered, version dexarb-v1).
* Database: `dexarb.db` next to `KOL_DB` on the persistent disk (or `DEXARB_DB`); schema versioned; the old
  `paper.db` is never opened by the app.
* Env: `APP_ACCESS_CODE` (dashboard), `DEXARB_RPC_<CHAIN>` (read-only RPC URLs, optional), `DEXARB_DB_MAX_MB`,
  `DEXARB_RETENTION=apply` (otherwise retention is a dry run), `DEXARB_CHAINS`.
* Pages: Tổng quan, Cơ hội (with full cost breakdown), Sổ paper, DEX / chain health, Nghiên cứu, Lưu trữ.
