# DEX Arbitrage Paper Lab — handover (2026-10-07). NOT deployed. No real transaction.

## 1. Repo / branch / commits
Repo `luckyleaf2204-jpg/kol-radar`. Production = `origin/main` = `938c4e4` (pushed 07:48 UTC; Render autoDeploy from
main; `/healthz` 07:57 UTC: connected, `/var/data/paper.db`, durable). The deployed commit is not exposed by the
site, so "938c4e4 is live" is inferred from the push + healthy redeploy, not read from the server. Work branch
`dexarb-lab` from `938c4e4`; result commit: see `git log dexarb-lab` (not pushed).

## 2. KOL data / code
Code removed from the app on `dexarb-lab`: `kolbot/` (KOL copy engine, KOL roster, signals, Smart Wallet, devs,
S1–S3 books, UI), `kols.json`, `run.bat`, `tools/demo_db.py`, old tests. History stays in git; old
pre-registrations moved to `docs/history/`.
Production KOL **data was NOT deleted**: production is only reachable by deploying (forbidden here) or a Render
shell. `tools/kol_cleanup.py` (dry-run default; apply needs `--backup-confirmed` + the dry-run fingerprint) removes:
tables kols, kol_roster, kol_daily, trades, gaps; row state 'engine'; signals with source='kol'; files
signal_paper_kol5.db / kol10.db. Dry-run on the LOCAL copy of paper.db (not production): kols 565 rows (56 kB data),
kol_roster 565 (25 kB), kol_daily 29 (4 kB), trades 64 (20 kB), gaps 2, state 1 row, signals(kol) 0, two files of
53 kB each; file 78.8 MB. Not deletable safely (kept, reported): Smart Wallet rows of KOL wallets (local: sw_trades
565, sw_open 70, sw_wallets 94).

## 3. New database
`dexarb.db` next to `KOL_DB` (production: `/var/data/dexarb.db`), schema_version 1, WAL; refuses paper.db / KOL
files. Not durable host → paper sample disabled. Quota 1,000 MB. Retention: raw quotes 7 d, raw rejected rows 30 d
(dry-run unless DEXARB_RETENTION=apply), rollups / ledger / audit kept. Backup (sqlite backup API + integrity) and
restore tested.

## 4. Chains / DEX (live read-only check 2026-10-07, docs/dexarb_connector_verification.json)
SUPPORTED: Base Uniswap V2, Uniswap V3, PancakeSwap V3; Polygon QuickSwap V2, SushiSwap V2, Uniswap V3; Ethereum
Uniswap V2, Uniswap V3; BNB PancakeSwap V2, BiSwap V2, PancakeSwap V3, Uniswap V3; Solana Orca Whirlpool, Meteora
DLMM. UNVERIFIED: Base SushiSwap V2 (no cbBTC pool), Ethereum SushiSwap V2 (no WBTC pool), Solana Raydium and Raydium
CLMM (no JUP route at the size). Atomic model B: NOT_SUPPORTED everywhere. Wallets: Trust Wallet all five; Phantom
Solana / Ethereum / Base / Polygon, BNB UNVERIFIED. Sources: public RPC (publicnode; Solana mainnet-beta) + Jupiter
lite quote API; no key, no cost. BNB public RPC returned HTTP 429 during runs (feed UNAVAILABLE / gas UNKNOWN);
Polygon batches ~3–6 s (quote_stale rejections).

## 5. Measurement levels
MEASURED: gas price (eth_feeHistory), Solana priority fees, rent, native price (DEX quote). SIMULATED: EVM approval
gas (eth_estimateGas). ESTIMATED: EVM swap gas (median of recent same-pool txs, protocol fallback), Solana base fee
and 1.4 M CU upper bound. Quotes: exact size, block / slot context, latency. Swap simulation itself: not done
(needs a funded wallet; none used) → swap execution is quote-based paper fill.

## 6. Paper model
A (sequential wallet swaps) with arms A_fast (2 s / 4 s) and A_slow (5 s / 15 s); B atomic NOT_SUPPORTED.

## 7. Tests
`python -m pytest -q tests/` → 47 passed. No signing / sending: RPC allow-list (single + batch), quote client only
reads /quote, source scan, no POST route.

## 8. Sample (local smoke runs, public endpoints, 2026-10-07 08:44–09:11 UTC)
Run 2 (16 min, after fixes): 4,810 evaluations, 0 candidates, best gross spread per chain all ≤ 0 (Base −0.002 /
−0.04, Ethereum −0.02 / −0.30, Polygon −0.74 / −70.1, Solana −0.28 at 1,000; BNB only failed quotes / ≤ 0).
Rejections: negative_spread 3,622, impact_high 651, cost_unknown 234, quote_failed 168, quote_stale 135. Benchmark
round trips (random pair, paper): Solana SOL 1,000: −0.71 (fast) / −1.00 (slow) USDC; Ethereum WETH 100: −1.71
(gas 1.16); Ethereum WBTC 100: −1.26; BNB ETH 1,000: −3.23 USDT; Base cbBTC 1,000: −5.20; Base WETH 100: −0.53;
Polygon WPOL 100: −12.5; Polygon WETH 100 via SushiSwap V2: −99.97 (illiquid) → baseline now applies the same
execution limits (set during the smoke test, before any sample).
These are smoke-test numbers, not the pre-registered sample (which starts at deployment, after a 24 h warm-up).

## 9. Storage
Run 2: 0.213 MB of data in 16 min ≈ 19 MB/day (vacuumed copy minus empty schema; no candidates in that window).

## 10. FAIL / UNVERIFIED
UNVERIFIED: production KOL deletion (not run), Phantom on BNB, 5 connectors (above), swap simulation, sustained
public-RPC coverage (BNB 429, Polygon slow), the deployed commit id. FAIL: none of the acceptance tests.

## 11. Site
Service `kol-radar`, hostname, plan, disk `/var/data`, `KOL_DB`, `APP_ACCESS_CODE` (secret stays in Render), health
path unchanged; page title "KOL Radar"; the stored access code key is reused.

## 12. Not deployed; no real transaction; no signing.
