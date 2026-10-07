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
