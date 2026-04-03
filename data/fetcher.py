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
    Get recent large transactions from DexScreener — both buys AND sells.
    """
    whales = []
    queries = ["WETH%20USDC", "SOL%20USDT", "WBTC%20USDT"]

    async with httpx.AsyncClient(timeout=10) as client:
        for query in queries:
            try:
                r = await client.get(f"{DEXSCREENER_BASE}/dex/search?q={query}")
                if r.status_code != 200:
                    continue
                pairs = r.json().get("pairs", [])
                for pair in pairs:
                    vol = pair.get("volume", {}).get("h24", 0) or 0
                    if vol < 500_000:
                        continue

                    change_5m = pair.get("priceChange", {}).get("m5", 0) or 0
                    change_1h = pair.get("priceChange", {}).get("h1", 0) or 0
                    change_24h = pair.get("priceChange", {}).get("h24", 0) or 0
                    buys = pair.get("txns", {}).get("h1", {}).get("buys", 0)
                    sells = pair.get("txns", {}).get("h1", {}).get("sells", 0)

                    if buys > sells and change_1h >= 0:
                        direction = "buy"
                    elif sells > buys and change_1h <= 0:
                        direction = "sell"
                    elif change_5m < -2 or change_1h < -3:
                        direction = "sell"
                    elif change_5m > 2 or change_1h > 3:
                        direction = "buy"
                    else:
                        direction = "buy" if change_24h >= 0 else "sell"

                    whales.append({
                        "type": direction,
                        "amount_usd": vol,
                        "token": pair.get("baseToken", {}).get("symbol", "???"),
                        "chain": pair.get("chainId", "unknown"),
                        "tx_url": pair.get("url", "https://dexscreener.com"),
                        "change_1h": change_1h,
                        "change_24h": change_24h,
                    })
            except Exception as e:
                log.error(f"Whale fetch error ({query}): {e}")

    # Deduplicate by token
    seen = set()
    unique = []
    for w in whales:
        if w["token"] not in seen:
            seen.add(w["token"])
            unique.append(w)

    unique.sort(key=lambda w: w["amount_usd"], reverse=True)
    return unique[:limit]


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
