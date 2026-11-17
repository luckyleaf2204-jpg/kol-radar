"""Read-only market data for calls (no key): DexScreener (snapshot, ticker search), GeckoTerminal (historical candles
of the bonding-curve pool and the main AMM pool, merged), Solana RPC (mint / freeze authority, holder concentration).
Every network call goes through `get` / `rpc` so tests run offline. Nothing here signs or sends anything."""
from __future__ import annotations

import json
import time
import urllib.request

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
RPC_URL = "https://api.mainnet-beta.solana.com"
GT_DEXES = ("pump-fun", "pumpswap", "raydium", "raydium-clmm", "meteora", "meteora-damm-v2", "orca")


def http_get(url: str, timeout: float = 25):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def http_rpc(method: str, params: list, url: str = RPC_URL, timeout: float = 25):
    if method not in ("getAccountInfo", "getTokenLargestAccounts", "getTokenSupply"):
        raise PermissionError(f"read-only market client: {method} not allowed")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    if out.get("error"):
        raise RuntimeError(str(out["error"])[:200])
    return out.get("result")


GT_MIN_GAP_S = 2.5            # <= 24 GeckoTerminal calls / minute (its free limit is ~30, the Insiders charts share it)


class Market:
    def __init__(self, get=http_get, rpc=http_rpc, sleep=time.sleep, now=time.time, gt_gap_s: float = GT_MIN_GAP_S):
        self._raw_get, self.rpc, self.sleep, self.now = get, rpc, sleep, now
        self.gt_gap_s, self._gt_next = gt_gap_s, 0.0
        self.pool_cache: dict[str, tuple[float, list]] = {}

    def get(self, url: str):
        """GeckoTerminal calls are paced (one every gt_gap_s) and retried once after a 429; others pass through."""
        if not url.startswith(GT):
            return self._raw_get(url)
        for attempt in range(2):
            wait = self._gt_next - self.now()
            if wait > 0:
                self.sleep(wait)
            self._gt_next = self.now() + self.gt_gap_s
            try:
                return self._raw_get(url)
            except Exception as e:
                if attempt == 0 and "429" in str(e):
                    self.sleep(10)
                    continue
                raise

    # --- DexScreener -------------------------------------------------------------------------------------------------
    def snapshot(self, mint: str) -> dict | None:
        """Best Solana pair of the coin now: price, mcap, liquidity, volume, changes, age, venue."""
        d = self.get(f"{DS}/latest/dex/tokens/{mint}") or {}
        pairs = [p for p in d.get("pairs") or [] if p.get("chainId") == "solana"
                 and (p.get("baseToken") or {}).get("address") == mint]
        if not pairs:
            return None
        p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
        f = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
        return {"mint": mint, "symbol": (p.get("baseToken") or {}).get("symbol"),
                "name": (p.get("baseToken") or {}).get("name"), "dex": p.get("dexId"), "pair": p.get("pairAddress"),
                "price_usd": f(p.get("priceUsd")), "mcap": f(p.get("marketCap") or p.get("fdv")),
                "liquidity_usd": f((p.get("liquidity") or {}).get("usd")),
                "volume_h1": f((p.get("volume") or {}).get("h1")), "volume_h24": f((p.get("volume") or {}).get("h24")),
                "change_h1": f((p.get("priceChange") or {}).get("h1")),
                "change_h24": f((p.get("priceChange") or {}).get("h24")),
                "created_at": (p.get("pairCreatedAt") or 0) / 1000 or None, "at": self.now()}

    def resolve_ticker(self, ticker: str, before_ts: float) -> tuple[str | None, str]:
        """$TICKER -> mint only when unambiguous: the most liquid Solana pair with that symbol, created before the
        post, holding >= 3x the liquidity of the next one. Otherwise (None, reason) — never a guess."""
        d = self.get(f"{DS}/latest/dex/search?q={ticker}") or {}
        cands = {}
        for p in d.get("pairs") or []:
            b = p.get("baseToken") or {}
            if p.get("chainId") != "solana" or (b.get("symbol") or "").upper() != ticker.upper():
                continue
            if (p.get("pairCreatedAt") or 0) / 1000 > before_ts:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            cands[b.get("address")] = max(liq, cands.get(b.get("address"), 0))
        if not cands:
            return None, "no_solana_pair_before_post"
        ranked = sorted(cands.items(), key=lambda x: -x[1])
        if len(ranked) > 1 and ranked[0][1] < 3 * ranked[1][1]:
            return None, f"ambiguous_{len(ranked)}_candidates"
        return ranked[0][0], "resolved_unique"

    # --- GeckoTerminal -------------------------------------------------------------------------------------------------
    def pools(self, mint: str) -> list[dict]:
        hit = self.pool_cache.get(mint)
        if hit and self.now() - hit[0] < 600:
            return hit[1]
        data = (self.get(f"{GT}/tokens/{mint}/pools?page=1") or {}).get("data") or []
        out = []
        for p in data:
            a, r = p.get("attributes") or {}, p.get("relationships") or {}
            dex = ((r.get("dex") or {}).get("data") or {}).get("id")
            base = ((r.get("base_token") or {}).get("data") or {}).get("id", "")
            if base == f"solana_{mint}" and dex in GT_DEXES:
                out.append({"address": a.get("address"), "dex": dex, "reserve": float(a.get("reserve_in_usd") or 0)})
        curve = [p for p in out if p["dex"] == "pump-fun"][:1]
        amm = sorted((p for p in out if p["dex"] != "pump-fun"), key=lambda p: -p["reserve"])[:1]
        self.pool_cache[mint] = (self.now(), curve + amm)
        return curve + amm

    def candles(self, mint: str, t0: float, t1: float, minutes: int = 1) -> list[list]:
        """[[ts, open, high, low, close, volume_usd]] covering [t0, t1] (USD per token), both pools merged (the
        busier candle wins a duplicate timestamp). Paged backwards with before_timestamp, 1 000 candles a page."""
        tf, agg = ("hour", minutes // 60) if minutes >= 60 else ("minute", minutes)
        step = minutes * 60
        merged: dict[int, list] = {}
        for p in self.pools(mint):
            before = int(t1) + step
            for _ in range(6):                                 # at most 6 pages per pool
                url = (f"{GT}/pools/{p['address']}/ohlcv/{tf}?aggregate={agg}&limit=1000&currency=usd"
                       f"&before_timestamp={before}")
                d = self.get(url)                              # paced + one retry on 429 (see get)
                rows = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                if not rows:
                    break
                for c in rows:
                    ts = int(c[0])
                    if t0 - step <= ts <= t1 and (ts not in merged or (c[5] or 0) > (merged[ts][5] or 0)):
                        merged[ts] = [ts, *[float(x) for x in c[1:6]]]
                oldest = min(int(c[0]) for c in rows)
                if oldest <= t0 or len(rows) < 1000:
                    break
                before = oldest
        return [merged[k] for k in sorted(merged)]

    # --- Solana RPC: safety ------------------------------------------------------------------------------------------
    def safety(self, mint: str) -> dict:
        """mint / freeze authority still set, and the share of supply in the 10 largest accounts (pools included:
        a coarse indicator, labelled so)."""
        out = {"mint_authority": None, "freeze_authority": None, "top10_share": None, "error": None}
        try:
            acc = self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
            info = ((((acc.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            out["mint_authority"] = info.get("mintAuthority")
            out["freeze_authority"] = info.get("freezeAuthority")
            supply = float(info.get("supply") or 0)
            big = self.rpc("getTokenLargestAccounts", [mint]) or {}
            amounts = [float(x.get("amount") or 0) for x in (big.get("value") or [])[:10]]
            out["top10_share"] = sum(amounts) / supply if supply else None
        except Exception as e:
            out["error"] = f"{type(e).__name__}"
        return out
