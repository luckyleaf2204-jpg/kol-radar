"""Outcome of one call, on the price a follower could actually have traded (docs/callers_method.md).

  entry_raw  close of the first 1-minute candle that ends at or after post time + DETECT_LATENCY_S (never the low
             that came later, never the wick)
  eff_entry  entry_raw x (1 + fee + extra slippage + buy impact); impact = TRADE_USD / (liquidity / 2)
  per horizon h (1h / 6h / 24h / 7d, from the entry):
    theoretical_gain  max candle HIGH / entry_raw - 1             (the chart peak: not sellable)
    tradable_gain     max over candles of net_sell(CLOSE) / eff_entry - 1, net_sell(P) = P x (1 - fee - extra -
                      sell impact), sell impact = TRADE_USD x (P / entry_raw) / (liquidity / 2), capped at 99 %
    return_at         net_sell(close at the end of h) / eff_entry - 1   (holding blindly until h)
    time_to_peak_s    when the best tradable gain happened
  class      CLASSES on the best tradable gain within WIN_HORIZON (7d); `final` once 7d has passed
  rug        price at or below -90 % of entry inside 24 h and never back above -50 % afterwards (inside 24 h)
  runup_1h / runup_24h   price change from 1 h / 24 h before the post to entry_raw (late-call detector)
Liquidity: a constant-product pool's two-sided value scales with sqrt(price), so the liquidity at any price P is
estimated as L_ref x sqrt(P / P_ref) from one observation (L_ref at price P_ref): the snapshot taken when the call
was detected (liq_source AT_CALL) or, for an imported old call, today's (CURRENT_ESTIMATE). This keeps a coin that
grew 50x from looking 7x more liquid at the call than it was. LP adds / removals are not seen. All figures exclude
anything we cannot see (MEV, failed txs)."""
from __future__ import annotations

from callers import config as C


def classify(gain: float | None) -> str:
    if gain is None:
        return "UNKNOWN"
    for th, name in C.CLASSES:
        if gain >= th:
            return name
    return "MISS"


def _impact(usd: float, liq: float | None) -> float:
    if not liq or liq <= 0:
        return 0.5                                   # unknown liquidity: punish hard rather than flatter the caller
    return min(0.99, usd / (liq / 2))


def evaluate_call(post_time: float, fine: list, coarse: list, liquidity: float | None, dex: str | None,
                  now: float, liq_source: str = "CURRENT_ESTIMATE", liq_price: float | None = None) -> dict:
    """fine: 1-minute candles around [post-1h, post+6h]; coarse: 15-minute candles around [post-24h, post+7d].
    Candles: [ts, open, high, low, close, volume]."""
    fee = C.FEE_BY_DEX.get(dex or "", C.DEFAULT_FEE)
    target = post_time + C.DETECT_LATENCY_S
    entry = next((c for c in fine if c[0] + 60 >= target and c[0] <= target + 600), None)
    if entry is None:
        entry = next((c for c in coarse if c[0] + 900 >= target and c[0] <= target + 1800), None)
    out = {"method": C.METHOD_VERSION, "dex": dex, "fee": fee, "liquidity_usd": liquidity, "liq_source": liq_source,
           "trade_usd": C.TRADE_USD, "post_time": post_time}
    if entry is None or not entry[4]:
        return {**out, "status": "NO_PRICE", "final": int(now - post_time > C.HORIZONS_S["7d"] + 86400),
                "cls": "UNKNOWN", "note": "no traded candle within 10 min after the post (not tradable / not indexed)"}
    entry_ts = entry[0] + 60 if entry in fine else entry[0] + 900
    raw = float(entry[4])

    def liq_at(p: float) -> float | None:
        if not liquidity or liquidity <= 0:
            return None
        return liquidity * (p / liq_price) ** 0.5 if liq_price and liq_price > 0 else liquidity

    liq_entry = liq_at(raw)
    buy_imp = _impact(C.TRADE_USD, liq_entry)
    eff = raw * (1 + fee + C.EXTRA_SLIPPAGE + buy_imp)

    def net_sell(p: float) -> float:
        lp = liq_at(p)
        imp = min(0.99, C.TRADE_USD * (p / raw) / (lp / 2)) if lp and lp > 0 else 0.5
        return p * max(0.0, 1 - fee - C.EXTRA_SLIPPAGE - imp)

    fine_end = fine[-1][0] if fine else entry_ts
    series = [c for c in fine if c[0] >= entry_ts - 60] + [c for c in coarse if c[0] > fine_end]
    horizons = {}
    for name, h in C.HORIZONS_S.items():
        end = entry_ts + h
        win = [c for c in series if c[0] < end]
        elapsed = now >= end
        if not win:
            horizons[name] = {"status": "PENDING" if not elapsed else "NO_TRADES"}
            continue
        best = max(win, key=lambda c: net_sell(c[4]))
        horizons[name] = {"status": "FINAL" if elapsed else "SO_FAR",
                          "theoretical_gain": max(c[2] for c in win) / raw - 1,
                          "tradable_gain": net_sell(best[4]) / eff - 1,
                          "time_to_peak_s": best[0] - entry_ts,
                          "return_at": net_sell(win[-1][4]) / eff - 1 if elapsed else None,
                          "low_vs_entry": min(c[3] for c in win) / raw - 1}
    day = [c for c in series if c[0] < entry_ts + 86400]
    rug = False
    if day:
        lows = [i for i, c in enumerate(day) if c[3] <= raw * (1 - C.RUG_DROP)]
        if lows and max(c[4] for c in day[lows[0]:]) < raw * 0.5:
            rug = True

    def price_before(sec: float) -> float | None:
        t = post_time - sec
        pool = fine if sec <= 3600 else coarse
        cands = [c for c in pool if c[0] <= t]
        return cands[-1][4] if cands else None
    p1, p24 = price_before(3600), price_before(86400)
    w = horizons.get(C.WIN_HORIZON, {})
    best_gain = max((v.get("tradable_gain") for v in horizons.values() if v.get("tradable_gain") is not None),
                    default=None)
    final = w.get("status") in ("FINAL", "NO_TRADES")
    return {**out, "status": "OK", "entry_ts": entry_ts, "entry_raw": raw, "eff_entry": eff, "buy_impact": buy_imp,
            "liquidity_at_entry_est": liq_entry, "liq_price": liq_price,
            "horizons": horizons, "best_tradable_gain": best_gain,
            "best_theoretical_gain": max((v.get("theoretical_gain") for v in horizons.values()
                                          if v.get("theoretical_gain") is not None), default=None),
            "cls": classify(best_gain), "final": int(final), "rug": rug,
            "runup_1h": raw / p1 - 1 if p1 else None, "runup_24h": raw / p24 - 1 if p24 else None}
