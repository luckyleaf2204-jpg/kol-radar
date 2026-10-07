"""DEX Arbitrage Paper Lab (served under the existing "KOL Radar" site name). PAPER / READ-ONLY.

Question (docs/prereg_dex_arbitrage.md): with a given capital, after detection time, real routes, both legs' fees,
gas / priority fees, price impact, slippage, latency, quote failures and liquidity, are there same-chain DEX-to-DEX
round trips with a positive, repeatable PAPER result? Arbitrage is never risk-free: prices move between legs,
quotes / gas change, and real transactions can revert or land late.

Layers: registry (chains, assets, protocols, wallet support) -> rpc / http (read-only clients) -> protocols
(per-DEX quote adapters) -> fees (gas, priority, approval, rent, native price) -> detector (cycles, costs, buffer,
rejections) -> paper (sequential wallet model A; atomic model B = NOT_SUPPORTED) -> store (dexarb.db) -> app / api /
ui. No code path signs, sends or broadcasts a transaction (tests/test_dexarb_paper_only.py)."""
SCHEMA_VERSION = 1
