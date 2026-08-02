#!/usr/bin/env python3
"""
Data fetcher for whale transactions, prices, and market data.
Uses free APIs: CoinGecko, Etherscan, DexScreener, Whale Alert.
"""

import os
import httpx
import logging
from datetime import datetime

log = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest"
WHALE_ALERT_BASE = "https://api.whale-alert.io/v1"
ETHERSCAN_BASE = "https://api.etherscan.io/api"

# Common token name -> CoinGecko ID mapping
TOKEN_MAP = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "bnb": "binancecoin", "usdt": "tether",
    "usdc": "usd-coin", "xrp": "ripple",
    "ada": "cardano", "doge": "dogecoin",
    "avax": "avalanche-2", "matic": "matic-network",
    "dot": "polkadot", "link": "chainlink",
    "uni": "uniswap", "atom": "cosmos",
    "near": "near", "arb": "arbitrum",
    "op": "optimism", "apt": "aptos",
    "sui": "sui", "sei": "sei-network",
    "ton": "the-open-network", "pepe": "pepe",
    "shib": "shiba-inu", "wif": "dogwifcoin",
}


async def get_price(symbol: str) -> dict | None:
    """Get current price for a token."""
    coin_id = TOKEN_MAP.get(symbol.lower(), symbol.lower())

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{COINGECKO_BASE}/coins/{coin_id}",
                params={"localization": "false", "tickers": "false",
                        "community_data": "false", "developer_data": "false"},
            )
            if r.status_code != 200:
                # Try search
                r2 = await client.get(
                    f"{COINGECKO_BASE}/search", params={"query": symbol}
                )
                if r2.status_code == 200:
                    coins = r2.json().get("coins", [])
                    if coins:
                        coin_id = coins[0]["id"]
                        r = await client.get(
                            f"{COINGECKO_BASE}/coins/{coin_id}",
                            params={"localization": "false", "tickers": "false",
                                    "community_data": "false", "developer_data": "false"},
                        )
                    else:
                        return None

            data = r.json()
            market = data.get("market_data", {})
            return {
                "name": data.get("name", symbol),
                "symbol": data.get("symbol", symbol),
                "price": market.get("current_price", {}).get("usd", 0),
                "change_24h": market.get("price_change_percentage_24h", 0) or 0,
                "market_cap": market.get("market_cap", {}).get("usd", 0),
                "volume": market.get("total_volume", {}).get("usd", 0),
            }
        except Exception as e:
            log.error(f"Price fetch error: {e}")
            return None


async def get_top_movers() -> dict | None:
    """Get top gainers and losers in the last 24h."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
            )
            if r.status_code != 200:
                return None

            coins = r.json()
            sorted_coins = sorted(
                [c for c in coins if c.get("price_change_percentage_24h") is not None],
                key=lambda c: c["price_change_percentage_24h"],
                reverse=True,
            )

            gainers = [
                {"symbol": c["symbol"], "price": c["current_price"],
                 "change": c["price_change_percentage_24h"]}
                for c in sorted_coins[:5]
            ]
            losers = [
                {"symbol": c["symbol"], "price": c["current_price"],
                 "change": c["price_change_percentage_24h"]}
                for c in sorted_coins[-5:]
            ]
            return {"gainers": gainers, "losers": losers}
        except Exception as e:
            log.error(f"Top movers error: {e}")
            return None


async def get_recent_whales(limit: int = 5) -> list[dict]:
    """
    Largest real trades visible right now, for the on-demand /whales command.

    Auto-alerts come from data.whale_source, which watches blocks and live
    order flow. This is the same data seen as a snapshot, because a user
    running /whales cannot wait ten minutes for the next Bitcoin block.
    """
    from data.whale_source import fetch_recent_snapshot
    return await fetch_recent_snapshot(limit=limit)


async def get_gas_prices() -> dict | None:
    """Get current ETH gas prices."""
    api_key = os.environ.get("ETHERSCAN_API_KEY", "")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            if api_key:
                r = await client.get(
                    ETHERSCAN_BASE,
                    params={"module": "gastracker", "action": "gasoracle", "apikey": api_key},
                )
                if r.status_code == 200:
                    result = r.json().get("result", {})
                    return {
                        "slow": result.get("SafeGasPrice", "?"),
                        "standard": result.get("ProposeGasPrice", "?"),
                        "fast": result.get("FastGasPrice", "?"),
                    }

            # Fallback: use blocknative or simple estimate
            r2 = await client.get("https://api.blocknative.com/gasprices/blockprices")
            if r2.status_code == 200:
                prices = r2.json().get("blockPrices", [{}])[0].get("estimatedPrices", [])
                if len(prices) >= 3:
                    return {
                        "slow": str(int(prices[2].get("price", 0))),
                        "standard": str(int(prices[1].get("price", 0))),
                        "fast": str(int(prices[0].get("price", 0))),
                    }
        except Exception as e:
            log.error(f"Gas price error: {e}")

    return {"slow": "~15", "standard": "~20", "fast": "~30"}
