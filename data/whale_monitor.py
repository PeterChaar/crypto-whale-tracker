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

# Survives an ISP that stops resolving api.telegram.org (see net_resilient.py).
import net_resilient  # noqa: F401,E402
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Thresholds for whale alerts (in USD)
# Thresholds now live in data/whale_source.py, where each one is sized
# against the asset's own liquidity instead of a single global number.
from data.whale_source import ONCHAIN_MIN_USD, PRINT_FLOOR_USD
from data import track_record

MAX_ALERTS_PER_CYCLE = 6  # a paid feed people keep reading, not a firehose

# ── Public channel (the distribution engine) ─────────────────────────────────
# The product cannot be sold by talking to strangers one at a time. A public
# channel posts the huge on-chain moves for free, because those are already
# public information and they are what people forward into group chats. The
# tradeable part, exchange order flow and instant delivery, stays behind Pro.
# Every post carries the bot link, so a forward into any crypto group is a
# funnel that runs without anyone selling anything.
PUBLIC_CHANNEL = os.environ.get("WR_PUBLIC_CHANNEL", "").strip()
PUBLIC_MIN_USD = float(os.environ.get("WR_PUBLIC_MIN_USD", 10_000_000))
MAX_PUBLIC_PER_CYCLE = 2
BOT_START_LINK = os.environ.get(
    "WR_BOT_LINK", "https://t.me/Whaleradarbot_bot?start=channel"
)

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
    """Real whale events: on-chain transfers plus large exchange fills."""
    from data.whale_source import fetch_whales
    return await fetch_whales()


def _money(n: float) -> str:
    """$177,030,737 reads slower than $177.0M in a phone notification."""
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:,.0f}"


def _qty(n: float) -> str:
    """Coin amounts, never in scientific notation (1.101e+06 helps nobody)."""
    if n >= 1_000_000:
        return f"{n:,.0f}"
    if n >= 1:
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"{n:.6f}".rstrip("0").rstrip(".")


def _price(n: float) -> str:
    """Prices, never as $6.356e+04."""
    if n >= 1000:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:,.4f}".rstrip("0").rstrip(".")
    return f"{n:.8f}".rstrip("0").rstrip(".")


def format_whale_alert(whale: dict) -> str:
    """
    Format one real whale event. Every alert names what happened, how big it
    was, and links to the proof (a tx hash on-chain, the pair on the exchange).
    """
    kind = whale.get("kind", "print")
    token = whale["token"]
    usd = whale["amount_usd"]
    change_1h = whale.get("change_1h", 0) or 0
    change_24h = whale.get("change_24h", 0) or 0
    mega = whale.get("is_mega")

    if kind == "onchain":
        head = "\U0001F6A8 *MEGA WHALE MOVE*" if mega else "\U0001F40B *WHALE MOVE*"
        native = whale.get("amount_native", 0)
        unit = whale.get("unit", token)
        amount = f"{_qty(native)} {unit}" if native else _money(usd)
        msg = (
            f"{head}\n\n"
            f"*{amount}* moved on {whale.get('chain', '').title()}\n"
            f"\U0001F4B0 Value: *{_money(usd)}*\n"
        )
        if unit not in ("USDT", "USDC"):
            msg += f"\U0001F4C8 {unit} 1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
        msg += f"\n[Verify on explorer]({whale['url']})"
        return msg

    is_sell = whale["type"] == "sell"
    side = "SELL" if is_sell else "BUY"
    dot = "\U0001F534" if is_sell else "\U0001F7E2"
    chart = "\U0001F4C9" if is_sell else "\U0001F4C8"

    if kind == "flow":
        head = "\U0001F6A8 *WHALE FLOW SURGE*" if mega else "\U0001F30A *WHALE FLOW*"
        buys = whale.get("window_buys_usd", 0)
        sells = whale.get("window_sells_usd", 0)
        return (
            f"{head}\n\n"
            f"{dot} *{_money(usd)} net {side}* on {token} in 60 seconds\n"
            f"\U0001F4CA Takers: {_money(buys)} bought vs {_money(sells)} sold\n"
            f"\U0001F4B5 Price: ${_price(whale.get('price', 0))}\n"
            f"{chart} 1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
            f"\n[Trade {token}]({whale['url']})"
        )

    head = "\U0001F6A8 *BLOCK TRADE*" if mega else "\U0001F40B *WHALE ORDER*"
    return (
        f"{head}\n\n"
        f"{dot} *{_money(usd)} market {side}* \u2014 {token}\n"
        f"\U0001F4E6 Size: {_qty(whale.get('amount_native', 0))} {token} "
        f"@ ${_price(whale.get('price', 0))}\n"
        f"\U0001F3E6 {whale.get('venue', 'exchange')}, filled in one order\n"
        f"{chart} 1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
        f"\n[Trade {token}]({whale['url']})"
    )


def format_whale_teaser(whale: dict) -> str:
    """Free users see that something real happened, not what it was."""
    kind = whale.get("kind", "print")
    usd = whale["amount_usd"]
    if kind == "onchain":
        line = f"\U0001F40B *Whale transfer detected on {whale.get('chain', 'chain').title()}*"
        what = f"\U0001F4B0 Value: *{_money(usd)}*\nAsset: *???*"
    else:
        side = "\U0001F534 SELL" if whale["type"] == "sell" else "\U0001F7E2 BUY"
        line = f"\U0001F40B *Whale {side} detected*"
        what = f"\U0001F4B0 Size: *{_money(usd)}*\nToken: *???* | {whale.get('venue', '')}"
    return (
        f"{line}\n\n{what}\n\n"
        "\U0001F512 _PRO shows the asset, the price, the wallet link and sends "
        "these the second they happen._\n"
        "Use /pro to upgrade"
    )


def format_public_post(whale: dict) -> str:
    """
    A channel post is an advert that happens to be useful.

    It gives away the whole on-chain event, including the explorer link, so
    nobody can call it bait, and then names the one thing it withheld.
    """
    usd = whale["amount_usd"]
    native = whale.get("amount_native", 0)
    unit = whale.get("unit", whale["token"])
    chain = whale.get("chain", "").title()
    amount = f"{_qty(native)} {unit}" if native else _money(usd)
    heat = "\U0001F6A8\U0001F6A8" if whale.get("is_mega") else "\U0001F40B"

    return (
        f"{heat} *{_money(usd)} WHALE MOVE*\n\n"
        f"*{amount}* just moved on {chain}\n"
        f"\U0001F517 [See the transaction]({whale['url']})\n\n"
        f"_Exchange order flow, who is buying and selling right now, goes to "
        f"PRO members the second it happens._\n"
        f"\U0001F449 [Get whale alerts]({BOT_START_LINK})"
    )


async def post_due_followups():
    """
    Post what the price did after an earlier alert, as a reply under it.

    This is the channel's proof. It runs whatever the outcome was, so the
    thread shows the misses next to the hits.
    """
    if not PUBLIC_CHANNEL:
        return
    due = track_record.take_due()
    if not due:
        return

    async with httpx.AsyncClient(timeout=20) as client:
        for row in due:
            try:
                r = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": row["symbol"]},
                )
                if r.status_code != 200:
                    continue
                price_now = float(r.json()["price"])
            except Exception as e:
                log.warning(f"follow-up price fetch failed: {e}")
                continue

            text = track_record.format_followup(row, price_now)
            if text:
                await send_telegram_message(
                    PUBLIC_CHANNEL, text, reply_to=row.get("message_id")
                )
                log.info(f"posted follow-up for message {row.get('message_id')}")


async def broadcast_public(whales: list[dict]):
    """Post the biggest on-chain moves to the public channel, if configured."""
    if not PUBLIC_CHANNEL:
        return

    posts = [
        w for w in whales
        if w.get("kind") == "onchain" and w["amount_usd"] >= PUBLIC_MIN_USD
    ][:MAX_PUBLIC_PER_CYCLE]
    if not posts:
        return

    sent = 0
    for whale in posts:
        message_id = await send_telegram_message(PUBLIC_CHANNEL, format_public_post(whale))
        if message_id:
            sent += 1
            # Schedule the proof that goes under this exact post.
            track_record.remember(whale, message_id)
        await asyncio.sleep(0.5)
    log.info(f"Posted {sent}/{len(posts)} to public channel {PUBLIC_CHANNEL}")


async def send_telegram_message(chat_id, text: str, reply_to: int | None = None):
    """
    Send one message and report whether it truly landed.

    Returns the Telegram message_id on success and None on failure, so callers
    can both count deliveries and reply under what they just posted.

    The old version fired the request and ignored the response, so a token
    called WIF_2 breaking Markdown came back 400 and looked exactly like a
    delivered alert. A paying user would have silently received nothing.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        # A follow-up whose parent was deleted should still post.
        payload["allow_sending_without_reply"] = True
    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(3):
            try:
                r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
                if r.status_code == 200:
                    return r.json().get("result", {}).get("message_id") or True

                body = r.text[:200]
                # Bad formatting is permanent: retrying sends the same 400
                # forever. Drop the markup and deliver the content instead.
                if r.status_code == 400 and "parse" in body.lower():
                    log.warning(f"markdown rejected for {chat_id}, resending as plain text")
                    plain = dict(payload)
                    plain.pop("parse_mode")
                    r2 = await client.post(f"{TELEGRAM_API}/sendMessage", json=plain)
                    if r2.status_code == 200:
                        return r2.json().get("result", {}).get("message_id") or True
                    log.error(f"plain-text retry failed for {chat_id}: {r2.status_code} {r2.text[:200]}")
                    return None

                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 3)
                    log.warning(f"rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue

                log.error(f"send to {chat_id} failed: {r.status_code} {body}")
                return None
            except Exception as e:
                log.warning(f"send to {chat_id} attempt {attempt + 1} errored: {e!r}")
                await asyncio.sleep(2 * (attempt + 1))
    log.error(f"send to {chat_id} gave up after 3 attempts")
    return None


async def notify_pro_users(whales: list[dict]):
    """Send whale alerts to all PRO subscribers."""
    pro_users = get_pro_subscribers()
    if not pro_users:
        log.info("No PRO subscribers to notify")
        return

    delivered = 0
    failed = 0
    for whale in whales:
        msg = format_whale_alert(whale)
        sent_alerts[whale["id"]] = datetime.utcnow()

        for chat_id in pro_users:
            if await send_telegram_message(chat_id, msg):
                delivered += 1
            else:
                failed += 1
            await asyncio.sleep(0.1)  # Rate limit

    # Count what Telegram accepted, not what we attempted.
    log.info(
        f"Delivered {delivered}/{delivered + failed} alerts to {len(pro_users)} PRO users"
        + (f" ({failed} FAILED)" if failed else "")
    )


async def monitor_loop():
    """Main monitoring loop."""
    log.info("\U0001F40B Whale Monitor started!")
    log.info(
        f"Checking every {CHECK_INTERVAL}s | on-chain floor ${ONCHAIN_MIN_USD:,.0f} "
        f"| exchange print floor ${PRINT_FLOOR_USD:,.0f}"
    )

    while True:
        try:
            # Clean expired cooldowns
            now = datetime.utcnow()
            expired = [k for k, v in sent_alerts.items() if (now - v).total_seconds() >= ALERT_COOLDOWN]
            for k in expired:
                del sent_alerts[k]

            # Proof for earlier alerts goes out even in a quiet cycle.
            await post_due_followups()

            whales = await fetch_whale_transactions()
            new_whales = [w for w in whales if w["id"] not in sent_alerts or (now - sent_alerts[w["id"]]).total_seconds() >= ALERT_COOLDOWN]

            if new_whales:
                new_whales.sort(key=lambda w: w["amount_usd"], reverse=True)
                batch = new_whales[:MAX_ALERTS_PER_CYCLE]
                dropped = len(new_whales) - len(batch)
                log.info(
                    f"Found {len(new_whales)} whale events, sending {len(batch)}"
                    + (f" (dropped {dropped} smaller)" if dropped else "")
                )
                await notify_pro_users(batch)
                # Pro users are served first, then the public feed that
                # brings the next Pro user in.
                await broadcast_public(new_whales)
            else:
                log.debug("No new whale transactions")

        except Exception as e:
            log.error(f"Monitor error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
