"""Pre-registered experiment constants (docs/prereg_dex_arbitrage.md, version dexarb-v1). Changing any value is a new
experiment version; v1 stays as the benchmark. tests/test_dexarb_prereg.py checks the document states the same."""
VERSION = "dexarb-v1"
SIZES = (100.0, 1000.0)                  # quote-asset units per cycle (USDC; USDT on BNB)
SCAN_SIZES_SOLANA = (1000.0,)            # Jupiter public quote rate limit: one size on Solana
BUFFER_BPS = 10                          # uncertainty buffer = 10 bps of size ...
BUFFER_COST_SHARE = 0.5                  # ... + 50 % of the estimated network + setup costs
MIN_NET_BPS = 5                          # candidate only if net after buffer > 5 bps of size
MAX_IMPACT = 0.01                        # per leg, from the quote
MAX_QUOTE_AGE_S = 5.0                    # both legs' quotes fetched within this before the decision
SLIPPAGE_TOL_BPS = 50                    # min-out = decision quote x (1 - 0.5 %)
ARMS = {"A_fast": (2.0, 4.0), "A_slow": (5.0, 15.0)}   # (detection -> leg 1, leg 1 fill -> leg 2) seconds
CAPITAL = 10_000.0                       # quote-asset units per chain per arm (paper)
NATIVE_FLOAT_QUOTE = 50.0                # paper native gas balance at start, worth this many quote units
MAX_OPEN_PER_TOKEN = 1
MAX_OPEN_PER_CHAIN = 3
RETRY_S = 30.0                           # leg-2 retry interval after a revert / failed quote
EXIT_ANY_VENUE_AFTER_S = 3600.0          # after 1 h leg 2 may use any SUPPORTED venue; never forced closed
BASELINE_EVERY_S = 3600.0                # one random (token, pair, size) cycle per chain per arm per hour
NEG_SAMPLE = 0.01                        # share of negative-spread evaluations stored raw (rest -> rollups)
EVAL_MIN_CYCLES = 30
EVAL_MIN_DAYS = 7
BOOT = 2000
