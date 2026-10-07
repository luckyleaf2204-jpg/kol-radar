# Sniper paper books S1 / S2 — PRE-REGISTERED (2026-10-07, before either book has any data)

Paper only: no key, no wallet, no transaction. The KOL paper bot, its copy rules, the other signal books, Smart
Wallet, Direction B / C of sol_memecoin_hunter are unchanged. Nothing below may be changed after data is seen.

## Why

Every "follow a wallet 3 s later" book was REJECTED (2026-10-07, n 104-195 per book, median about -13 % per trade).
The only wallets seen making money were fast ones (e.g. BwWK17cb: +118 SOL in 27 min, 96 % wins, 13 s average hold).
Public research says much of that profit is creator-funded sniping (manipulation) and that 50-70 % of a sniper's
expected profit goes to Jito tips. Question: with OUR latency (~3 s, public RPC, no slot-0 landing), does
sniping new launches, or copying fast bots, make money after costs?

## Common rules

~$50 per entry; real bonding-curve reserves; pump.fun fees, 1 % extra slippage per side, network + priority fee per
transaction; at most 10 open; one position per token; migrated token -> last curve price; every entry funded
(top-ups recorded), judged per trade.

## S1 — launch sniper

* Universe: every CreateEvent seen on the stream.
* Entry: at the first trade >= 3 s after the creation (or the curve state 3 s + 4 s grace later by wall clock if no
  trade), unless at that moment:
  * the creator holds > 10 % of the supply from its own buys, or
  * >= 3 different non-creator wallets bought in the creation second (bundle / creator-funded sniping), or
  * the creator's stored profile risk is HIGH RISK, REPEAT FAILURE or SUSPICIOUS / RUG HISTORY.
  Skips are counted per reason. When 10 positions are open, new launches are skipped (max_open).
* Exit: take profit +50 %, stop loss -30 % (on the net value a sale would return), 5 min max hold.

## S2 — copy fast bots

* Sources (refreshed every minute): non-KOL wallets with >= 100 resolved own round trips, average hold < 60 s,
  95 % CI of the mean ROI per trade > 0, < 30 % of trades on tokens they created; Top 10 by win rate, then trades,
  then wins.
* Entry: a source buys >= 0.05 SOL -> paper buy 3 s later (30 min (wallet, token) dedup).
* Exit: 3 s after the source's first sell of the token, 24 h max.

## Decision rule (fixed now)

Per book, closed trades not overlapping a stream gap: n < 100 -> no conclusion; n >= 100 -> mean net return per trade
after costs with a 95 % bootstrap CI (clusters: creator for S1, source wallet for S2). Lower bound > 0 -> PROVISIONAL
(never PASS: no control); otherwise REJECT. No real money unless a book passes and stays positive over a fresh
forward period of equal length, then 2-4 weeks of unchanged paper. Building real sniping would also need paid
low-latency infrastructure; that cost is not in these numbers.

## Amendment 1 — 2026-10-07, user decision (before the amended S2 has any data)

S2 now follows ONLY the #1 wallet of the S2 source list (same eligibility, same order; today BwWK17cb…, 2,028
round trips, 96.4 % wins, 15 s average hold). This is a new test: it records into a new file
(signal_paper_s2_top1.db). The first S2 run (Top 10 sources, 12 closed trades locally, mean -15.2 %) is kept apart
and not mixed in. Entry, exit, costs and the decision rule are unchanged, except that with a single source
wallet the 95 % CI resamples trades (a CI by source wallet needs >= 2 wallets).

## Amendment 2 — 2026-10-07, user request (before S2b has any data)

New book S2b = S2 (follow only the #1 source) with a 1 s delay for both entry and exit instead of 3 s (paper fill
at the first trade >= 1 s after the source's buy / sell). Own file signal_paper_s2b.db; S2 keeps running unchanged
for comparison. 1 s is optimistic for a follower that only sees confirmed transactions. Same costs and decision rule.

## Amendment 3 — 2026-10-07, user decision

S2 and S2b are pinned to the wallet BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s (until now the #1 of the source
list) instead of "whatever wallet ranks #1". Data so far is kept: the #1 has been this wallet since both books
started (checked locally; the production ranking cannot be read without the access code, so a change of #1 on
production before this amendment cannot be excluded). Everything else unchanged.

## S3 — PRE_SNIPER_SIGNAL (registered 2026-10-07, before any S3 code ran on live data)

Research / paper only; not an auto-buy, not a trade candidate, not part of any production strategy or D1-D8.
S1, S2, S2b (pinned to BwWK17cb), Direction B / C, KOL and Smart Wallet methodology, recorder, copy rules and costs
are unchanged; S1 / S2 / S2b keep running as controls. Code: kolbot/s3.py.

**Question.** Before the sniper wallet BwWK17cb buys a token, does the early transaction flow show a fingerprint that
predicts incoming sniper money? Not an attempt to predict BwWK17cb itself.

**No look-ahead / no sniper data.**
* Clock = the bot's receive clock (ms). On-chain block times have 1 s resolution, so sub-second windows can only be
  measured on the receive clock, which is also the clock a live bot decides on.
* A fingerprint for window W is computed only from events received up to creation-receive time + W.
* BwWK17cb's transactions are removed before any feature is computed; its P&L, buys and sells are never inputs.
* If a BwWK17cb buy of the token was received before the signal time, the signal is INVALID_BWWK: stored, never
  traded, never counted. BwWK17cb's later buy time is stored only for the post-hoc latency metric.
* No future price, market cap, buyer count or token outcome is used for entry.

**Windows.** 100, 250, 500, 1000, 2000, 3000 ms after the creation event is received. Each window is its own
experiment and paper book; all are reported; no window will be "picked" afterwards.

**Fingerprint** (identical thresholds for every window, fixed now, not tuned):
* A/C. distinct non-creator buying wallets >= 3;
* B. SOL inflow from non-creator buys >= 1.0 SOL; buy share = buy SOL / (buy + sell SOL) of non-creators >= 0.8;
* C. largest single buyer's share of that inflow <= 0.6;
* E. creator holds <= 10 % of the supply from its own trades; the creator's stored profile risk (as stored at that
  moment) is not HIGH RISK, REPEAT FAILURE or SUSPICIOUS / RUG HISTORY.
* Stored for analysis but NOT used for entry: transaction count, acceleration (second-half vs first-half events).
* D/F (co-occurring early wallets, clusters) are NOT used in S3: no independent history exists yet. If wanted,
  they will be registered as S3b with clusters built only from data before the S3b start.

**Entry.** Paper buy ~$50 on the curve state 1 s after the signal (S2b's latency), recorded as
PRE_SNIPER_SIGNAL with the exact signal time and features. At most one entry per token per window; at most 10 open
per window book; same gates and funding as the other books.

**Exit.** TP +50 %, SL -30 % (net value a sale would return), 5 min max hold, migrated token = last curve price.

**Costs.** The shared model: real curve reserves, pump.fun fees, 1 % extra slippage per side, network + priority fee
per transaction. No special cost model.

**Metrics.** Per window: tokens evaluated, signals, trades, INVALID_BWWK count, number of signals followed by a
BwWK17cb buy, median signal -> BwWK17cb buy latency; per book: closed trades, wins, losses, win rate, mean /
median P&L, total P&L, ROI, best / worst, 95 % CI (clusters = creator).

**Decision rule.** n < 100 closed trades: no conclusion. n >= 100: CI lower bound > 0 = initial evidence of an edge
(PROVISIONAL, never PASS); otherwise no evidence. No re-tuning: any other threshold / exit is S3b, registered before
it runs, and S3 stays as the benchmark. The S3 sample starts when this registration is committed and deployed;
anything run before (local tests) is not part of it.

**Storage.** One row per fingerprint that passed (plus INVALID ones) and one per paper trade; no raw events.

## Amendment 4 — 2026-10-07, user decision (S3 has never run on live data; no S3 result exists)

Data integrity for S3 only (S1 / S2 / S2b unchanged):
* Stream gaps (listener reconnects) and process downtime (heartbeat older than 5 s at start) are stored. A window
  whose span [creation receive, signal / fill] overlaps a gap is GAP_INVALID (stored, never traded, never counted);
  a trade filled after such a gap is TRADED_GAP and excluded. There is no stream backfill, so these stay invalid; a
  row could only be revalidated by a backfill that re-confirms every event of its window.
* No fill while no event has been received for > 5 s; if a gap is then reported, the fill is invalidated.
* S3 books flag any trade whose holding overlaps any gap (gap_flag_s = 0 for S3); flagged trades are excluded.
* Pending fills are persisted. After a restart a fill is restored only if its time is still ahead and no gap
  overlaps it; otherwise INVALID_RESTART (also for SIGNAL rows written before this amendment).
* Reported per window: signals (valid only), gap_invalid, invalid_restart, invalid_bwwk.
