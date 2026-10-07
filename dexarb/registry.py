"""Chains, assets, protocols and wallet support (pre-registered universe, docs/prereg_dex_arbitrage.md).

Identity: every chain has its own key + chain_id (EVM) / cluster (Solana); balances and addresses are per chain.
A protocol is SUPPORTED only if (1) its read path has deterministic fixture tests and (2) a recorded live read-only
check (tools/dexarb_verify.py -> docs/dexarb_connector_verification.json) quoted the configured size for every
registered pair on that chain with a known route. Otherwise UNVERIFIED (never used for opportunities). A protocol
whose feed fails at run time is UNAVAILABLE / STALE in feed_health and produces no opportunity."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

VERIFY_FILE = Path(__file__).resolve().parents[1] / "docs" / "dexarb_connector_verification.json"


@dataclass(frozen=True)
class Asset:
    symbol: str
    address: str              # EVM: lower-case hex; Solana: mint
    decimals: int


@dataclass(frozen=True)
class Protocol:
    key: str                  # unique per chain
    name: str
    kind: str                 # "v2_router" | "v3_quoter" | "jupiter_single_dex"
    version: str
    address: str              # router / quoter / Jupiter dex label
    pool_types: str
    quote_source: str


@dataclass(frozen=True)
class Chain:
    key: str
    kind: str                 # "evm" | "solana"
    chain_id: int | str
    native: str
    wrapped: Asset
    quote_asset: Asset        # P&L unit of the chain (a stablecoin: may depeg, never assumed = 1 USD)
    tokens: tuple             # tokens traded against quote_asset
    protocols: tuple
    rpc_env: str
    rpc_default: str
    scan_every_s: float
    wallets: dict = field(default_factory=dict)   # wallet -> support status + source

    def rpc_url(self) -> str:
        return os.environ.get(self.rpc_env) or self.rpc_default


A = Asset
# Wallet support checked 2026-10-07 (web): Trust Wallet lists swaps on Ethereum, BNB Chain, Polygon, Solana, Base;
# Phantom supports Solana, Ethereum, Base, Polygon; Phantom announced BNB Chain on 2026-10-01 but its help-center
# network list still showed BSC as unsupported -> UNVERIFIED for Phantom on BNB.
W_BOTH = {"trust_wallet": "SUPPORTED (trustwallet.com swap guide)", "phantom": "SUPPORTED (help.phantom.com list)"}
W_BNB = {"trust_wallet": "SUPPORTED (trustwallet.com swap guide)",
         "phantom": "UNVERIFIED (announced 2026-10-01; help-center list not updated)"}

CHAINS: dict[str, Chain] = {
    "base": Chain("base", "evm", 8453, "ETH", A("WETH", "0x4200000000000000000000000000000000000006", 18),
                  A("USDC", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6),
                  (A("WETH", "0x4200000000000000000000000000000000000006", 18),
                   A("cbBTC", "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", 8)),
                  (Protocol("uniswap_v2", "Uniswap V2", "v2_router", "2", "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
                            "constant product 0.30 %", "router getAmountsOut (eth_call)"),
                   Protocol("sushiswap_v2", "SushiSwap V2", "v2_router", "2", "0x6bded42c6da8fbf0d2ba55b2fa120c5e0c8d7891",
                            "constant product 0.30 %", "router getAmountsOut (eth_call)"),
                   Protocol("uniswap_v3", "Uniswap V3", "v3_quoter", "3", "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a",
                            "concentrated, fee tiers 0.01/0.05/0.3/1 %", "QuoterV2 quoteExactInputSingle (eth_call)"),
                   Protocol("pancakeswap_v3", "PancakeSwap V3", "v3_quoter", "3",
                            "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997", "concentrated, fee tiers 0.01/0.05/0.25/1 %",
                            "QuoterV2 quoteExactInputSingle (eth_call)")),
                  "DEXARB_RPC_BASE", "https://base-rpc.publicnode.com", 15.0, W_BOTH),
    "polygon": Chain("polygon", "evm", 137, "POL", A("WPOL", "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270", 18),
                     A("USDC", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6),
                     (A("WETH", "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", 18),
                      A("WPOL", "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270", 18)),
                     (Protocol("quickswap_v2", "QuickSwap V2", "v2_router", "2",
                               "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff", "constant product 0.30 %",
                               "router getAmountsOut (eth_call)"),
                      Protocol("sushiswap_v2", "SushiSwap V2", "v2_router", "2",
                               "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", "constant product 0.30 %",
                               "router getAmountsOut (eth_call)"),
                      Protocol("uniswap_v3", "Uniswap V3", "v3_quoter", "3",
                               "0x61ffe014ba17989e743c5f6cb21bf9697530b21e", "concentrated, fee tiers",
                               "QuoterV2 quoteExactInputSingle (eth_call)")),
                     "DEXARB_RPC_POLYGON", "https://polygon-bor-rpc.publicnode.com", 15.0, W_BOTH),
    "ethereum": Chain("ethereum", "evm", 1, "ETH", A("WETH", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18),
                      A("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
                      (A("WETH", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18),
                       A("WBTC", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", 8)),
                      (Protocol("uniswap_v2", "Uniswap V2", "v2_router", "2",
                                "0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "constant product 0.30 %",
                                "router getAmountsOut (eth_call)"),
                       Protocol("sushiswap_v2", "SushiSwap V2", "v2_router", "2",
                                "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f", "constant product 0.30 %",
                                "router getAmountsOut (eth_call)"),
                       Protocol("uniswap_v3", "Uniswap V3", "v3_quoter", "3",
                                "0x61ffe014ba17989e743c5f6cb21bf9697530b21e", "concentrated, fee tiers",
                                "QuoterV2 quoteExactInputSingle (eth_call)")),
                      "DEXARB_RPC_ETHEREUM", "https://ethereum-rpc.publicnode.com", 24.0, W_BOTH),
    "bnb": Chain("bnb", "evm", 56, "BNB", A("WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", 18),
                 A("USDT", "0x55d398326f99059ff775485246999027b3197955", 18),
                 (A("WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", 18),
                  A("ETH", "0x2170ed0880ac9a755fd29b2688956bd959f933f8", 18)),
                 (Protocol("pancakeswap_v2", "PancakeSwap V2", "v2_router", "2",
                           "0x10ed43c718714eb63d5aa57b78b54704e256024e", "constant product 0.25 %",
                           "router getAmountsOut (eth_call)"),
                  Protocol("biswap_v2", "BiSwap V2", "v2_router", "2", "0x3a6d8ca21d1cf76f653a67577fa0d27453350dd8",
                           "constant product (pair-specific fee)", "router getAmountsOut (eth_call)"),
                  Protocol("pancakeswap_v3", "PancakeSwap V3", "v3_quoter", "3",
                           "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997", "concentrated, fee tiers",
                           "QuoterV2 quoteExactInputSingle (eth_call)"),
                  Protocol("uniswap_v3", "Uniswap V3", "v3_quoter", "3", "0x78d78e420da98ad378d7799be8f4af69033eb077",
                           "concentrated, fee tiers", "QuoterV2 quoteExactInputSingle (eth_call)")),
                 "DEXARB_RPC_BNB", "https://bsc-rpc.publicnode.com", 15.0, W_BNB),
    "solana": Chain("solana", "solana", "mainnet-beta", "SOL",
                    A("SOL", "So11111111111111111111111111111111111111112", 9),
                    A("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
                    (A("SOL", "So11111111111111111111111111111111111111112", 9),
                     A("JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6)),
                    tuple(Protocol(k, n, "jupiter_single_dex", "jupiter swap/v1", n,
                                   "single DEX, direct route only (onlyDirectRoutes, dexes=<label>)",
                                   "lite-api.jup.ag/swap/v1/quote restricted to one DEX; routePlan label checked")
                          for k, n in (("raydium", "Raydium"), ("raydium_clmm", "Raydium CLMM"),
                                       ("orca_whirlpool", "Whirlpool"), ("meteora_dlmm", "Meteora DLMM"))),
                    "DEXARB_RPC_SOLANA", "https://api.mainnet-beta.solana.com", 15.0, W_BOTH),
}

# Atomic composition (paper model B): no protocol here can be composed into ONE transaction that is simulated as a
# whole without a custom executor contract / program, which this paper-only task must not build.
ATOMIC = {f"{c}/{p.key}": "NOT_SUPPORTED (no bundle / executor simulation)" for c, ch in CHAINS.items()
          for p in ch.protocols}


def verification() -> dict:
    try:
        return json.loads(VERIFY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def status(chain: str, proto: str, ver: dict | None = None) -> str:
    rec = (verification() if ver is None else ver).get(f"{chain}/{proto}") or {}
    return "SUPPORTED" if rec.get("ok") else "UNVERIFIED"


def supported(chain: str, ver: dict | None = None) -> list[Protocol]:
    ver = verification() if ver is None else ver
    return [p for p in CHAINS[chain].protocols if status(chain, p.key, ver) == "SUPPORTED"]


def coverage(ver: dict | None = None) -> list[dict]:
    ver = verification() if ver is None else ver
    return [{"chain": c.key, "chain_id": c.chain_id, "protocol": p.key, "name": p.name, "version": p.version,
             "address": p.address, "pool_types": p.pool_types, "quote_source": p.quote_source,
             "status": status(c.key, p.key, ver), "atomic": ATOMIC[f"{c.key}/{p.key}"],
             "checked_at": (ver.get(f"{c.key}/{p.key}") or {}).get("checked_at"),
             "note": (ver.get(f"{c.key}/{p.key}") or {}).get("note"), "wallets": c.wallets}
            for c in CHAINS.values() for p in c.protocols]
