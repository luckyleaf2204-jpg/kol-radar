"""Smart Caller Hunter (research, PAPER / read-only). Finds X accounts that call Solana coins, measures every call on
the price a follower could actually have traded, scores callers and coins, and shows live signals.

Layers: config (pre-registered rules) -> extract (mints / tickers from post text) -> market (DexScreener +
GeckoTerminal, read-only) -> evaluate (tradable entry, horizons, peaks, rug, run-up) -> score (caller 0-100, tiers;
coin Signal Score 0-100) -> xapi (official X API v2, bearer token, hard monthly budget; OFF until configured) ->
discovery / runner (background loops) -> store (callers.db) -> web (pages + read-only API, watchlist writes).
No wallet, no key of the user's X account, no posting, nothing is traded. A score is NOT a probability."""
