"""Find Solana coins in a post: mint addresses (bare or inside pump.fun / dexscreener / birdeye / gmgn / photon /
solscan links) and $TICKERS, plus buy wording. A mint is accepted only if it base58-decodes to exactly 32 bytes."""
from __future__ import annotations

import re
from dataclasses import dataclass

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
BUY_WORDS = re.compile(r"\b(buy|bought|aped|ape|aping|loading|loaded|entry|entered|bag|accumulat\w*|gem|send it|"
                       r"called|call|longing|long|early|moon\w*|x\d+|\d+x)\b", re.I)
SKIP_TICKERS = {"SOL", "USDC", "USDT", "BTC", "ETH", "BNB", "WSOL"}
NON_MINTS = {"So11111111111111111111111111111111111111112", "11111111111111111111111111111111",
             "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}


def b58decode(s: str) -> bytes | None:
    n = 0
    for c in s:
        i = B58.find(c)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def is_mint(s: str) -> bool:
    if s in NON_MINTS:
        return False
    b = b58decode(s)
    return b is not None and len(b) == 32


@dataclass
class Found:
    mints: list
    tickers: list
    buy_words: bool


def extract(text: str, urls: list | None = None) -> Found:
    blob = " ".join([text or ""] + list(urls or []))
    mints = []
    for m in B58_RE.findall(blob):
        if is_mint(m) and m not in mints:
            mints.append(m)
    tickers = []
    for t in TICKER_RE.findall(text or ""):
        t = t.upper()
        if t not in SKIP_TICKERS and t not in tickers:
            tickers.append(t)
    return Found(mints, tickers, bool(BUY_WORDS.search(text or "")))


# --- X post ids carry their creation time (snowflake) ----------------------------------------------------------------
TWEPOCH_MS = 1288834974657
POST_URL_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")


def snowflake_time(post_id: str | int) -> float:
    """Exact creation time (unix seconds, ms precision) of an X post from its id."""
    return ((int(post_id) >> 22) + TWEPOCH_MS) / 1000.0


def parse_post_url(url: str) -> tuple[str, str] | None:
    m = POST_URL_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None
