# Paper books H1 / H2 — PRE-REGISTERED (2026-10-06, before either book has any data)

Paper only. No real order, no change to the KOL paper bot, its copy rules, the Smart Wallet module, Direction B / C
of sol_memecoin_hunter, or the signal rules. Nothing below may be changed after data is seen (a change = a new test).

## Why these two

The seven existing signal books lose (local: Top 10 −32.8 % of capital after 32 trades; Top 20 −13.9 % after 25).
Two mechanisms were visible in the data that motivated them (exploratory, small samples — not evidence of an edge):

1. Exit too early: the rule "exit 3 s after the source's FIRST sell" gave a median paper hold of 18 s while the
   main source (sssssDdM) holds 183 s on average — it sells a small part early.
2. Adverse selection after a single source buy: the token's market cap 5 min after a single source buy had a
   median of −23.8 % (n = 26).

## Common rules (identical to the existing books)

~$50 per entry at the current SOL price; 3 s delay; fill on the real bonding-curve reserves; pump.fun fees,
1 % extra slippage per side, network + priority fee per transaction; at most 10 open positions; one position per
token; 24 h max hold; a migrated token exits at its last curve price. Sources = the signal sources in force
(Top 10 of each list, status not REJECT, average hold >= 60 s, no backfill below #10).

## H1 — later exit

* Entry: exactly the Top 10 book's entry (one paper buy per displayed signal).
* Exit: 3 s after the source wallet has sold >= 50 % of the tokens it bought in that token, counted from the
  signal buy (its later buys add to the total). If that happens before the paper buy fills, the buy is cancelled.

## H2 — confluence

* Entry: when >= 2 DIFFERENT source wallets (Top 10 of either list, rules above) have each bought >= 0.05 SOL of the
  same token within 10 minutes (30 min (wallet, token) dedup), one paper buy at the second wallet's buy (+3 s).
* Exit: 3 s after those source wallets together have sold >= 50 % of the tokens they bought, counted from their
  triggering buys; same cancel rule as H1.

## Accounting (all books, user decision 2026-10-06)

Every entry is ~$50 whatever the balance; when cash is short the book is topped up and the top-up is recorded.
Shown: balance if the book had only its $500 (can go negative = bust), max drawdown of realized P&L.

## Decision rule (fixed now)

Per book, on closed trades not overlapping a stream gap:

* n < 100: no conclusion.
* n >= 100: mean net return per trade after all costs, 95 % bootstrap CI resampling whole source wallets
  (the dashboard's CI). Lower bound > 0 -> "PROVISIONAL" (no random control: never PASS). Otherwise REJECT.
* With 9 books running on the same stream, at least one may look positive by chance: a single positive book among
  nine is NOT taken as an edge unless it stays positive on a fresh forward period of the same length afterwards.
* No real money before a book passes AND survives 2-4 more weeks of unchanged paper trading.
