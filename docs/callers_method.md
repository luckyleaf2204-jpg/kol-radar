# Smart Caller Hunter — method (callers-v1, 2026-11-17)

Research / PAPER only. No wallet, nothing traded, nothing posted. Scores rank setups; they are **not** probabilities and
not financial advice. Code: `callers/`; page: `/callers/` on kol-radar (same access code); data: `callers.db` on the
persistent disk.

## Data sources
* **X (Twitter)** — only the official X API v2 with an app bearer token from the user's own X developer account
  (`X_BEARER_TOKEN`, set by the user on Render). Never the user's password or cookies, no scraping, no posting.
  Hard monthly budget `X_MONTHLY_BUDGET_USD` (default **0 = X collection OFF**). Before every request the worst case
  (max results × price) must fit in the budget left; the ledger uses `X_COST_PER_POST` (0.005) / `X_COST_PER_USER`
  (0.010) and is an ESTIMATE — X's bill is authoritative. Recent search only covers ~7 days; history comes from each
  caller's own timeline (up to `X_BACKFILL_POSTS`, default 200, per new caller).
* **Manual import** (works without X): one call per line, `x post URL, mint or $TICKER` (the post time is decoded
  from the post id: exact to the millisecond) or `username, ISO time, mint`.
* **Telegram channel seed**: the "SOL Pump Insiders" export already in the repo is imported once as caller
  `tg:SOL Pump Insiders` (platform telegram), so the scoring has real data from day one.
* **Market** (no key): DexScreener (snapshot, ticker search), GeckoTerminal (1-minute and 15-minute candles of the
  bonding-curve and main AMM pools; paced to ≤ 24 calls/min), Solana public RPC (mint / freeze authority, top-10
  holder share — pools included, coarse).

## What a call is
A post containing a Solana mint (bare or in a pump.fun / dexscreener / birdeye / gmgn / photon / solscan link;
accepted only if it base58-decodes to 32 bytes), or a `$TICKER` with buy wording that resolves to exactly one
Solana pair created before the post and ≥ 3× more liquid than any other (otherwise UNRESOLVED, not counted). One
call per (post, mint). A post seen within 10 minutes of publication is LIVE and gets a Signal Score.

## Outcome of a call (tradable, not the chart peak)
* Entry = close of the first 1-minute candle ending at or after post time + **60 s** (a fast follower).
* Costs: venue fee (pump.fun 1.25 %, AMMs 0.25–0.3 %, unknown 1 %) + 1 % extra slippage + constant-product impact
  of a **$500** order on half the pool liquidity, on both sides. Pool liquidity at a price P is estimated as
  L_ref × sqrt(P / P_ref) (constant-product pools) from the snapshot at detection (live: `AT_CALL`) or today's
  (old imported calls: `CURRENT_ESTIMATE`), so a coin that grew 50× is not treated as 7× deeper at the call.
* Horizons 1 h / 6 h / 24 h / 7 d from entry: theoretical gain (max candle high), **tradable gain** (best net sale
  at a candle close), return if held to the end of the horizon, time to the best sale.
* Class on the best tradable gain within 7 d: < 50 % MISS, 50–99 % NEAR, 100–199 % WIN_2X, 200–499 % WIN_3_5X,
  500–999 % WIN_6_10X, ≥ 1 000 % MEGA_WIN. Final once 7 d have passed.
* Rug: −90 % from entry inside 24 h without getting back above −50 %. Run-up: price change in the 1 h / 24 h before
  the post (≥ +100 % in 1 h = late call).
* No traded candle within 10 minutes after the post → NO_PRICE (unverified, never counted).

## Caller score (0–100) and tiers
Weights: win rate 30 (95 % Wilson lower bound of the 2x rate, full at 50 %), early calls 15 (share with ≤ +30 % run-up),
average return 15 (median tradable gain, log scale, full at 10x), consistency 15 (share of active weeks with a 2x),
rug 15 (1 − rug rate), risk-adjusted 10 (mean / sd of the 24 h held return). Follower counts are never used.
Tiers: NEW (< 3 verified calls), CANDIDATE (< 10, or not meeting a higher tier), QUALIFIED (≥ 10, score ≥ 55, ≥ 1 win),
ELITE (≥ 10, score ≥ 75, ≥ 3 wins), REJECTED (≥ 10 and score < 35, or > 50 % rugs, or > 60 % late calls). No official
rank below 10 verified calls.

## Signal Score (0–100) for a live call
Caller 35 (score × tier factor), consensus 20 (distinct QUALIFIED / ELITE callers on the coin in 6 h), timing 10
(1 h run-up), volume 10 (1 h volume / liquidity), liquidity 10 (< $10k → 0), volatility 5, smart money 5 (Insiders group
wallets traded the coin), safety 5 (no mint / freeze authority, top-10 ≤ 50 %). Caps at 49: caller not QUALIFIED /
ELITE, mint or freeze authority still set, liquidity < $10k. Labels: < 50 ignore, 50–64 Watchlist, 65–79 Strong,
≥ 80 High Priority. Telegram push only for High Priority and only with `CALLERS_TELEGRAM=1`.

## Known limits
Peak and impact use candles and a constant-product approximation (no MEV, no failed transactions); liquidity of old
calls is today's pool scaled by sqrt(price) (LP adds / removals not seen); top-10 share includes pools; X search sees ~7 days; GeckoTerminal may not have minute candles for
old or tiny pools (→ NO_PRICE); a caller that deletes losing posts is only caught if the post was stored before.
