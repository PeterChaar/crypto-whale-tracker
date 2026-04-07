#!/usr/bin/env python3
"""
Whale Monitor — Runs continuously, checks for large transactions,
sends Telegram alerts to PRO subscribers.
Free users get a teaser if they manually check /whales.
"""

import os
import sys
import json
import asyncio
import logging
import httpx
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Thresholds for whale alerts (in USD)
WHALE_THRESHOLD = 500_000  # $500K+ = whale
MEGA_WHALE_THRESHOLD = 5_000_000  # $5M+ = mega whale

# Check interval in seconds
CHECK_INTERVAL = 30  # every 30 seconds

# In-memory subscriber store (replace with Supabase later)
# Format: {chat_id: {"is_pro": bool, "subscribed_at": datetime}}
SUBSCRIBERS_FILE = os.path.join(os.path.dirname(__file__), "..", "subscribers.json")


def load_subscribers() -> dict:
    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_subscribers(subs: dict):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subs, f, indent=2, default=str)


def add_pro_subscriber(chat_id: int):
    subs = load_subscribers()
    subs[str(chat_id)] = {"is_pro": True, "subscribed_at": datetime.utcnow().isoformat()}
    save_subscribers(subs)


def get_pro_subscribers() -> list[int]:
    subs = load_subscribers()
    return [int(cid) for cid, info in subs.items() if info.get("is_pro")]


# Track already-sent alerts to avoid duplicates
# Format: {pair_id: timestamp_sent}
sent_alerts = {}
ALERT_COOLDOWN = 1800  # 30 minutes — re-alert same pair after cooldown


async def fetch_whale_transactions() -> list[dict]:
    """Fetch large transactions from multiple sources — both buys AND sells."""
    whales = []

    search_queries = [
        "WETH%20USDC",
        "SOL%20USDT",
        "WBTC%20USDT",
        "ETH%20USDT",
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for query in search_queries:
            try:
                r = await client.get(f"https://api.dexscreener.com/latest/dex/search?q={query}")
                if r.status_code != 200:
                    continue
                pairs = r.json().get("pairs", [])
                for p in pairs:
                    vol = p.get("volume", {}).get("h24", 0) or 0
                    if vol < WHALE_THRESHOLD:
                        continue
                    pair_id = p.get("pairAddress", "")[:10]
                    # Skip if alerted recently (within cooldown)
                    if pair_id in sent_alerts:
                        elapsed = (datetime.utcnow() - sent_alerts[pair_id]).total_seconds()
                        if elapsed < ALERT_COOLDOWN:
                            continue

                    change_5m = p.get("priceChange", {}).get("m5", 0) or 0
                    change_1h = p.get("priceChange", {}).get("h1", 0) or 0
                    change_24h = p.get("priceChange", {}).get("h24", 0) or 0
                    buys = p.get("txns", {}).get("h1", {}).get("buys", 0)
                    sells = p.get("txns", {}).get("h1", {}).get("sells", 0)

                    # Determine direction from actual txn counts + price movement
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
                        "id": pair_id,
                        "type": direction,
                        "token": p.get("baseToken", {}).get("symbol", "???"),
                        "amount_usd": vol,
                        "chain": p.get("chainId", "unknown"),
                        "change_5m": change_5m,
                        "change_1h": change_1h,
                        "change_24h": change_24h,
                        "buys_1h": buys,
                        "sells_1h": sells,
                        "url": p.get("url", "https://dexscreener.com"),
                        "is_mega": vol >= MEGA_WHALE_THRESHOLD,
                    })
            except Exception as e:
                log.error(f"DexScreener error ({query}): {e}")

    # Strict filter
    whales = [w for w in whales if w["amount_usd"] >= WHALE_THRESHOLD]
    # Deduplicate by pair_id
    seen = set()
    unique = []
    for w in whales:
        if w["id"] not in seen:
            seen.add(w["id"])
            unique.append(w)
    # Sort by volume descending, take top alerts
    unique.sort(key=lambda w: w["amount_usd"], reverse=True)
    return unique[:10]


def format_whale_alert(whale: dict) -> str:
    """Format a single whale alert for Telegram — buys AND sells."""
    is_sell = whale["type"] == "sell"
    emoji = "\U0001F6A8" if whale["is_mega"] else "\U0001F40B"  # 🚨 or 🐋
    direction = "\U0001F534" if is_sell else "\U0001F7E2"  # 🔴 or 🟢
    size_label = "MEGA WHALE" if whale["is_mega"] else "WHALE"
    action = "SELL" if is_sell else "BUY"
    chart_emoji = "\U0001F4C9" if is_sell else "\U0001F4C8"  # 📉 or 📈

    change_5m = whale.get("change_5m", 0)
    change_1h = whale.get("change_1h", 0)
    change_24h = whale.get("change_24h", 0)
    buys = whale.get("buys_1h", 0)
    sells = whale.get("sells_1h", 0)

    msg = (
        f"{emoji} *{size_label} {action} ALERT* {emoji}\n\n"
        f"{direction} *{action}* — {whale['token']}\n"
        f"\U0001F4B0 Volume: *${whale['amount_usd']:,.0f}*\n"
        f"\u26D3 Chain: {whale['chain']}\n"
        f"{chart_emoji} 5m: {change_5m:+.1f}% | 1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
    )
    if buys or sells:
        msg += f"\U0001F4CA Txns (1h): {buys} buys / {sells} sells\n"
    msg += f"\n[View on DexScreener]({whale['url']})"
    return msg


def format_whale_teaser(whale: dict) -> str:
    """Format a teaser for free users (limited info)."""
    is_sell = whale["type"] == "sell"
    direction = "\U0001F534 SELL" if is_sell else "\U0001F7E2 BUY"
    return (
        f"\U0001F40B *Whale {direction} Detected!*\n\n"
        f"\U0001F4B0 Volume: *${whale['amount_usd']:,.0f}*\n"
        f"Token: *???* | Chain: {whale['chain']}\n\n"
        f"\U0001F512 _Upgrade to PRO to see token name, charts & auto-alerts!_\n"
        f"Use /pro to upgrade"
    )


async def send_telegram_message(chat_id: int, text: str):
    """Send a message via Telegram Bot API."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as e:
            log.error(f"Failed to send to {chat_id}: {e}")


async def notify_pro_users(whales: list[dict]):
    """Send whale alerts to all PRO subscribers."""
    pro_users = get_pro_subscribers()
    if not pro_users:
        log.info("No PRO subscribers to notify")
        return

    for whale in whales:
        msg = format_whale_alert(whale)
        sent_alerts[whale["id"]] = datetime.utcnow()

        for chat_id in pro_users:
            await send_telegram_message(chat_id, msg)
            await asyncio.sleep(0.1)  # Rate limit

    log.info(f"Sent {len(whales)} alerts to {len(pro_users)} PRO users")


async def monitor_loop():
    """Main monitoring loop."""
    log.info("\U0001F40B Whale Monitor started!")
    log.info(f"Checking every {CHECK_INTERVAL}s | Threshold: ${WHALE_THRESHOLD:,}")

    while True:
        try:
            # Clean expired cooldowns
            now = datetime.utcnow()
            expired = [k for k, v in sent_alerts.items() if (now - v).total_seconds() >= ALERT_COOLDOWN]
            for k in expired:
                del sent_alerts[k]

            whales = await fetch_whale_transactions()
            new_whales = [w for w in whales if w["id"] not in sent_alerts or (now - sent_alerts[w["id"]]).total_seconds() >= ALERT_COOLDOWN]

            if new_whales:
                log.info(f"Found {len(new_whales)} new whale transactions")
                await notify_pro_users(new_whales)
            else:
                log.debug("No new whale transactions")

        except Exception as e:
            log.error(f"Monitor error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
