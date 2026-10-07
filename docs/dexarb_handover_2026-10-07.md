# DEX Arbitrage Paper Lab — handover (updated 2026-10-07 ~10:00 UTC). NOT deployed. No real transaction.

## 1. Production KOL data — NOT cleaned (blocked, by design)
* **No Render Shell access** from this machine: no Render CLI, no Render config / API key, and the dashboard
  (dashboard.render.com) shows the sign-in page in the in-app browser. No credential was asked for or entered.
  Per the instruction, the production cleanup stops here: nothing was deleted, nothing deployed.
* **KOL writers still active in production** (commit `938c4e4`, read from the code): the KOL copy engine saves
  `state` ('engine') and `trades` / `gaps` (engine stopped taking new copies, but its 3 open positions still close
  and gaps are still written); `KolHistory` writes `kols` / `kol_daily` on every `hist.tick()`; `SmartTracker` deletes
  and re-inserts `kol_roster` at every start; `SignalEngine` writes `signals`. Stopping them without stopping the site
  means replacing the app (deploying `dexarb-lab`, which has none of these writers) — not allowed in this task.
  Suspending the Render service would take the whole site down.
* **Not verified** (needs the Render account): the production DB path beyond `/healthz` (`/var/data/paper.db`,
  durable true), the latest disk snapshot / backup and a restore of it.
* **Procedure once access and a deploy are approved:** deploy → `/healthz` shows `app: dexarb` → Render Shell:
  `python tools/kol_cleanup.py --db /var/data/paper.db --report /var/data/kol_cleanup_dryrun.json` → confirm a disk
  snapshot exists → apply with `--backup-confirmed "<snapshot id/date>" --expect-fingerprint <from the dry run>` →
  re-run the dry run (KOL tables absent, `signals WHERE source='kol'` = 0, files gone) → check `/healthz`.
* **Smart Wallet rows of KOL wallets (sw_trades / sw_open / sw_wallets)**: written by the Smart Wallet tracker for
  every wallet with the same code path, i.e. they ARE Smart Wallet data, not rows owned by the KOL bot → kept and
  reported, never deleted by the tool (local copy: 565 / 70 / 94 rows).
* Local dry run (data/paper.db, NOT production): kols 565 rows, kol_roster 565, kol_daily 29, trades 64, gaps 2,
  state 1, signals(kol) 0, two kol book files of 53 kB each.

## 2. Fresh `dexarb.db` (verified locally on the live smoke-run DB)
schema_version 1, WAL, 23 tables, none of the old tables (kols, kol_roster, trades, sw_*, signals, devs, tokens,
gaps, state). Backup (sqlite online backup) integrity ok; restore to a new file integrity ok, schema 1, row counts
equal at backup time. Retention dry run: 26 quote rows / 5 rejected rows eligible at +40 d, 0 deleted. Quota
1,000 MB, 0.9 MB used. Production path will be `/var/data/dexarb.db` (next to KOL_DB); not created yet (no deploy).
The app never opens paper.db (Store refuses the old names). Site name "KOL Radar", service `kol-radar`, hostname,
disk, `KOL_DB`, `APP_ACCESS_CODE` (value stays in Render) unchanged.

## 3. Cost / fill provenance
| Item | Level | Source |
|---|---|---|
| EVM gas price | MEASURED | eth_feeHistory (next base fee + p50 tip) |
| EVM swap gas, paper leg, 11 venues | SIMULATED | eth_estimateGas of the exact unsigned swap (state override) |
| EVM swap gas, detection; other venues | ESTIMATED | median gasUsed of recent same-pool txs (protocol fallback) |
| Base L1 data fee | ESTIMATED | median l1Fee of the same-pool sample |
| EVM approval | SIMULATED | eth_estimateGas approve(router) from the paper address |
| Paper fill, 11 EVM venues | SIMULATED | eth_call of the exact unsigned swap with min-out (revert = REVERTED, gas paid) |
| Paper fill, Solana / PancakeSwap V3 Base / no token layout | QUOTE_ONLY | provider / on-chain quote, labelled so |
| Solana base fee | ESTIMATED | 5,000 lamports / signature |
| Solana priority | MEASURED price × ESTIMATED 1.4 M CU upper bound | getRecentPrioritizationFees |
| Solana token-account rent | MEASURED | getMinimumBalanceForRentExemption(165) |
| Native → quote asset | MEASURED | DEX quote of 1 wrapped native, timestamped |
| Anything missing | UNKNOWN | opportunity rejected (cost_unknown) |
Simulation check (`tools/dexarb_verify.py --sim`, live, read-only): simulated output equal to the quote (max
deviation 3.4e-5) on Base Uniswap V2/V3, Ethereum Uniswap V2/V3, Polygon QuickSwap/Sushi V2/Uniswap V3, BNB
PancakeSwap V2/V3, BiSwap, Uniswap V3; PancakeSwap V3 on Base reverted (router / ABI) → QUOTE_ONLY. Solana: no
funded account and no balance override in simulateTransaction → QUOTE_ONLY. Nothing is signed or broadcast.

## 4. Sequential model, baseline, connectors
Two legs, leg 2 re-quoted (and re-simulated) after the latency, gas charged on reverts, position OPEN_EXPOSURE
until a real fill; pending legs survive a restart (verified live: 3 cycles stuck by a crash were re-decided with
fresh quotes and closed after restart). Baseline uses the candidate execution limits. Amendment 1 (timestamped) in
docs/prereg_dex_arbitrage.md records the smoke-test changes before any sample. UNVERIFIED connectors (never used):
Sushi V2 Base, Sushi V2 Ethereum, Raydium, Raydium CLMM.

## 5. Bugs found and fixed in this round
18-decimal simulated amounts overflowed SQLite INTEGER and crashed the server; one failing paper event stopped the
timer loop (now retried and logged; timer loop cannot die); retention NOT IN with NULLs deleted nothing; pending
cycles did not reserve capital (overdraft); rollups showed gross 0 for failed quotes; baseline ignored the impact cap.

## 6. RPC / coverage (public endpoints, smoke runs 08:44–09:46 UTC)
Base, Ethereum, Polygon, Solana feeds OK; BNB had HTTP 429 runs earlier (later run OK); Polygon batches 3–6 s
(quote_stale). Sustained 24 h behaviour on public endpoints: UNVERIFIED.

## 7. Storage
≈ 19 MB/day measured (16-min run, vacuumed copy minus empty schema, no candidates). Paper legs / simulations add a
few kB per cycle.

## 8. Smoke-run results (not the pre-registered sample)
0 candidates; every gross spread ≤ 0. Benchmark round trips (paper, net): Solana SOL 1,000 −0.97 / −1.33; Ethereum
WETH 100 −0.85 (simulated gas 0.19), WBTC 100 −0.52; Base cbBTC 100 −0.08, cbBTC 1,000 −10.30 (held through a crash
→ exposure); BNB ETH 100 −0.22, WBNB 100 −0.52.

## 9. Tests
`python -m pytest -q tests/` → 53 passed.

## 10. Still open
Render access for the production cleanup; deploy decision (replaces the KOL app); Phantom on BNB; Solana simulation;
24 h public-RPC coverage.
