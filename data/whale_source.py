#!/usr/bin/env python3
"""
Real whale detection.

The old detector used a DEX pair's 24h volume as if it were a single whale
trade, so it fired on the same pairs forever at +0.0%. This module reports
things that actually happened: a specific on-chain transfer, or a specific
market order that filled on an exchange.

Three keyless, free sources:

  1. Bitcoin  — every new block is scanned for transfers above a USD floor.
  2. Ethereum — USDT/USDC Transfer logs plus native ETH sends above the floor.
  3. Binance  — single aggregated fills ("block prints") and one-sided taker
                flow surges, both sized against each symbol's own liquidity
                so the thresholds hold up in a quiet or a busy market.

Every alert carries the evidence with it (tx hash or trade id), so anything
sent to a paying user can be checked on an explorer.
"""

import os
import time
import json
import asyncio
import logging
from collections import deque

import httpx

log = logging.getLogger(__name__)

# Public endpoints reject the default python-httpx agent, so present a browser.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

# ── Thresholds ───────────────────────────────────────────────────────────────
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Tunable without touching code: set these in .env and restart.
ONCHAIN_MIN_USD = _env_float("WR_ONCHAIN_MIN_USD", 5_000_000)
ONCHAIN_MEGA_USD = _env_float("WR_ONCHAIN_MEGA_USD", 20_000_000)
MAX_ONCHAIN_PER_SCAN = int(_env_float("WR_MAX_ONCHAIN_PER_SCAN", 2))

# A whale print is judged two ways: an absolute floor, and the top 0.1% of
# what this symbol normally trades. Both have to be cleared.
PRINT_FLOOR_USD = _env_float("WR_PRINT_FLOOR_USD", 50_000)
PRINT_MEGA_USD = _env_float("WR_PRINT_MEGA_USD", 250_000)

FLOW_WINDOW_S = 60               # taker flow is measured over one minute
FLOW_FLOOR_USD = _env_float("WR_FLOW_FLOOR_USD", 150_000)
FLOW_LIQUIDITY_SHARE = 0.75      # ...or 45s of the symbol's average volume
FLOW_MULTIPLE = 3.0              # ...and this many times its own recent norm
FLOW_COOLDOWN_S = _env_float("WR_FLOW_COOLDOWN_S", 900)

BINANCE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT",
]

ERC20_TOKENS = {
    "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6, 1.0),
    "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, 1.0),
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ETH_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
]

# ── Shared state ─────────────────────────────────────────────────────────────
_prices = {"BTC": 0.0, "ETH": 0.0}
_price_fetched_at = 0.0
_ticker_cache: dict[str, dict] = {}
_ticker_fetched_at = 0.0

_last_btc_height = 0
_last_eth_block = 0

_agg_cursor: dict[str, int] = {}          # symbol -> last aggTrade id seen
_print_sizes: dict[str, deque] = {}       # symbol -> recent fill notionals
_flow_history: dict[str, deque] = {}      # symbol -> recent 60s net flows
_flow_last_alert: dict[str, float] = {}   # symbol -> unix ts of last alert


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0), headers=HEADERS)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


# ── Prices and 24h context ───────────────────────────────────────────────────
async def refresh_market_context(client: httpx.AsyncClient):
    """Pull spot prices and 1h/24h change for every tracked symbol at once."""
    global _price_fetched_at, _ticker_fetched_at
    now = time.time()
    if now - _ticker_fetched_at < 60 and _ticker_cache:
        return
    symbols = json.dumps(BINANCE_SYMBOLS, separators=(",", ":"))
    try:
        day = await client.get(
            "https://api.binance.com/api/v3/ticker/24hr", params={"symbols": symbols}
        )
        hour = await client.get(
            "https://api.binance.com/api/v3/ticker",
            params={"symbols": symbols, "windowSize": "1h"},
        )
        if day.status_code != 200 or hour.status_code != 200:
            return
        hour_by_symbol = {h["symbol"]: h for h in hour.json()}
        for row in day.json():
            sym = row["symbol"]
            _ticker_cache[sym] = {
                "price": float(row["lastPrice"]),
                "change_24h": float(row["priceChangePercent"]),
                "change_1h": float(hour_by_symbol.get(sym, {}).get("priceChangePercent", 0) or 0),
                "quote_volume_24h": float(row["quoteVolume"]),
            }
        _prices["BTC"] = _ticker_cache.get("BTCUSDT", {}).get("price", _prices["BTC"])
        _prices["ETH"] = _ticker_cache.get("ETHUSDT", {}).get("price", _prices["ETH"])
        _ticker_fetched_at = now
        _price_fetched_at = now
    except Exception as e:
        log.warning(f"market context refresh failed: {e}")


def _context(symbol: str) -> dict:
    return _ticker_cache.get(symbol, {"price": 0.0, "change_1h": 0.0, "change_24h": 0.0})


# ── Source 1: Bitcoin on-chain ───────────────────────────────────────────────
async def scan_bitcoin(client: httpx.AsyncClient) -> list[dict]:
    """Scan each newly mined block for transfers above the whale floor."""
    global _last_btc_height
    out = []
    try:
        head = await client.get("https://blockchain.info/latestblock")
        if head.status_code != 200:
            return out
        head = head.json()
        height, block_hash = head["height"], head["hash"]
        if height == _last_btc_height:
            return out

        first_run = _last_btc_height == 0
        _last_btc_height = height
        if first_run:
            # Don't alert on a block that was already mined before we started.
            return out

        price = _prices.get("BTC") or 0
        if price <= 0:
            return out

        block = await client.get(f"https://blockchain.info/rawblock/{block_hash}")
        if block.status_code != 200:
            return out

        found = []
        for tx in block.json().get("tx", []):
            # Coinbase transactions are block rewards, not somebody moving money.
            if not tx.get("inputs") or not tx["inputs"][0].get("prev_out"):
                continue
            btc = sum(o.get("value", 0) for o in tx.get("out", [])) / 1e8
            usd = btc * price
            if usd >= ONCHAIN_MIN_USD:
                found.append((usd, btc, tx["hash"]))

        found.sort(reverse=True)
        for usd, btc, tx_hash in found[:MAX_ONCHAIN_PER_SCAN]:
            out.append({
                "id": f"btc:{tx_hash}",
                "kind": "onchain",
                "type": "move",
                "token": "BTC",
                "amount_usd": usd,
                "amount_native": btc,
                "unit": "BTC",
                "chain": "bitcoin",
                "venue": f"Bitcoin block {height:,}",
                "price": price,
                "change_1h": _context("BTCUSDT")["change_1h"],
                "change_24h": _context("BTCUSDT")["change_24h"],
                "url": f"https://mempool.space/tx/{tx_hash}",
                "is_mega": usd >= ONCHAIN_MEGA_USD,
                "detail": f"block {height:,}",
            })
        if found:
            log.info(f"BTC block {height}: {len(found)} transfers over ${ONCHAIN_MIN_USD:,}")
    except Exception as e:
        log.warning(f"bitcoin scan failed: {e}")
    return out


# ── Source 2: Ethereum on-chain ──────────────────────────────────────────────
async def _eth_rpc(client: httpx.AsyncClient, method: str, params: list):
    last_error = None
    for url in ETH_RPCS:
        try:
            r = await client.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            if r.status_code == 200:
                body = r.json()
                if "result" in body and body["result"] is not None:
                    return body["result"]
                last_error = body.get("error")
        except Exception as e:
            last_error = e
    if last_error:
        log.debug(f"eth rpc {method} failed: {last_error}")
    return None


async def scan_ethereum(client: httpx.AsyncClient) -> list[dict]:
    """Large stablecoin transfers and native ETH sends in the newest blocks."""
    global _last_eth_block
    out = []
    try:
        head_hex = await _eth_rpc(client, "eth_blockNumber", [])
        if not head_hex:
            return out
        head = int(head_hex, 16)
        if head == _last_eth_block:
            return out

        first_run = _last_eth_block == 0
        start = head if first_run else max(_last_eth_block + 1, head - 20)
        _last_eth_block = head
        if first_run:
            return out

        found = []

        for name, (address, decimals, usd_each) in ERC20_TOKENS.items():
            logs = await _eth_rpc(client, "eth_getLogs", [{
                "fromBlock": hex(start), "toBlock": hex(head),
                "address": address, "topics": [TRANSFER_TOPIC],
            }])
            for entry in logs or []:
                data = entry.get("data") or "0x"
                if data in ("0x", ""):
                    continue
                try:
                    amount = int(data, 16) / (10 ** decimals)
                except ValueError:
                    continue
                usd = amount * usd_each
                if usd >= ONCHAIN_MIN_USD:
                    found.append({
                        "usd": usd, "amount": amount, "unit": name,
                        "hash": entry["transactionHash"],
                        "key": f"erc20:{entry['transactionHash']}:{entry.get('logIndex','0')}",
                        "change_1h": 0.0, "change_24h": 0.0,
                    })

        eth_price = _prices.get("ETH") or 0
        if eth_price > 0:
            block = await _eth_rpc(client, "eth_getBlockByNumber", [hex(head), True])
            for tx in (block or {}).get("transactions", []):
                try:
                    native = int(tx["value"], 16) / 1e18
                except (KeyError, ValueError):
                    continue
                usd = native * eth_price
                if usd >= ONCHAIN_MIN_USD:
                    found.append({
                        "usd": usd, "amount": native, "unit": "ETH",
                        "hash": tx["hash"], "key": f"eth:{tx['hash']}",
                        "change_1h": _context("ETHUSDT")["change_1h"],
                        "change_24h": _context("ETHUSDT")["change_24h"],
                    })

        found.sort(key=lambda f: f["usd"], reverse=True)
        for f in found[:MAX_ONCHAIN_PER_SCAN]:
            out.append({
                "id": f["key"],
                "kind": "onchain",
                "type": "move",
                "token": f["unit"],
                "amount_usd": f["usd"],
                "amount_native": f["amount"],
                "unit": f["unit"],
                "chain": "ethereum",
                "venue": f"Ethereum block {head:,}",
                "price": eth_price if f["unit"] == "ETH" else 1.0,
                "change_1h": f["change_1h"],
                "change_24h": f["change_24h"],
                "url": f"https://etherscan.io/tx/{f['hash']}",
                "is_mega": f["usd"] >= ONCHAIN_MEGA_USD,
                "detail": f"block {head:,}",
            })
        if found:
            log.info(f"ETH blocks {start}-{head}: {len(found)} transfers over ${ONCHAIN_MIN_USD:,}")
    except Exception as e:
        log.warning(f"ethereum scan failed: {e}")
    return out


# ── Source 3: Binance order flow ─────────────────────────────────────────────
async def _new_agg_trades(client: httpx.AsyncClient, symbol: str) -> list[dict]:
    """Every aggregated fill since the last poll (a few pages at most)."""
    trades: list[dict] = []
    cursor = _agg_cursor.get(symbol)
    for _ in range(3):
        params = {"symbol": symbol, "limit": 1000}
        if cursor:
            params["fromId"] = cursor + 1
        r = await client.get("https://api.binance.com/api/v3/aggTrades", params=params)
        if r.status_code != 200:
            break
        page = r.json()
        if not page:
            break
        trades.extend(page)
        cursor = page[-1]["a"]
        if len(page) < 1000:
            break
    if trades:
        _agg_cursor[symbol] = trades[-1]["a"]
    elif cursor:
        _agg_cursor[symbol] = cursor
    return trades


def _detect_prints(symbol: str, trades: list[dict], seeding: bool) -> list[dict]:
    """A single fill that dwarfs this symbol's normal trade size."""
    sizes = _print_sizes.setdefault(symbol, deque(maxlen=5000))
    base = symbol[:-4]
    ctx = _context(symbol)
    out = []

    for t in trades:
        notional = float(t["p"]) * float(t["q"])
        # A whale is judged against what came before it, not against itself.
        threshold = max(PRINT_FLOOR_USD, _percentile(list(sizes), 0.999)) if len(sizes) >= 500 else PRINT_FLOOR_USD
        sizes.append(notional)
        if seeding or notional < threshold:
            continue
        is_sell = t["m"]  # buyer was the maker, so the taker sold into the bid
        out.append({
            "id": f"print:{symbol}:{t['a']}",
            "kind": "print",
            "type": "sell" if is_sell else "buy",
            "token": base,
            "amount_usd": notional,
            "amount_native": float(t["q"]),
            "unit": base,
            "chain": "binance",
            "venue": "Binance spot",
            "price": float(t["p"]),
            "change_1h": ctx["change_1h"],
            "change_24h": ctx["change_24h"],
            "url": f"https://www.binance.com/en/trade/{base}_USDT",
            "is_mega": notional >= PRINT_MEGA_USD,
            "detail": f"single fill of {float(t['q']):,.4g} {base} at ${float(t['p']):,.4f}",
        })

    # One symbol should never own the whole feed.
    out.sort(key=lambda w: w["amount_usd"], reverse=True)
    return out[:2]


def _detect_flow(symbol: str, trades: list[dict], seeding: bool) -> list[dict]:
    """One-sided taker pressure inside a 60 second window."""
    now = time.time()
    cutoff_ms = (now - FLOW_WINDOW_S) * 1000
    buys = sum(float(t["p"]) * float(t["q"]) for t in trades if not t["m"] and t["T"] >= cutoff_ms)
    sells = sum(float(t["p"]) * float(t["q"]) for t in trades if t["m"] and t["T"] >= cutoff_ms)
    net = buys - sells

    history = _flow_history.setdefault(symbol, deque(maxlen=60))
    baseline = _percentile([abs(x) for x in history], 0.80) if len(history) >= 10 else 0.0
    history.append(net)

    # A thin alt and BTC cannot share one dollar threshold, so the floor is
    # also expressed as a slice of what this symbol trades in a normal minute.
    per_minute = _context(symbol).get("quote_volume_24h", 0.0) / 1440 if _ticker_cache.get(symbol) else 0.0
    floor = max(FLOW_FLOOR_USD, per_minute * FLOW_LIQUIDITY_SHARE)

    if seeding or abs(net) < floor:
        return []
    if baseline and abs(net) < baseline * FLOW_MULTIPLE:
        return []
    if now - _flow_last_alert.get(symbol, 0) < FLOW_COOLDOWN_S:
        return []

    _flow_last_alert[symbol] = now
    base = symbol[:-4]
    ctx = _context(symbol)
    return [{
        "id": f"flow:{symbol}:{int(now // FLOW_WINDOW_S)}",
        "kind": "flow",
        "type": "buy" if net > 0 else "sell",
        "token": base,
        "amount_usd": abs(net),
        "amount_native": 0.0,
        "unit": base,
        "chain": "binance",
        "venue": "Binance spot",
        "price": ctx["price"],
        "change_1h": ctx["change_1h"],
        "change_24h": ctx["change_24h"],
        "url": f"https://www.binance.com/en/trade/{base}_USDT",
        "is_mega": abs(net) >= FLOW_FLOOR_USD * 4,
        "detail": f"${buys:,.0f} taker buys vs ${sells:,.0f} sells in {FLOW_WINDOW_S}s",
        "window_buys_usd": buys,
        "window_sells_usd": sells,
    }]


async def scan_binance(client: httpx.AsyncClient) -> list[dict]:
    out = []
    for symbol in BINANCE_SYMBOLS:
        try:
            seeding = symbol not in _agg_cursor
            trades = await _new_agg_trades(client, symbol)
            if not trades:
                continue
            out.extend(_detect_prints(symbol, trades, seeding))
            out.extend(_detect_flow(symbol, trades, seeding))
        except Exception as e:
            log.debug(f"binance scan failed for {symbol}: {e}")
    return out


# ── Public API ───────────────────────────────────────────────────────────────
async def fetch_whales(include_onchain: bool = True) -> list[dict]:
    """All fresh whale events, largest first."""
    async with _client() as client:
        await refresh_market_context(client)
        tasks = [scan_binance(client)]
        if include_onchain:
            tasks += [scan_bitcoin(client), scan_ethereum(client)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    whales = []
    for r in results:
        if isinstance(r, Exception):
            log.warning(f"source error: {r}")
            continue
        whales.extend(r)
    whales.sort(key=lambda w: w["amount_usd"], reverse=True)
    return whales


async def fetch_recent_snapshot(limit: int = 5) -> list[dict]:
    """
    What the /whales command shows on demand.

    An on-demand check cannot wait for the next block, so this reports the
    largest fills currently visible in Binance's recent trade history. It is
    still real trade data, just the last few minutes rather than live.
    """
    async with _client() as client:
        await refresh_market_context(client)
        out = []
        for symbol in BINANCE_SYMBOLS:
            try:
                r = await client.get(
                    "https://api.binance.com/api/v3/aggTrades",
                    params={"symbol": symbol, "limit": 1000},
                )
                if r.status_code != 200:
                    continue
                trades = r.json()
                if not trades:
                    continue
                base = symbol[:-4]
                ctx = _context(symbol)
                biggest = max(trades, key=lambda t: float(t["p"]) * float(t["q"]))
                notional = float(biggest["p"]) * float(biggest["q"])
                age_s = max(0, time.time() - biggest["T"] / 1000)
                out.append({
                    "id": f"print:{symbol}:{biggest['a']}",
                    "kind": "print",
                    "type": "sell" if biggest["m"] else "buy",
                    "token": base,
                    "amount_usd": notional,
                    "amount_native": float(biggest["q"]),
                    "unit": base,
                    "chain": "binance",
                    "venue": "Binance spot",
                    "price": float(biggest["p"]),
                    "change_1h": ctx["change_1h"],
                    "change_24h": ctx["change_24h"],
                    "url": f"https://www.binance.com/en/trade/{base}_USDT",
                    "tx_url": f"https://www.binance.com/en/trade/{base}_USDT",
                    "is_mega": notional >= PRINT_MEGA_USD,
                    "age_s": age_s,
                    "detail": f"{float(biggest['q']):,.4g} {base} at ${float(biggest['p']):,.4f}",
                })
            except Exception as e:
                log.debug(f"snapshot failed for {symbol}: {e}")
    out.sort(key=lambda w: w["amount_usd"], reverse=True)
    return out[:limit]
