"""Token metadata, creator history and SOL/USD price from public read-only endpoints, fetched in the background
with a throttle and cached. pump.fun's frontend API is unofficial and rate-limited (429): failures just leave the
field empty."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

PUMP_API = "https://frontend-api-v3.pump.fun"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_PRICE = f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}"
GAP_S = 1.2                    # at most ~1 pump.fun request per GAP_S


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def creator_rating(coins: list[dict], current_mint: str | None = None) -> dict:
    """Heuristic, shown with its numbers: how many coins this wallet launched, how many finished the curve."""
    past = [c for c in coins if c.get("mint") != current_mint]
    n = len(past)
    grad = sum(1 for c in past if c.get("complete"))
    best = max((c.get("ath_market_cap") or c.get("usd_market_cap") or 0 for c in past), default=0)
    if n == 0:
        label, tone = "Dev mới (coin đầu tiên)", "warn"
    elif n >= 10 and grad == 0:
        label, tone = "Spam: nhiều coin, không coin nào tốt nghiệp", "bad"
    elif grad >= 1 and grad / n >= 0.2:
        label, tone = "Có lịch sử tốt", "good"
    elif grad >= 1:
        label, tone = "Trung bình", "warn"
    else:
        label, tone = "Chưa có coin tốt nghiệp", "warn"
    return {"coins": n, "more": len(coins) >= 50, "graduated": grad, "best_ath_usd": round(best), "label": label,
            "tone": tone}


class Meta:
    def __init__(self, log=print, on_creator=None, on_coin=None):
        self.log, self.on_creator = log, on_creator      # on_creator(wallet, coins): persist a dev's history
        self.on_coin = on_coin                           # on_coin(mint, coin): fill name / creation time
        self.coins: dict[str, dict] = {}
        self.creators: dict[str, dict] = {}
        self.sol_usd: float | None = None
        self.queue: asyncio.Queue | None = None
        self.queued: set[str] = set()
        self.creator_at: dict[str, float] = {}

    def want(self, mint: str) -> None:
        if mint not in self.coins and mint not in self.queued and self.queue is not None:
            self.queued.add(mint)
            self.queue.put_nowait(("mint", mint))

    def want_creator(self, wallet: str, max_age_s: float = 3600) -> None:
        key = "creator:" + wallet
        fresh = time.time() - self.creator_at.get(wallet, 0) < max_age_s
        if not fresh and key not in self.queued and self.queue is not None:
            self.queued.add(key)
            self.queue.put_nowait(("creator", wallet))

    async def _creator(self, cr: str, current_mint: str | None = None) -> None:
        lst = await asyncio.to_thread(_get, f"{PUMP_API}/coins?creator={cr}&limit=50&offset=0&includeNsfw=true")
        lst = lst if isinstance(lst, list) else []
        self.creators[cr] = creator_rating(lst, current_mint)
        self.creator_at[cr] = time.time()
        if self.on_creator:
            self.on_creator(cr, lst)

    async def run(self, stop: asyncio.Event) -> None:
        self.queue = asyncio.Queue()
        asyncio.get_running_loop().create_task(self._price_loop(stop))
        while not stop.is_set():
            kind, key = await self.queue.get()
            try:
                if kind == "creator":
                    await self._creator(key)
                else:
                    d = await asyncio.to_thread(_get, f"{PUMP_API}/coins-v2/{key}")
                    self.coins[key] = {k: d.get(k) for k in ("name", "symbol", "image_uri", "creator", "twitter",
                                                             "telegram", "website", "reply_count",
                                                             "is_currently_live", "created_timestamp",
                                                             "ath_market_cap")}
                    if self.on_coin:
                        self.on_coin(key, d)
                    cr = d.get("creator")
                    if cr and cr not in self.creators:
                        await asyncio.sleep(GAP_S)
                        await self._creator(cr, key)
            except Exception as e:
                self.log(f"[meta] {kind} {key[:8]}.. {type(e).__name__}: {str(e)[:80]}")
                if kind == "mint":
                    self.coins.setdefault(key, {})
            finally:
                self.queued.discard(key if kind == "mint" else "creator:" + key)
            await asyncio.sleep(GAP_S)

    async def _price_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                d = await asyncio.to_thread(_get, JUP_PRICE)
                self.sol_usd = float(d[SOL_MINT]["usdPrice"])
            except Exception as e:
                self.log(f"[meta] SOL price {type(e).__name__}")
            await asyncio.sleep(60)

    def refresh_old(self, mints, max_age_s: float = 300) -> None:
        """Re-fetch metadata (comments, live flag) of the given tokens every max_age_s."""
        now = time.time()
        for m in mints:
            c = self.coins.get(m)
            if c is not None and now - c.get("_at", now) > max_age_s:
                self.coins.pop(m, None)
                self.want(m)
            elif c is not None:
                c.setdefault("_at", now)
