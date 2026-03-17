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
CHECK_INTERVAL = 120  # every 2 minutes

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
sent_alerts = set()


async def fetch_whale_transactions() -> list[dict]:
    """Fetch large transactions from multiple sources."""
    whales = []

    async with httpx.AsyncClient(timeout=15) as client:
        # Source 1: DexScreener — high volume pairs
        try:
            r = await client.get("https://api.dexscreener.com/latest/dex/search?q=WETH%20USDC")
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                for p in pairs:
                    vol = p.get("volume", {}).get("h24", 0) or 0
                    if vol >= WHALE_THRESHOLD:
                        pair_id = p.get("pairAddress", "")[:10]
                        if pair_id not in sent_alerts:
                            change = p.get("priceChange", {}).get("h24", 0) or 0
                            whales.append({
                                "id": pair_id,
                                "type": "buy" if change > 0 else "sell",
                                "token": p.get("baseToken", {}).get("symbol", "???"),
                                "amount_usd": vol,
                                "chain": p.get("chainId", "ethereum"),
                                "change_24h": change,
                                "url": p.get("url", "https://dexscreener.com"),
                                "is_mega": vol >= MEGA_WHALE_THRESHOLD,
                            })
        except Exception as e:
            log.error(f"DexScreener error: {e}")

        # Source 2: DexScreener — top gainers with high volume
        try:
            r = await client.get("https://api.dexscreener.com/latest/dex/search?q=SOL%20USDT")
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                for p in pairs:
                    vol = p.get("volume", {}).get("h24", 0) or 0
                    if vol >= WHALE_THRESHOLD:
                        pair_id = p.get("pairAddress", "")[:10]
                        if pair_id not in sent_alerts:
                            change = p.get("priceChange", {}).get("h24", 0) or 0
                            whales.append({
                                "id": pair_id,
                                "type": "buy" if change > 0 else "sell",
                                "token": p.get("baseToken", {}).get("symbol", "???"),
                                "amount_usd": vol,
                                "chain": p.get("chainId", "solana"),
                                "change_24h": change,
                                "url": p.get("url", "https://dexscreener.com"),
                                "is_mega": vol >= MEGA_WHALE_THRESHOLD,
                            })
        except Exception as e:
            log.error(f"DexScreener source 2 error: {e}")

    # Strict filter: nothing under $100K
    whales = [w for w in whales if w["amount_usd"] >= WHALE_THRESHOLD]
    # Sort by volume descending, take top alerts
    whales.sort(key=lambda w: w["amount_usd"], reverse=True)
    return whales[:8]


def format_whale_alert(whale: dict) -> str:
    """Format a single whale alert for Telegram."""
    emoji = "\U0001F6A8" if whale["is_mega"] else "\U0001F40B"  # 🚨 or 🐋
    direction = "\U0001F7E2" if whale["type"] == "buy" else "\U0001F534"  # 🟢 or 🔴
    size_label = "MEGA WHALE" if whale["is_mega"] else "WHALE"

    return (
        f"{emoji} *{size_label} ALERT* {emoji}\n\n"
        f"{direction} *{whale['type'].upper()}* — {whale['token']}\n"
        f"\U0001F4B0 Volume: *${whale['amount_usd']:,.0f}*\n"
        f"\u26D3 Chain: {whale['chain']}\n"
        f"\U0001F4C8 24h: {whale['change_24h']:+.1f}%\n\n"
        f"[View on DexScreener]({whale['url']})"
    )


def format_whale_teaser(whale: dict) -> str:
    """Format a teaser for free users (limited info)."""
    return (
        f"\U0001F40B *Whale Detected!*\n\n"
        f"{'Buy' if whale['type'] == 'buy' else 'Sell'} — *${whale['amount_usd']:,.0f}*\n"
        f"Token: *???* | Chain: {whale['chain']}\n\n"
        f"\U0001F512 _Upgrade to PRO to see full details and get auto-alerts!_\n"
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
        sent_alerts.add(whale["id"])

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
            whales = await fetch_whale_transactions()
            new_whales = [w for w in whales if w["id"] not in sent_alerts]

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
