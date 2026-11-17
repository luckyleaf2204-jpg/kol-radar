"""Pre-registered rules of the Smart Caller Hunter (docs/callers_method.md). Changing a value = a new method version;
the version is stored with every evaluation so old and new numbers are never mixed silently."""
import os

METHOD_VERSION = "callers-v1"

# --- what a "call" is -------------------------------------------------------------------------------------------------
DETECT_LATENCY_S = 60          # entry = first candle close at or after post time + this (a fast follower, not the wick)
TRADE_USD = 500.0              # paper size used for impact / fee haircuts
FEE_BY_DEX = {"pump-fun": 0.0125, "pumpswap": 0.0025, "raydium": 0.0025, "raydium-clmm": 0.0025,
              "meteora": 0.0025, "orca": 0.003}
DEFAULT_FEE = 0.01             # unknown venue: conservative
EXTRA_SLIPPAGE = 0.01          # per side, on top of the constant-product impact estimate
HORIZONS_S = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
WIN_HORIZON = "7d"             # classification uses the best TRADABLE gain inside this horizon

# --- outcome classes (spec table), on the tradable gain ---------------------------------------------------------------
CLASSES = ((10.0, "MEGA_WIN"), (5.0, "WIN_6_10X"), (2.0, "WIN_3_5X"), (1.0, "WIN_2X"), (0.5, "NEAR"),
           (float("-inf"), "MISS"))

# --- rug / late-call flags ----------------------------------------------------------------------------------------------
RUG_DROP = 0.90                # price -90 % from entry inside 24 h and never back above -50 %
LATE_RUNUP_1H = 1.0            # +100 % in the hour before the post = a late call
EARLY_RUNUP_1H = 0.30          # <= +30 % in the hour before = early

# --- caller tiers ------------------------------------------------------------------------------------------------------
MIN_RANKED_CALLS = 10          # no official rank below this many verified calls
TIER_RULES = {"ELITE": {"min_calls": 10, "min_score": 75, "min_wins": 3},
              "QUALIFIED": {"min_calls": 10, "min_score": 55, "min_wins": 1}}
REJECT_SCORE = 35              # >= MIN_RANKED_CALLS calls and score below this
REJECT_RUG_RATE = 0.5          # or more than half of its calls rugged
REJECT_LATE_RATE = 0.6         # or most of its calls came after the coin had already doubled (pump-chaser / exit liq.)
SCORE_WEIGHTS = {"win_rate": 30, "early": 15, "avg_return": 15, "consistency": 15, "rug": 15, "risk_adjusted": 10}

# --- coin Signal Score -------------------------------------------------------------------------------------------------
SIGNAL_WEIGHTS = {"caller": 35, "consensus": 20, "timing": 10, "volume": 10, "liquidity": 10, "volatility": 5,
                  "smart_money": 5, "safety": 5}
CONSENSUS_WINDOW_S = 6 * 3600
SIGNAL_LABELS = ((80, "HIGH_PRIORITY"), (65, "STRONG"), (50, "WATCHLIST"), (0, "IGNORE"))
MIN_LIQUIDITY_USD = 10_000

# --- X API (official v2, bearer token; the bill is X's — this ledger is an ESTIMATE) -----------------------------------
X_BEARER_TOKEN_ENV = "X_BEARER_TOKEN"
X_MONTHLY_BUDGET_USD = float(os.environ.get("X_MONTHLY_BUDGET_USD") or 0)     # 0 = X collection OFF
X_COST_PER_POST = float(os.environ.get("X_COST_PER_POST") or 0.005)
X_COST_PER_USER = float(os.environ.get("X_COST_PER_USER") or 0.010)
X_POLL_EVERY_S = int(os.environ.get("X_POLL_EVERY_S") or 300)
X_DISCOVERY_QUERIES = (
    '(pump.fun OR dexscreener.com/solana OR "CA:" OR birdeye.so OR gmgn.ai) -is:retweet',
)
X_MAX_RESULTS = 100
X_BACKFILL_POSTS = int(os.environ.get("X_BACKFILL_POSTS") or 200)   # history per new candidate (budget permitting)
DISCOVERY_MIN_CALLS = 2        # an author with >= 2 different mints in discovery results becomes a NEW caller
