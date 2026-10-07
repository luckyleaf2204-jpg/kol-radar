# DEX Arbitrage Paper Lab — PRE-REGISTERED (dexarb-v1, 2026-10-07, before any paper result exists)

Paper only: no seed phrase, no private key, no signing, no sending / broadcasting, no live order. Not deployed by
this task. Arbitrage is never risk-free: prices move between legs, quotes and gas change, and real transactions can
revert or land late. A paper NET ESTIMATE is never a real profit. Changing anything below = a new version; dexarb-v1
stays as the benchmark. `tests/test_dexarb_prereg.py` checks this document against the code.

## 1. Universe

Same-chain DEX-to-DEX only (no bridge, no cross-chain). Cycle = quote asset → token on DEX A → quote asset on DEX B.

| Chain (id) | Quote asset | Tokens | Venues registered |
|---|---|---|---|
| Base (8453) | USDC | WETH, cbBTC | Uniswap V2, SushiSwap V2, Uniswap V3, PancakeSwap V3 |
| Polygon (137) | USDC | WETH, WPOL | QuickSwap V2, SushiSwap V2, Uniswap V3 |
| Ethereum (1) | USDC | WETH, WBTC | Uniswap V2, SushiSwap V2, Uniswap V3 |
| BNB Chain (56) | USDT | WBNB, ETH | PancakeSwap V2, BiSwap V2, PancakeSwap V3, Uniswap V3 |
| Solana (mainnet-beta) | USDC | SOL, JUP | Raydium, Raydium CLMM, Orca Whirlpool, Meteora DLMM |

A venue is used only when SUPPORTED: fixture tests + a recorded live read-only check that quoted every registered
token at every size in both directions with a known route (`tools/dexarb_verify.py`). Wallets (checked 2026-10-07):
Trust Wallet lists swaps on all five chains; Phantom on Solana, Ethereum, Base, Polygon; Phantom on BNB Chain is
UNVERIFIED (announced 2026-10-01, help-center list not updated).

## 2. Quotes

Exact token in / out and amount at the paper size (never a small-size price scaled). EVM: router getAmountsOut
(V2) or QuoterV2 quoteExactInputSingle over every fee tier, best tier kept (V3), via eth_call in one JSON-RPC
batch with the head block as context. Solana: Jupiter quote restricted to ONE DEX with direct routes only, accepted
only if every route hop carries that DEX label (an aggregator route is never labelled as a DEX quote); contextSlot
kept; platform fee must be absent. Pool fees are inside the quoted output. Impact: EVM 1 − effective / 1/1000-size
price (same batch); Solana provider priceImpactPct.

## 3. Costs (each with MEASURED / SIMULATED / ESTIMATED / UNKNOWN and its source)

* EVM gas price MEASURED (eth_feeHistory: next base fee + p50 tip). Swap gas ESTIMATED = median gasUsed (+ median L1
  fee on OP-stack) of up to 8 recent transactions that swapped in the same pool (last 400 blocks); a pool
  with no swap in that window uses the median of the other sampled pools of the same protocol on that chain (no
  sample at all = UNKNOWN). A failed sample is retried after 2 min. Approval
  SIMULATED (eth_estimateGas approve from the paper address), charged once per (router, input token) per paper
  account.
* Solana: base fee 5,000 lamports / signature ESTIMATED (protocol default); priority = median of
  getRecentPrioritizationFees (MEASURED) × 1,400,000 CU (ESTIMATED upper bound: simulation needs a funded wallet,
  none is used). Token-account rent MEASURED (getMinimumBalanceForRentExemption(165)), charged once per non-quote,
  non-SOL token per paper account (refundable on close; not credited back).
* Native → quote asset with a native price QUOTED on a SUPPORTED DEX (MEASURED, timestamped, max age 30 s in the
  scanner). Stablecoins may depeg: all results are in the chain's quote asset, no USD conversion.
* UNKNOWN cost → the opportunity is rejected (cost_unknown); never counted as executable.

## 4. Detection rule

net_profit = amount_out − amount_in − gas_and_priority (two transactions) − setup (approvals / token accounts) −
other known costs (0); swap fees are inside amount_out. uncertainty_buffer = 10 bps of size + 50 % of
(gas + setup). Candidate only if net_profit_after_buffer > 5 bps of size. Sizes 100 and 1,000 quote units (Solana
1,000 only: public quote rate limit). Rejections in order: quote_failed / no_route, chain_mismatch, token_mismatch,
quote_stale (both quotes fetched within 5 s of the decision and of each other), context_mismatch
(sell quote read more than 1 block / slot behind the buy quote: node lag or reorg), impact_high (> 1 % on a leg),
cost_unknown, negative_spread, below_threshold. All rejected evaluations are counted (opportunity_rollups); those
with gross spread > 0 and every candidate are stored raw; of all other rejections 1 % is stored raw
(deterministic hash sample).

## 5. Paper execution

* **Model A — sequential wallet swaps (default, two signatures).** Arms with fixed latencies: `A_fast` (detection
  → leg 1: 2 s; leg 1 → leg 2: 4 s) and `A_slow` (5 s; 15 s). At detection + L1: fresh buy AND sell quotes; if the
  fresh net after buffer ≤ threshold → ABORTED (no cost; quote decay recorded). Leg 1 with min-out = detection
  quote × (1 − 0.5 %); fresh quote below it → REVERTED (gas paid, cycle FAILED). Leg 2 decided at leg-1 fill with a
  new quote (min-out from it), executed at + L2 with a NEW quote for the exact token balance; below min-out →
  REVERTED (gas paid), retried every 30 s; meanwhile OPEN_EXPOSURE. After 1 h any SUPPORTED venue may be used.
  Never closed by assumption. Capital 10,000 quote units per chain per arm + a native gas float worth 50 quote units;
  max 1 open cycle per token and 3 per chain per arm.
* **Model B — atomic / composed:** NOT_SUPPORTED for every connector (no executor / bundle simulation; this
  paper-only task builds none). Never substituted by adding two quotes.
* **Benchmark (non-trading signal):** once per hour per chain per arm, a random (token, ordered venue pair, size)
  cycle executed by the same model regardless of any signal, under the same execution limits (quotes OK, fresh,
  impact ≤ 1 % per leg; otherwise that hour's baseline is skipped and logged). Hold-the-quote-asset = 0 is the
  trivial benchmark. (Set on 2026-10-07 during the smoke test, before the sample: an unrestricted random pair had
  picked an illiquid pool and lost 99.97 % - an unfair, too-easy benchmark.)

## 6. Periods and conclusions

Warm-up: the first 24 h after deployment are excluded from conclusions. P0 = days 1–7, P1 = days 8–14, forward
after that; nothing is re-tuned between them. Per (arm, chain): NO_EVIDENCE_YET if < 30 closed signal cycles or
< 7 days; NOT_EXECUTABLE if < 20 % of candidates fill leg 1; BENCHMARK_UNAVAILABLE without closed baseline
cycles; PAPER_LOSS if the 95 % CI (bootstrap by day, 2,000) of the mean net per closed cycle is < 0;
PAPER_PROFIT_ESTIMATE only if its lower bound > 0, no cycle has an UNKNOWN cost and the mean exceeds the baseline
mean; otherwise NO_EVIDENCE_YET. Number of tests = arms × chains × (venue pairs × tokens × sizes) cells, reported.

## 7. Data

New `dexarb.db` next to the existing persistent database (Render: /var/data), schema versioned, WAL. Never reads or
writes paper.db / KOL / Smart Wallet / S1–S3 data. Raw quote snapshots 7 days; rejected raw rows 30 days; rollups,
ledger, cycles, legs and audit events kept. Quota DEXARB_DB_MAX_MB (1,000). If the host has no persistent disk the
paper sample does not start.
